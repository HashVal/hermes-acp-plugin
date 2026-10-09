"""Agent permission requests through Hermes' gateway approval queue.

The Feishu card buttons and typed /approve call
tools.approval.resolve_gateway_approval(key, choice). We add our own entry to
that queue under a separate key ("agent-bridge:<session key>") so a Hermes
agent run in the same chat cannot release or answer our requests.

This uses private names in tools.approval (_ApprovalEntry, _gateway_queues,
_lock). They are not a public API: if a Hermes upgrade changes them, fix this
module only.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from tools import approval as _ha


def key_for(session_key: str) -> str:
    return f"agent-bridge:{session_key}"


def enqueue(key: str, data: dict):
    entry = _ha._ApprovalEntry(data)
    with _ha._lock:
        _ha._gateway_queues.setdefault(key, []).append(entry)
    return entry


async def wait(key: str, entry, timeout: float) -> Optional[str]:
    """Return "once" | "session" | "always" | "deny", or None on timeout/release."""
    done = await asyncio.to_thread(entry.event.wait, timeout)
    if not done:
        _drop(key, entry)
        return None
    return entry.result


def pending(key: str) -> bool:
    return _ha.has_blocking_approval(key)


def resolve(key: str, choice: str, resolve_all: bool = False) -> int:
    return _ha.resolve_gateway_approval(key, choice, resolve_all=resolve_all)


def release(key: str) -> None:
    """Unblock all waiters for key (their result stays None = rejected)."""
    with _ha._lock:
        entries = _ha._gateway_queues.pop(key, [])
    for entry in entries:
        entry.event.set()


def _drop(key: str, entry) -> None:
    with _ha._lock:
        queue = _ha._gateway_queues.get(key, [])
        if entry in queue:
            queue.remove(entry)
        if not queue:
            _ha._gateway_queues.pop(key, None)
