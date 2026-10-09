# Repository instructions

These instructions apply to the whole repository. Keep shared guidance here;
`CLAUDE.md` points to this file.

## Working principles

- 实事求是，实践是检验真理的唯一标准。
- 遇到问题，尝试用第一性原理去分析和解决。
- 如果你不知道或者认为我的说法是错误的，直接指出，我们可以讨论，不用刻意迎合。
- Ground conclusions in the code and observed behavior. Distinguish a hypothesis,
  a passing unit test, and a verified live integration.
- Keep changes focused on the requested task and preserve unrelated working-tree
  changes. Prefer the existing small modules over new abstractions or dependencies.
- Reply or document follow the way ASD-STE100 required, 80% of the way would be good enoug.

## What this project does

`agent-bridge` is a Python plugin for the Hermes gateway. It routes chat messages
to Claude Code or Codex through ACP (Agent Client Protocol) adapters over stdio.
Feishu is the documented chat integration. This is a gateway plugin; the Hermes
terminal CLI does not run its `pre_gateway_dispatch` hook.

Read `README.md` for usage and `docs/design.md` for architecture and known limits.

| Path | Responsibility |
| --- | --- |
| `plugin/agent-bridge/__init__.py`, `plugin.yaml` | Plugin registration and metadata |
| `plugin/agent-bridge/bridge.py` | Dispatch, ACP callbacks, subprocesses, session lifecycle, chat commands |
| `plugin/agent-bridge/approvals.py` | Integration with Hermes's approval queue |
| `plugin/agent-bridge/config.py` | Defaults and runtime configuration loading |
| `plugin/agent-bridge/store.py` | Persistent chat-to-agent session records |
| `adapters/package.json`, `adapters/package-lock.json` | Pinned external Node ACP adapters |
| `config/config.example.json` | Installation template with placeholder user settings |
| `scripts/install.sh` | Copy/symlink installation into a Hermes home and adapter installation |
| `tests/` | Unit tests using the installed Hermes Python environment |

## Behavior to preserve

- The gateway hook is synchronous. Schedule asynchronous work and return promptly:
  `None` lets Hermes handle the message, `skip` takes it, and `rewrite` forwards
  replacement text to Hermes.
- Require an allowed user. An empty `allowed_users` list disables the bridge, and
  group chats are disabled by default. Preserve these defaults.
- Keep one agent session per Hermes session key. Plain text goes to the open
  session; unrelated slash commands pass through. `/hermes <text>` provides a
  one-message escape to Hermes.
- Keep bridge approvals isolated under `agent-bridge:<session key>`. Intercept
  typed `/approve` and `/deny` only when a bridge approval is pending. Timeouts and
  cancellation must never grant permission.
- Keep use of Hermes's private approval internals (`_ApprovalEntry`,
  `_gateway_queues`, `_lock`) in `approvals.py`. Check the installed Hermes source
  when changing that integration; these are not stable public APIs.
- Prevent overlapping turns for a session. Cancellation and shutdown must release
  pending approvals and clean up subprocess resources. Idle shutdown preserves
  the saved session so the next message can resume it.
- Preserve atomic session-store writes and suppress history replay during
  `session/load` so old replies are not sent again.
- Forward only the intended environment variables to adapter processes. Preserve
  the existing locale/proxy handling and explicit per-agent environment overrides.

## Configuration and installation

Runtime configuration and session state live under `AGENT_BRIDGE_HOME`, defaulting
to the Hermes home plus `agent-bridge/` (normally `~/.hermes/agent-bridge/`).
`config/config.json` in this repository is not automatically the active runtime
configuration. Use the example file for portable defaults; do not copy personal
user IDs or machine-specific paths into examples or tests.

The dispatch hook reloads configuration on each message, but an existing
`AgentSession` retains its configuration snapshot. Do not assume every setting
changes an already-open session immediately.

Installation uses `scripts/install.sh` or `scripts/install.sh --link`. It backs up
an existing plugin, runs `npm ci` for the adapters, and creates runtime config from
the example only if none exists. It does not enable the plugin or restart Hermes.
Treat installation and gateway restarts as deployment steps, not unit-test setup.
Keep adapter version changes consistent between `package.json` and its lockfile.

## Verification

Run from the repository root using the Hermes virtual environment:

```bash
~/.hermes/hermes-agent/venv/bin/python -m pytest tests -q
```

For a Hermes checkout elsewhere, set `HERMES_AGENT_DIR` to that checkout and run
its Python environment. The tests import Hermes internals and the Python `acp`
package; a bare system Python may not have the required dependencies.

`tests/conftest.py` loads the hyphenated plugin directory through `importlib` and
sets a temporary `AGENT_BRIDGE_HOME` before importing it. Preserve this isolation.
The suite does not start the gateway or ACP adapters, so a passing run does not
prove that live agent turns or Feishu approval cards work.

For behavior changes, add focused regression coverage where useful and run the
relevant tests. Documentation-only changes need a content/link check. When
changing Hermes/ACP integration or dependency versions, also verify a live turn,
approval, and session resume if the task includes access to that environment;
otherwise report that live verification remains outstanding.

Update `README.md` and `docs/design.md` when changing commands, configuration,
installation, or documented behavior. Current limits include text-only input,
replies delivered at the end of a turn, paths without spaces, and separate Hermes
session keys for Feishu thread replies.
