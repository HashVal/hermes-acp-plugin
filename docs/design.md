# agent-bridge design

## Why ACP

The Agent Client Protocol (ACP) gives one client interface for both agents:
`session/new`, `session/prompt` (with streamed `session/update`),
`session/request_permission`, `session/load`, `session/set_mode`, `session/cancel`.
One client in `bridge.py` drives both adapters.

| Requirement | ACP / Hermes part |
|---|---|
| Start a session from chat | `/cc`, `/codex` → `session/new` |
| Results back to chat | `agent_message_chunk` updates, joined and sent at the end of the turn |
| Session stays until closed | `sessions.json` + `session/load` after a restart or idle stop |
| Approvals to chat | `request_permission` → Feishu approval card → selected option |

## Message flow

1. The Hermes gateway calls the `pre_gateway_dispatch` hook for each user message
   (`gateway/run.py`). The hook is synchronous, so the bridge starts a background task
   and returns `{"action": "skip"}` at once. Hermes then does not answer the message.
2. Messages that the bridge does not take return `None`, and Hermes handles them as
   before: other users, group chats, chats with no open session, and slash commands
   other than the bridge's.
3. `/hermes <text>` returns `{"action": "rewrite"}`, so Hermes answers that one message.
4. A turn that is still running blocks a new message; the user gets "still working".
5. Feishu topic replies carry both `thread_id` and the inbound `message_id` as
   `reply_to_message_id` metadata. If the inbound message ID is absent, use the
   event's parent/reply message ID. The Feishu adapter uses that anchor to call
   the reply API with `reply_in_thread=true`, including for approval cards.
   A topic ID alone does not provide a message to reply to. Failed send results
   are logged even when the adapter returns an error without raising an exception.

## Approvals

The Feishu adapter already has approval cards (`send_exec_approval`). When a button is
clicked it calls `tools.approval.resolve_gateway_approval(session_key, choice)`, which
wakes the waiting `_ApprovalEntry` in `_gateway_queues`.

`approvals.py` puts its own entry in that queue under the key
`agent-bridge:<hermes session key>`. Because the key is different, a Hermes agent
run in the same chat cannot answer or release a bridge request, and the reverse.
Typed `/approve` and `/deny` are taken by the bridge only when a bridge request is
waiting for that chat.

Choice → ACP option: `once` → `allow_once`; `session`/`always` → `allow_always`;
`deny` → `reject_once`; timeout or cancel → `cancelled`.

**Risk:** `_ApprovalEntry`, `_gateway_queues` and `_lock` are private names in
`tools/approval.py`. A Hermes upgrade can change them. All use is in `approvals.py`;
after an upgrade, run the tests and one real approval.

## Sessions and processes

- One adapter process per open session. It starts on the first message and stops after
  `idle_timeout`, on `/agent close`, or when it crashes.
- `sessions.json` keeps `{agent, cwd, acp_session_id, mode}` per Hermes session key.
  After a gateway restart the next message starts the adapter again and calls
  `session/load`. The adapters replay history as updates during load; the bridge drops them.
- The ACP client passes only a short list of env vars to the adapter
  (`HOME, LOGNAME, PATH, SHELL, TERM, USER`). The bridge adds `LANG`, `LC_ALL` and the
  proxy vars, plus `agents.<name>.env`. Claude and Codex read their logins from
  `~/.claude` and `~/.codex` as usual.

## Security

- Fail closed: an empty `allowed_users` disables the bridge.
- DM only by default (`allow_groups: false`).
- Feishu checks that the user who clicks a card is allowed to approve
  (`_is_interactive_operator_authorized`).
- Whoever can send messages as an allowed user can run code on this host through the
  agent. The approval cards are the gate, so the agent mode matters: Codex
  `agent-full-access` and Claude `bypassPermissions` remove that gate.

## Observed adapter behavior (2026-10)

- Claude (`default` mode) asks before each write or command. Options: "Yes",
  "Yes, allow all edits during this session", "No".
- Codex adapter default mode is `agent`: Codex's auto reviewer decides, and most requests
  never reach the client. The bridge sets the configured mode at session start.
- Codex `workspace-write` sent a card for an edit inside the workspace in testing.
  The cause is not confirmed. It may be that the test directory was not a trusted
  project in `~/.codex/config.toml`.
- Codex `read-only`: the first request took more than 180 s to arrive in one test run.

## Known limits

- The plugin works only through the gateway. The Hermes terminal CLI does not run
  `pre_gateway_dispatch`, so `/cc` there does nothing.
- Replies are sent once at the end of a turn; tool progress is sent every
  `progress_interval` seconds. No streaming card.
- No path restriction for `--cwd`, and no spaces in paths.
- Feishu topics have separate session keys. Start an agent inside each topic;
  the bridge does not copy agent sessions or Hermes conversation history from
  the main chat into a topic.
- Images and files in messages are not forwarded; only text.
