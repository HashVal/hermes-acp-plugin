"""Gateway side of agent-bridge: ACP client, session runtime, chat commands."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import acp
from acp.schema import (AllowedOutcome, ClientCapabilities, DeniedOutcome,
                        RequestPermissionResponse)
from acp.transports import default_environment

from . import approvals
from .config import load_config
from .store import SessionStore

logger = logging.getLogger(__name__)

_store = SessionStore()
_sessions: Dict[str, "AgentSession"] = {}
# Reserve capacity before session startup or replacement can yield to another task.
_starting: Dict[str, str] = {}
_tasks: Set[asyncio.Task] = set()
_reaper: Optional[asyncio.Task] = None

# ACP passes only HOME, LOGNAME, PATH, SHELL, TERM, USER to the adapter; add these.
_PASS_ENV = ("LANG", "LC_ALL", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
             "http_proxy", "https_proxy", "no_proxy", "all_proxy")
_AGENT_ALIASES = {"cc": "claude", "claude": "claude", "codex": "codex"}

_HELP = """agent-bridge commands:
/cc [--cwd DIR] [prompt]     start a Claude Code session, or send to it
/codex [--cwd DIR] [prompt]  start a Codex session, or send to it
/agent status | list | cancel | close
/agent mode [ID]             show or set the permission mode
/hermes <text>               send one message to Hermes while a session is open
/approve [session|all] | /deny   answer an agent permission request
Plain messages go to the open session until /agent close."""

class _Chat:
    """Where to send replies for one session (refreshed on every inbound message)."""

    def __init__(self, adapter, chat_id: str, thread_id: Optional[str],
                 reply_to_message_id: Optional[str] = None):
        self.adapter = adapter
        self.chat_id = chat_id
        self.metadata = {"thread_id": thread_id} if thread_id else None
        if self.metadata is not None and reply_to_message_id:
            # Feishu needs a message anchor for both replies and approval cards.
            self.metadata["reply_to_message_id"] = reply_to_message_id

    async def say(self, text: str) -> None:
        try:
            result = await self.adapter.send(self.chat_id, text, metadata=self.metadata)
            if not getattr(result, "success", True):
                logger.error("agent-bridge: send to %s (thread %s) failed: %s",
                             self.chat_id, (self.metadata or {}).get("thread_id"),
                             getattr(result, "error", None) or "unknown send error")
        except Exception:
            logger.exception("agent-bridge: send to %s failed", self.chat_id)


class _BridgeClient:
    """ACP client side: receives agent updates and permission requests."""

    def __init__(self, sess: "AgentSession"):
        self.sess = sess

    async def request_permission(self, options, session_id, tool_call, **kw):
        return await self.sess.ask_permission(options, tool_call)

    async def session_update(self, session_id, update, **kw):
        self.sess.on_update(update)

    async def ext_method(self, method, params):
        return {}

    async def ext_notification(self, method, params):
        pass


def _summarize_tool_call(tool_call) -> str:
    """Card text: title, paths, then raw input or diff (Codex sends paths/diffs, not raw input)."""
    parts = [getattr(tool_call, "title", None) or "tool call"]
    paths = [getattr(loc, "path", "") for loc in (getattr(tool_call, "locations", None) or [])]
    if any(paths):
        parts.append("files: " + ", ".join(p for p in paths if p))
    raw = getattr(tool_call, "raw_input", None)
    if raw not in (None, {}, ""):
        try:
            parts.append(raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, indent=1))
        except Exception:
            parts.append(str(raw))
    else:
        for item in getattr(tool_call, "content", None) or []:
            if getattr(item, "type", "") == "diff":
                parts.append(f"--- {item.path}\n{item.new_text or ''}")
    detail = "\n".join(parts)
    return detail if len(detail) <= 1500 else detail[:1500] + " …"


def _pick_option(options, kinds) -> Optional[str]:
    for kind in kinds:
        for opt in options:
            if str(opt.kind) == kind or getattr(opt.kind, "value", None) == kind:
                return opt.option_id
    return None


class AgentSession:
    """One ACP adapter process + one agent session, bound to one Hermes chat."""

    def __init__(self, key: str, agent: str, cwd: str, cfg: Dict[str, Any],
                 acp_session_id: Optional[str] = None, mode: Optional[str] = None):
        self.key = key
        self.agent = agent
        self.cwd = cwd
        self.cfg = cfg
        self.acp_session_id = acp_session_id
        self.mode = mode or cfg["agents"][agent].get("mode")
        self.approval_key = approvals.key_for(key)
        self.label = cfg["agents"][agent].get("label", agent)
        self.chat: Optional[_Chat] = None
        self.conn = None
        self.proc = None
        self.modes: List[str] = []
        self.busy = False
        self.last_used = time.monotonic()
        self._stack: Optional[contextlib.AsyncExitStack] = None
        self._log = None
        self._loading = False
        self._text: List[str] = []
        self._tools: List[str] = []

    @property
    def running(self) -> bool:
        return self.conn is not None and self.proc is not None and self.proc.returncode is None

    def record(self) -> Dict[str, Any]:
        return {"agent": self.agent, "cwd": self.cwd, "acp_session_id": self.acp_session_id,
                "mode": self.mode, "updated": int(time.time())}

    async def ensure_started(self) -> None:
        if self.running:
            return
        await self.stop()
        acfg = self.cfg["agents"][self.agent]
        command = acfg["command"]
        if not os.path.isabs(command):
            command = str(Path(self.cfg["adapters_dir"]) / command)
        if not os.path.exists(command) and not shutil.which(command):
            raise RuntimeError(f"adapter not found: {command}")
        env = default_environment()
        env.update({k: os.environ[k] for k in _PASS_ENV if k in os.environ})
        env.update({k: str(v) for k, v in (acfg.get("env") or {}).items()})
        log_dir = Path(self.cfg.get("log_dir") or _store_dir() / "logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        self._log = open(log_dir / f"{self.agent}.log", "ab")
        self._stack = contextlib.AsyncExitStack()
        self.conn, self.proc = await self._stack.enter_async_context(acp.spawn_agent_process(
            _BridgeClient(self), command, env=env, cwd=self.cwd,
            transport_kwargs={"stderr": self._log, "limit": 16 * 1024 * 1024}))
        await self.conn.initialize(protocol_version=acp.PROTOCOL_VERSION,
                                   client_capabilities=ClientCapabilities())
        if self.acp_session_id:
            # Adapters replay the history as updates during load; drop them.
            self._loading = True
            try:
                resp = await self.conn.load_session(cwd=self.cwd, session_id=self.acp_session_id)
            finally:
                self._loading = False
        else:
            resp = await self.conn.new_session(cwd=self.cwd)
            self.acp_session_id = resp.session_id
        modes = getattr(resp, "modes", None)
        self.modes = [m.id for m in modes.available_modes] if modes else []
        if self.mode and self.mode in self.modes and self.mode != modes.current_mode_id:
            await self.conn.set_session_mode(mode_id=self.mode, session_id=self.acp_session_id)
        elif modes and self.mode not in self.modes:
            self.mode = modes.current_mode_id
        _store.put(self.key, self.record())

    async def set_mode(self, mode_id: str) -> str:
        await self.ensure_started()
        if mode_id not in self.modes:
            return f"Unknown mode {mode_id!r}. Available: {', '.join(self.modes) or 'none'}"
        await self.conn.set_session_mode(mode_id=mode_id, session_id=self.acp_session_id)
        self.mode = mode_id
        _store.put(self.key, self.record())
        return f"{self.label} mode set to {mode_id}."

    async def stop(self) -> None:
        approvals.release(self.approval_key)
        stack, self._stack = self._stack, None
        self.conn = self.proc = None
        if stack is not None:
            with contextlib.suppress(Exception):
                await stack.aclose()
        if self._log is not None:
            with contextlib.suppress(Exception):
                self._log.close()
            self._log = None

    # --- ACP callbacks -------------------------------------------------------

    def on_update(self, update) -> None:
        if self._loading:
            return
        kind = getattr(update, "session_update", "")
        if kind == "agent_message_chunk":
            text = getattr(getattr(update, "content", None), "text", None)
            if text:
                self._text.append(text)
        elif kind == "tool_call":
            title = getattr(update, "title", None)
            if title:
                self._tools.append(title)

    async def ask_permission(self, options, tool_call) -> RequestPermissionResponse:
        allow_once = _pick_option(options, ("allow_once",))
        allow_always = _pick_option(options, ("allow_always",))
        reject = _pick_option(options, ("reject_once", "reject_always"))
        summary = _summarize_tool_call(tool_call)
        entry = approvals.enqueue(self.approval_key, {"command": summary, "agent": self.agent})
        chat = self.chat
        sent = False
        if chat is not None and hasattr(chat.adapter, "send_exec_approval"):
            try:
                result = await chat.adapter.send_exec_approval(
                    chat.chat_id, summary, session_key=self.approval_key,
                    description=f"{self.label} wants to run",
                    metadata=chat.metadata, allow_permanent=False,
                    allow_session=allow_always is not None)
                sent = bool(getattr(result, "success", True))
            except Exception:
                logger.exception("agent-bridge: approval card failed")
        if chat is not None and not sent:
            await chat.say(f"🔐 {self.label} asks permission:\n{summary}\n\n"
                           "Reply /approve, /approve session, or /deny.")
        choice = await approvals.wait(self.approval_key, entry, self.cfg["approval_timeout"])
        if choice is None and chat is not None:
            await chat.say(f"⌛ {self.label} permission request expired or was cancelled; rejected.")
        option = None
        if choice == "once":
            option = allow_once or allow_always
        elif choice in ("session", "always"):
            option = allow_always or allow_once
        elif reject:
            option = reject
        if option is None:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        return RequestPermissionResponse(outcome=AllowedOutcome(outcome="selected", option_id=option))

    # --- turns ---------------------------------------------------------------

    async def prompt(self, text: str) -> None:
        self.busy = True
        self.last_used = time.monotonic()
        self._text, self._tools = [], []
        ticker = asyncio.create_task(self._progress())
        try:
            await self.ensure_started()
            resp = await self.conn.prompt(prompt=[acp.text_block(text)],
                                          session_id=self.acp_session_id)
            stop = str(getattr(resp, "stop_reason", "") or "")
            reply = "".join(self._text).strip() or "(no text reply)"
            limit = self.cfg["max_reply_chars"]
            if len(reply) > limit:
                reply = reply[:limit] + "\n… (truncated)"
            if stop and stop not in ("end_turn", "StopReason.end_turn"):
                reply += f"\n\n[{self.label} stopped: {stop}]"
            await self.chat.say(reply)
        except Exception as exc:
            logger.exception("agent-bridge: %s turn failed", self.agent)
            await self.chat.say(f"❌ {self.label} error: {exc}")
            if not self.running:
                await self.stop()
        finally:
            ticker.cancel()
            self.busy = False
            self.last_used = time.monotonic()
            # A closing turn must not recreate a closed or replaced session.
            if _sessions.get(self.key) is self:
                _store.put(self.key, self.record())

    async def _progress(self) -> None:
        interval = self.cfg["progress_interval"]
        shown = 0
        while True:
            await asyncio.sleep(interval)
            if len(self._tools) > shown and self.chat is not None:
                new = self._tools[shown:]
                shown = len(self._tools)
                lines = "\n".join(f"• {t}" for t in new[-8:])
                await self.chat.say(f"⏳ {self.label} working…\n{lines}")

    async def cancel(self) -> None:
        approvals.release(self.approval_key)
        if self.running and self.acp_session_id:
            with contextlib.suppress(Exception):
                await self.conn.cancel(session_id=self.acp_session_id)


def _store_dir() -> Path:
    from .store import BRIDGE_HOME
    return BRIDGE_HOME


def _spawn(coro) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _reap_idle() -> None:
    """Stop adapter processes idle for too long. The saved session stays resumable."""
    while True:
        await asyncio.sleep(300)
        limit = load_config()["idle_timeout"]
        now = time.monotonic()
        for sess in list(_sessions.values()):
            if sess.running and not sess.busy and now - sess.last_used > limit:
                logger.info("agent-bridge: stopping idle %s for %s", sess.agent, sess.key)
                await sess.stop()


def _parse_start_args(args: str):
    """'--cwd DIR rest of prompt' -> (DIR or None, prompt)."""
    parts = args.strip().split(None, 2)
    # Feishu/phone keyboards turn "--" into an em/en dash.
    if parts and parts[0] in ("--cwd", "—cwd", "–cwd", "-cwd") and len(parts) >= 2:
        return os.path.expanduser(parts[1]), (parts[2] if len(parts) > 2 else "")
    return None, args.strip()


def _get_session(key: str, cfg) -> Optional[AgentSession]:
    sess = _sessions.get(key)
    if sess is None:
        rec = _store.get(key)
        if rec and rec.get("agent") in cfg["agents"]:
            sess = AgentSession(key, rec["agent"], rec["cwd"], cfg,
                                acp_session_id=rec.get("acp_session_id"), mode=rec.get("mode"))
            _sessions[key] = sess
    return sess


async def _close(key: str) -> bool:
    sess = _sessions.pop(key, None)
    rec = _store.pop(key)
    if sess is not None:
        await sess.cancel()
        await sess.stop()
    return bool(sess or rec)


async def _send_to(sess: AgentSession, text: str) -> None:
    if sess.key in _starting:
        await sess.chat.say("This agent session is still starting. Wait, then resend your message.")
        return
    if sess.busy:
        await sess.chat.say(f"{sess.label} is still working. Wait, or /agent cancel.")
        return
    await sess.prompt(text)


def _codex_session_keys() -> Set[str]:
    """Count each open session once, including saved and still-starting sessions."""
    keys = {key for key, rec in _store.all().items() if rec.get("agent") == "codex"}
    keys.update(key for key, sess in _sessions.items() if sess.agent == "codex")
    keys.update(key for key, agent in _starting.items() if agent == "codex")
    return keys


def _codex_capacity(cfg) -> str:
    return f"Codex sessions: {len(_codex_session_keys())}/{cfg['codex_limit']} (limit)."


async def _start(key: str, agent: str, args: str, chat: _Chat, cfg) -> None:
    if key in _starting:
        await chat.say("This agent session is still starting. Wait, then retry.")
        return
    cwd, prompt = _parse_start_args(args)
    sess = _get_session(key, cfg)
    if sess is not None and sess.agent == agent and cwd is None:
        sess.chat = chat
        if prompt:
            await _send_to(sess, prompt)
        else:
            await chat.say(f"{sess.label} session is open in {sess.cwd}. Send a message.")
        return
    if sess is not None and sess.busy:
        await chat.say(f"{sess.label} is still working. /agent cancel or /agent close first.")
        return
    cwd = cwd or cfg["default_cwd"]
    if not os.path.isdir(cwd):
        await chat.say(f"❌ Directory not found: {cwd}")
        return
    keys = _codex_session_keys()
    if agent == "codex" and key not in keys and len(keys) >= cfg["codex_limit"]:
        await chat.say(f"Codex session limit reached ({len(keys)}/{cfg['codex_limit']}). "
                       "Close an existing Codex session with /agent close in its chat or topic, "
                       "then retry. This request was not queued.")
        return
    # The capacity check and reservation have no await between them.
    _starting[key] = agent
    try:
        if sess is not None:
            await _close(key)
        sess = AgentSession(key, agent, cwd, cfg)
        sess.chat = chat
        _sessions[key] = sess
        try:
            await sess.ensure_started()
        except (Exception, asyncio.CancelledError) as exc:
            _sessions.pop(key, None)
            _store.pop(key)
            await sess.stop()
            if isinstance(exc, asyncio.CancelledError):
                raise
            logger.exception("agent-bridge: start %s failed", agent)
            await chat.say(f"❌ Could not start {sess.label}: {exc}")
            return
        await chat.say(f"🟢 {sess.label} session started in {cwd} (mode: {sess.mode}).\n"
                       "Plain messages now go to it. /agent close to end, /hermes <text> for Hermes.")
    finally:
        _starting.pop(key, None)
    if prompt:
        await _send_to(sess, prompt)


async def _agent_cmd(key: str, args: str, chat: _Chat, cfg) -> None:
    sub, _, rest = args.strip().partition(" ")
    sub = sub.lower() or "status"
    sess = _get_session(key, cfg)
    if sess is not None:
        sess.chat = chat
    if sub in ("help", "-h", "--help"):
        await chat.say(_HELP)
    elif key in _starting and sub not in ("status", "list"):
        await chat.say("This agent session is still starting. Wait, then retry.")
    elif sub == "list":
        recs = _store.all()
        lines = [f"• {r['agent']} in {r['cwd']} (mode {r.get('mode')})"
                 f"{' — running' if k in _sessions and _sessions[k].running else ''}"
                 + (" ← this chat" if k == key else "")
                 for k, r in recs.items()]
        listing = "Bridged sessions:\n" + "\n".join(lines) if lines else "No bridged sessions."
        await chat.say(listing + "\n" + _codex_capacity(cfg))
    elif sess is None:
        await chat.say("No agent session in this chat. Start one with /cc or /codex.\n"
                       + _codex_capacity(cfg) + "\n\n" + _HELP)
    elif sub == "status":
        state = "working" if sess.busy else ("running" if sess.running else "stopped (resumes on next message)")
        if key in _starting:
            state = "starting"
        await chat.say(f"{sess.label} session in {sess.cwd}\nmode: {sess.mode}\nstate: {state}\n"
                       f"id: {sess.acp_session_id}\n" + _codex_capacity(cfg))
    elif sub == "cancel":
        await sess.cancel()
        await chat.say(f"Cancel sent to {sess.label}.")
    elif sub in ("close", "stop", "end"):
        label = sess.label
        await _close(key)
        await chat.say(f"🔴 {label} session closed. Messages go to Hermes again.")
    elif sub == "mode":
        if not rest.strip():
            if not sess.modes:
                with contextlib.suppress(Exception):
                    await sess.ensure_started()
            await chat.say(f"Current mode: {sess.mode}\nAvailable: {', '.join(sess.modes) or 'unknown'}")
        elif sess.busy:
            await chat.say(f"{sess.label} is working. Change the mode after this turn.")
        else:
            await chat.say(await sess.set_mode(rest.strip()))
    else:
        await chat.say(_HELP)


def _allowed(source, cfg) -> bool:
    users = {str(u) for u in cfg.get("allowed_users") or []}
    if not users or str(getattr(source, "user_id", "") or "") not in users:
        return False
    return cfg.get("allow_groups") or getattr(source, "chat_type", "dm") == "dm"


def _answer_approval(key: str, cmd: str, args: str) -> Optional[str]:
    """Typed /approve or /deny for a bridged request. None = not ours, let Hermes handle it."""
    akey = approvals.key_for(key)
    if not approvals.pending(akey):
        return None
    words = args.lower().split()
    if cmd == "deny":
        n = approvals.resolve(akey, "deny", resolve_all="all" in words)
        return f"Denied {n} request(s)."
    choice = "session" if ("session" in words or "always" in words) else "once"
    n = approvals.resolve(akey, choice, resolve_all="all" in words)
    return f"Approved {n} request(s)" + (" for this session." if choice == "session" else ".")


def on_pre_gateway_dispatch(event=None, gateway=None, **_kw):
    """Runs before Hermes handles a message. Return {"action": "skip"} to take it."""
    global _reaper
    source = getattr(event, "source", None)
    text = (getattr(event, "text", None) or "").strip()
    if source is None or gateway is None or not text:
        return None
    cfg = load_config()
    if not _allowed(source, cfg):
        return None
    adapter = getattr(gateway, "adapters", {}).get(source.platform)
    if adapter is None:
        return None
    key = gateway._session_key_for_source(source)
    thread_id = getattr(source, "thread_id", None)
    reply_to_message_id = None
    if thread_id and getattr(source.platform, "value", source.platform) == "feishu":
        reply_to_message_id = (getattr(event, "message_id", None)
                               or getattr(event, "reply_to_message_id", None))
    chat = _Chat(adapter, source.chat_id, thread_id, reply_to_message_id)
    if _reaper is None or _reaper.done():
        _reaper = asyncio.get_running_loop().create_task(_reap_idle())

    cmd, args = "", text
    if text.startswith("/"):
        head, _, args = text[1:].partition(" ")
        cmd = head.split("@", 1)[0].lower()

    if cmd in _AGENT_ALIASES:
        _spawn(_start(key, _AGENT_ALIASES[cmd], args, chat, cfg))
        return {"action": "skip", "reason": "agent-bridge start"}
    if cmd == "agent":
        _spawn(_agent_cmd(key, args, chat, cfg))
        return {"action": "skip", "reason": "agent-bridge command"}
    if cmd in ("approve", "deny"):
        reply = _answer_approval(key, cmd, args)
        if reply is None:
            return None
        _spawn(chat.say(reply))
        return {"action": "skip", "reason": "agent-bridge approval"}

    sess = _get_session(key, cfg)
    if sess is None:
        return None
    if cmd == "hermes":
        return {"action": "rewrite", "text": args} if args.strip() else {"action": "skip"}
    if cmd:
        return None  # other slash commands stay with Hermes
    sess.chat = chat
    _spawn(_send_to(sess, text))
    return {"action": "skip", "reason": f"agent-bridge -> {sess.agent}"}
