"""agent-bridge: drive Claude Code / Codex ACP sessions from gateway chats.

Commands (in an allowed DM):
  /cc [--cwd DIR] <prompt>      start a Claude Code session (or send to it)
  /codex [--cwd DIR] <prompt>   start a Codex session (or send to it)
  /agent status|list|cancel|close|mode <id>
  /hermes <text>                talk to Hermes while a session is open
  /approve [all] | /deny        answer a pending agent permission request
Plain messages go to the open session until /agent close.
"""

from __future__ import annotations

from .bridge import on_pre_gateway_dispatch


def register(ctx) -> None:
    ctx.register_hook("pre_gateway_dispatch", on_pre_gateway_dispatch)
