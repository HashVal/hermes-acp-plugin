"""Unit tests that do not start the ACP adapters."""
import json
import threading
import time
import types
from dataclasses import dataclass
from typing import Optional

import pytest

ME = "ou_test_me"


@dataclass
class Source:
    platform: str = "feishu"
    chat_id: str = "oc_test"
    chat_type: str = "dm"
    user_id: Optional[str] = ME
    thread_id: Optional[str] = None


class Gateway:
    def __init__(self):
        self.adapters = {"feishu": object()}

    def _session_key_for_source(self, source):
        return f"agent:main:{source.platform}:{source.chat_type}:{source.chat_id}"


@pytest.fixture
def cfg(ab, monkeypatch):
    conf = ab.config.load_config()
    conf["allowed_users"] = [ME]
    monkeypatch.setattr(ab.bridge, "load_config", lambda: conf)
    # Record started coroutines instead of running them.
    started = []
    monkeypatch.setattr(ab.bridge, "_spawn", lambda coro: (started.append(coro), coro.close()))
    monkeypatch.setattr(ab.bridge, "_reaper", types.SimpleNamespace(done=lambda: False))
    conf["_started"] = started
    return conf


def dispatch(ab, text, **src):
    event = types.SimpleNamespace(text=text, source=Source(**src))
    return ab.bridge.on_pre_gateway_dispatch(event=event, gateway=Gateway())


@pytest.mark.parametrize("args,expected", [
    ("--cwd ~/x do it", ("~/x", "do it")),
    ("—cwd /tmp hello there", ("/tmp", "hello there")),   # em dash from Feishu
    ("–cwd /tmp", ("/tmp", "")),                         # en dash
    ("just a prompt", (None, "just a prompt")),
    ("--cwd", (None, "--cwd")),
])
def test_parse_start_args(ab, args, expected):
    cwd, prompt = ab.bridge._parse_start_args(args)
    exp_cwd = None if expected[0] is None else __import__("os").path.expanduser(expected[0])
    assert (cwd, prompt) == (exp_cwd, expected[1])


def test_stranger_and_group_pass_through(ab, cfg):
    assert dispatch(ab, "/cc hi", user_id="ou_other") is None
    assert dispatch(ab, "/cc hi", chat_type="group") is None
    assert cfg["_started"] == []


def test_empty_allowlist_fails_closed(ab, cfg):
    cfg["allowed_users"] = []
    assert dispatch(ab, "/cc hi") is None


def test_start_commands_are_taken(ab, cfg):
    for text in ("/cc hi", "/claude hi", "/codex hi", "/agent status"):
        assert dispatch(ab, text)["action"] == "skip"
    assert len(cfg["_started"]) == 4


def test_no_session_plain_text_goes_to_hermes(ab, cfg):
    assert dispatch(ab, "hello hermes") is None


def test_open_session_routing(ab, cfg):
    key = Gateway()._session_key_for_source(Source())
    ab.bridge._sessions[key] = ab.bridge.AgentSession(key, "claude", "/tmp", cfg, "sid-1")
    try:
        assert dispatch(ab, "fix the bug")["action"] == "skip"
        assert dispatch(ab, "/hermes what time") == {"action": "rewrite", "text": "what time"}
        assert dispatch(ab, "/status") is None                # other commands stay with Hermes
        assert dispatch(ab, "/approve") is None               # nothing pending -> Hermes
    finally:
        ab.bridge._sessions.pop(key, None)


def test_approval_queue_roundtrip(ab):
    key = ab.approvals.key_for("agent:main:feishu:dm:oc_q")
    entry = ab.approvals.enqueue(key, {"command": "x"})
    assert ab.approvals.pending(key)
    threading.Timer(0.1, lambda: ab.approvals.resolve(key, "once")).start()
    import asyncio
    assert asyncio.run(ab.approvals.wait(key, entry, 5)) == "once"
    assert not ab.approvals.pending(key)


def test_approval_release_rejects(ab):
    key = ab.approvals.key_for("agent:main:feishu:dm:oc_r")
    entry = ab.approvals.enqueue(key, {"command": "x"})
    threading.Timer(0.1, lambda: ab.approvals.release(key)).start()
    import asyncio
    assert asyncio.run(ab.approvals.wait(key, entry, 5)) is None


def test_approval_timeout_cleans_queue(ab):
    key = ab.approvals.key_for("agent:main:feishu:dm:oc_t")
    entry = ab.approvals.enqueue(key, {"command": "x"})
    import asyncio
    assert asyncio.run(ab.approvals.wait(key, entry, 0.1)) is None
    assert not ab.approvals.pending(key)


def test_typed_approve_for_bridged_request(ab, cfg):
    gkey = Gateway()._session_key_for_source(Source())
    akey = ab.approvals.key_for(gkey)
    ab.approvals.enqueue(akey, {"command": "x"})
    assert dispatch(ab, "/approve session")["action"] == "skip"
    assert not ab.approvals.pending(akey)


def test_store_roundtrip(ab, tmp_path):
    s = ab.store.SessionStore(tmp_path / "s.json")
    s.put("k", {"agent": "codex", "cwd": "/tmp"})
    assert ab.store.SessionStore(tmp_path / "s.json").get("k")["agent"] == "codex"
    assert s.pop("k")["cwd"] == "/tmp"
    assert json.loads((tmp_path / "s.json").read_text()) == {}


def test_config_merge(ab, tmp_path, monkeypatch):
    monkeypatch.setattr(ab.config, "BRIDGE_HOME", tmp_path)
    (tmp_path / "config.json").write_text(json.dumps(
        {"allowed_users": ["u"], "agents": {"codex": {"mode": "read-only"}}}))
    conf = ab.config.load_config()
    assert conf["allowed_users"] == ["u"]
    assert conf["agents"]["codex"]["mode"] == "read-only"
    assert conf["agents"]["codex"]["command"] == "codex-acp"      # default kept
    assert conf["agents"]["claude"]["mode"] == "default"


def test_summarize_tool_call(ab):
    tc = types.SimpleNamespace(title="Edit files", raw_input=None,
                               locations=[types.SimpleNamespace(path="/w/a.txt")],
                               content=[types.SimpleNamespace(type="diff", path="/w/a.txt", new_text="hi")])
    out = ab.bridge._summarize_tool_call(tc)
    assert "Edit files" in out and "files: /w/a.txt" in out and "hi" in out
