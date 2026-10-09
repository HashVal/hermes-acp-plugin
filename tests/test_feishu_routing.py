"""Exercise bridge replies through Hermes's Feishu adapter without network I/O."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def feishu_bridge(ab, monkeypatch, tmp_path):
    from gateway.config import Platform, PlatformConfig
    from gateway.session import SessionSource, build_session_key
    from plugins.platforms.feishu.adapter import FeishuAdapter

    adapter = FeishuAdapter(PlatformConfig())
    requests = []

    def response(ok=True):
        return SimpleNamespace(success=lambda: ok, code=0 if ok else 99992402,
                               msg="field validation failed" if not ok else "ok",
                               data=SimpleNamespace(message_id="om_sent"))

    def create(request):
        requests.append(("create", request))
        return response(request.receive_id_type == "chat_id")

    def reply(request):
        requests.append(("reply", request))
        return response()

    adapter._client = SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(
            message=SimpleNamespace(create=create, reply=reply))))

    async def direct(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(adapter, "_run_blocking", direct)
    conf = ab.config.load_config()
    conf["allowed_users"] = ["ou_test_me"]
    monkeypatch.setattr(ab.bridge, "load_config", lambda: conf)
    monkeypatch.setattr(ab.bridge, "_sessions", {})
    monkeypatch.setattr(ab.bridge, "_store", ab.store.SessionStore(tmp_path / "sessions.json"))
    monkeypatch.setattr(ab.bridge, "_reaper", SimpleNamespace(done=lambda: False))
    monkeypatch.setattr(ab.bridge.AgentSession, "ensure_started", AsyncMock())
    tasks = []
    monkeypatch.setattr(ab.bridge, "_spawn", lambda coro: tasks.append(asyncio.create_task(coro)))
    gateway = SimpleNamespace(adapters={Platform.FEISHU: adapter},
                              _session_key_for_source=build_session_key)

    async def dispatch(thread_id, message_id="om_trigger", parent_id="om_parent", text=None):
        source = SessionSource(platform=Platform.FEISHU, chat_id="oc_test",
                               chat_type="dm", user_id="ou_test_me", thread_id=thread_id)
        event = SimpleNamespace(source=source, message_id=message_id,
                                reply_to_message_id=parent_id,
                                text=text or f"/codex --cwd {tmp_path}")
        result = ab.bridge.on_pre_gateway_dispatch(event=event, gateway=gateway)
        assert result["action"] == "skip"
        await asyncio.gather(*tasks)
        tasks.clear()
        return ab.bridge._sessions[build_session_key(source)]

    return SimpleNamespace(adapter=adapter, requests=requests, dispatch=dispatch)


@pytest.mark.parametrize("message_id,parent_id,expected", [
    ("om_trigger", "om_parent", "om_trigger"),
    (None, "om_parent", "om_parent"),
])
def test_topic_start_replies_to_a_message(feishu_bridge, message_id, parent_id, expected):
    asyncio.run(feishu_bridge.dispatch("omt_topic", message_id, parent_id))
    [(method, request)] = feishu_bridge.requests
    assert method == "reply"
    assert request.message_id == expected
    assert request.request_body.reply_in_thread is True


def test_topic_approval_card_uses_same_reply_anchor(ab, feishu_bridge, monkeypatch):
    monkeypatch.setattr(ab.approvals, "wait", AsyncMock(return_value="once"))

    async def exercise():
        session = await feishu_bridge.dispatch("omt_topic")
        options = [SimpleNamespace(kind="allow_once", option_id="allow")]
        try:
            result = await session.ask_permission(options, SimpleNamespace(title="Run tests"))
            assert result.outcome.option_id == "allow"
        finally:
            ab.approvals.release(session.approval_key)

    asyncio.run(exercise())
    assert len(feishu_bridge.requests) == 2
    for method, request in feishu_bridge.requests:
        assert method == "reply"
        assert request.message_id == "om_trigger"
        assert request.request_body.reply_in_thread is True
    assert feishu_bridge.requests[-1][1].request_body.msg_type == "interactive"


def test_topics_keep_separate_sessions_and_refresh_reply_anchor(ab, feishu_bridge):
    async def exercise():
        main = await feishu_bridge.dispatch(None)
        first = await feishu_bridge.dispatch("omt_first", "om_first")
        second = await feishu_bridge.dispatch("omt_second", "om_second")
        resumed = await feishu_bridge.dispatch("omt_first", "om_followup", text="/agent status")
        assert resumed is first
        assert len({main.key, first.key, second.key}) == 3
        assert len(ab.bridge._sessions) == 3

    asyncio.run(exercise())
    [(method, request), *topic_requests] = feishu_bridge.requests
    assert method == "create"
    assert request.receive_id_type == "chat_id"
    assert request.request_body.receive_id == "oc_test"
    assert len(topic_requests) == 3
    for (method, request), anchor in zip(topic_requests, ("om_first", "om_second", "om_followup")):
        assert method == "reply"
        assert request.message_id == anchor
        assert request.request_body.reply_in_thread is True


def test_failed_send_result_is_logged(ab, caplog):
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(
        success=False, error="[99992402] field validation failed")))
    chat = ab.bridge._Chat(adapter, "oc_test", "omt_topic")
    asyncio.run(chat.say("private reply text"))
    assert "99992402" in caplog.text
    assert "private reply text" not in caplog.text
