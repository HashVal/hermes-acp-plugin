"""Session capacity tests with ACP processes and chat sends stubbed out."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def runtime(ab, monkeypatch, tmp_path):
    bridge = ab.bridge
    monkeypatch.setattr(bridge, "_sessions", {})
    monkeypatch.setattr(bridge, "_starting", {})
    monkeypatch.setattr(bridge, "_store", ab.store.SessionStore(tmp_path / "sessions.json"))
    cfg = ab.config.load_config()
    cfg.update(default_cwd=str(tmp_path), codex_limit=2)
    started = []
    chats = {}

    async def ensure_started(sess):
        started.append(sess.key)
        sess.acp_session_id = sess.acp_session_id or f"sid-{sess.key}"
        sess.conn = SimpleNamespace(
            prompt=AsyncMock(return_value=SimpleNamespace(stop_reason="end_turn")),
            cancel=AsyncMock())
        sess.proc = SimpleNamespace(returncode=None)
        bridge._store.put(sess.key, sess.record())

    monkeypatch.setattr(bridge.AgentSession, "ensure_started", ensure_started)

    async def start(key, agent="codex", args=""):
        chat = chats.setdefault(key, SimpleNamespace(say=AsyncMock()))
        await bridge._start(key, agent, args, chat, dict(cfg))
        return bridge._sessions.get(key)

    return SimpleNamespace(bridge=bridge, cfg=cfg, start=start, started=started,
                           chats=chats, ensure_started=ensure_started)


def test_idle_sessions_count_and_claude_does_not(runtime):
    async def exercise():
        first = await runtime.start("first")
        await runtime.start("second")
        assert not first.busy
        assert await runtime.start("third", args="do work") is None
        assert runtime.started == ["first", "second"]
        assert "session limit reached (2/2)" in runtime.chats["third"].say.call_args.args[0]
        assert await runtime.start("claude", agent="claude") is not None
        assert await runtime.start("claude", agent="codex") is not None
        assert runtime.bridge._sessions["claude"].agent == "claude"

    asyncio.run(exercise())


def test_simultaneous_starts_reserve_capacity(runtime, monkeypatch):
    async def exercise():
        release = asyncio.Event()
        starting = []

        async def slow_start(sess):
            starting.append(sess.key)
            await release.wait()
            await runtime.ensure_started(sess)

        monkeypatch.setattr(runtime.bridge.AgentSession, "ensure_started", slow_start)
        tasks = [asyncio.create_task(runtime.start(key)) for key in ("first", "second")]
        await asyncio.sleep(0)
        try:
            assert await runtime.start("third") is None
            assert starting == ["first", "second"]
            # A repeated start must not race the first initialization.
            await runtime.start("first", args="do work")
            assert starting == ["first", "second"]
            assert "still starting" in runtime.chats["first"].say.call_args.args[0]
        finally:
            release.set()
            await asyncio.gather(*tasks)
        assert len(runtime.bridge._store.all()) == 2

    asyncio.run(exercise())


@pytest.mark.parametrize("cancelled", [False, True])
def test_failed_or_cancelled_start_releases_capacity(runtime, monkeypatch, cancelled):
    runtime.cfg["codex_limit"] = 1

    async def fail_start(sess):
        await runtime.ensure_started(sess)  # Also exercise cleanup of a saved record.
        if cancelled:
            raise asyncio.CancelledError()
        raise RuntimeError("adapter startup failed")

    async def exercise():
        monkeypatch.setattr(runtime.bridge.AgentSession, "ensure_started", fail_start)
        if cancelled:
            with pytest.raises(asyncio.CancelledError):
                await runtime.start("first")
        else:
            assert await runtime.start("first") is None
        assert runtime.bridge._store.all() == {}
        monkeypatch.setattr(runtime.bridge.AgentSession, "ensure_started", runtime.ensure_started)
        assert await runtime.start("second") is not None

    asyncio.run(exercise())


def test_saved_sessions_count_after_restart_and_resume_at_limit(runtime, monkeypatch):
    async def exercise():
        first = await runtime.start("first")
        await runtime.start("second")
        await first.stop()  # An idle process stop must not free a session slot.
        assert await runtime.start("third") is None
        runtime.bridge._sessions.clear()
        store = runtime.bridge._store
        monkeypatch.setattr(runtime.bridge, "_store", type(store)(store._path))
        assert await runtime.start("third") is None
        resumed = await runtime.start("first", args="continue")
        assert resumed.acp_session_id == first.acp_session_id
        resumed.conn.prompt.assert_awaited_once()
        assert len(runtime.bridge._store.all()) == 2

    asyncio.run(exercise())


def test_turn_completion_failure_and_cancel_keep_slot(runtime, monkeypatch):
    runtime.cfg["codex_limit"] = 1

    async def exercise():
        sess = await runtime.start("first", args="hello")
        assert not sess.busy
        assert await runtime.start("second") is None
        monkeypatch.setattr(sess, "ensure_started", AsyncMock())
        sess.conn.prompt.side_effect = RuntimeError("stream timeout")
        await runtime.bridge._send_to(sess, "again")
        assert await runtime.start("second") is None
        await sess.cancel()
        assert await runtime.start("second") is None
        await runtime.bridge._close("first")
        assert await runtime.start("second") is not None

    asyncio.run(exercise())


def test_late_turn_completion_does_not_restore_closed_session(runtime, monkeypatch):
    runtime.cfg["codex_limit"] = 1

    async def exercise():
        sess = await runtime.start("first")
        entered, release = asyncio.Event(), asyncio.Event()

        async def prompt(**kwargs):
            entered.set()
            await release.wait()
            return SimpleNamespace(stop_reason="cancelled")

        monkeypatch.setattr(sess, "ensure_started", AsyncMock())
        sess.conn.prompt = prompt
        turn = asyncio.create_task(runtime.bridge._send_to(sess, "hello"))
        await asyncio.wait_for(entered.wait(), 1)
        try:
            await runtime.bridge._close("first")
            assert await runtime.start("second") is not None
        finally:
            release.set()
            await turn
        assert set(runtime.bridge._store.all()) == {"second"}

    asyncio.run(exercise())


def test_directory_replacement_keeps_its_slot(runtime, monkeypatch, tmp_path):
    runtime.cfg["codex_limit"] = 1

    async def exercise():
        old = await runtime.start("first")
        entered, release = asyncio.Event(), asyncio.Event()
        close = runtime.bridge._close

        async def slow_close(key):
            result = await close(key)
            entered.set()
            await release.wait()
            return result

        monkeypatch.setattr(runtime.bridge, "_close", slow_close)
        replacement = asyncio.create_task(runtime.start("first", args=f"--cwd {tmp_path}"))
        await asyncio.wait_for(entered.wait(), 1)
        try:
            assert await runtime.start("second") is None
        finally:
            release.set()
            new = await replacement
        assert new is not old
        assert len(runtime.bridge._store.all()) == 1

    asyncio.run(exercise())


def test_changed_limit_applies_to_new_sessions_and_status(runtime):
    async def exercise():
        await runtime.start("first")
        runtime.cfg["codex_limit"] = 1
        assert await runtime.start("second") is None
        runtime.cfg["codex_limit"] = 2
        assert await runtime.start("second") is not None
        runtime.cfg["codex_limit"] = 1
        assert await runtime.start("third") is None
        assert await runtime.start("first", args="continue") is not None
        for command in ("status", "list"):
            chat = SimpleNamespace(say=AsyncMock())
            await runtime.bridge._agent_cmd("first", command, chat, runtime.cfg)
            assert "Codex sessions: 2/1" in chat.say.call_args.args[0]

    asyncio.run(exercise())


@pytest.mark.parametrize("value,expected", [(1, 1), (3, 3), (0, 2), (-1, 2),
                                           (True, 2), (1.5, 2), ("3", 2), (None, 2)])
def test_codex_limit_config_validation(ab, tmp_path, monkeypatch, value, expected):
    monkeypatch.setattr(ab.config, "BRIDGE_HOME", tmp_path)
    assert ab.config.load_config()["codex_limit"] == 2
    (tmp_path / "config.json").write_text(json.dumps({"codex_limit": value}))
    assert ab.config.load_config()["codex_limit"] == expected
