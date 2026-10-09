# hermes-acp-plugin (agent-bridge)

A Hermes gateway plugin that lets you run Claude Code and Codex sessions from a
chat app (tested with Feishu). Hermes stays the front door: `/cc` or `/codex`
opens a session, plain messages go to that session, and the agent's permission
requests come back as Feishu approval cards.

```
Feishu DM ──> Hermes gateway ──pre_gateway_dispatch──> agent-bridge
                                                          │  ACP (JSON-RPC, stdio)
                                          ┌───────────────┴───────────────┐
                                   claude-agent-acp                   codex-acp
                                   (Claude Code SDK)              (codex app-server)
```

Tested with Hermes v0.19.0, `@agentclientprotocol/claude-agent-acp` 0.88.0,
`@agentclientprotocol/codex-acp` 2.1.1, Python ACP client `agent-client-protocol` 0.9.0
(already in the Hermes venv).

## Chat commands

| Command | Effect |
|---|---|
| `/cc [--cwd DIR] [prompt]` | Start a Claude Code session in DIR, or send to the open one |
| `/codex [--cwd DIR] [prompt]` | Same for Codex |
| plain message | Goes to the open session |
| `/hermes <text>` | Send one message to Hermes while a session is open |
| `/approve`, `/approve session`, `/deny` (or the card buttons) | Answer a permission request |
| `/agent status \| list \| cancel \| close` | Session control |
| `/agent mode [ID]` | Show or set the permission mode |

- Each main chat or topic has its own session. `/cc --cwd OTHER` or switching to
  `/codex` closes the old session in that chat or topic.
- `—cwd` (em dash from the Feishu keyboard) is accepted as `--cwd`.
- Paths with spaces are not supported.
- In Feishu, start `/cc` or `/codex` inside the topic where you want to work.
  Replies and approval cards stay in that topic. A topic has a different Hermes
  session key, so it does not inherit the main chat's agent session or Hermes history.

## Install

```bash
scripts/install.sh            # copy plugin, npm ci the adapters, create config if missing
scripts/install.sh --link     # symlink the plugin to this repo instead (for development)
```

Then add `agent-bridge` to `plugins.enabled` in `~/.hermes/config.yaml` and restart the
gateway (`systemctl --user restart hermes-gateway`). The script does neither.

Installed layout:

| Path | Content |
|---|---|
| `~/.hermes/plugins/agent-bridge/` | plugin code (`plugin/agent-bridge/`) |
| `~/.hermes/acp-adapters/` | ACP adapters, pinned by `adapters/package-lock.json` |
| `~/.hermes/agent-bridge/config.json` | settings (`config/config.example.json`) |
| `~/.hermes/agent-bridge/sessions.json` | chat → agent session map (runtime) |
| `~/.hermes/agent-bridge/logs/` | adapter stderr (runtime) |

## Configuration

`~/.hermes/agent-bridge/config.json` is read on every message; no restart needed.
Defaults are in `plugin/agent-bridge/config.py`.

| Key | Default | Meaning |
|---|---|---|
| `allowed_users` | `[]` | Platform user ids allowed to use the bridge. Empty = nobody. |
| `allow_groups` | `false` | Allow group chats. Off: other members could click approval buttons. |
| `default_cwd` | `$HOME` | Directory when `--cwd` is not given |
| `agents.<name>.mode` | claude `default`, codex `workspace-write` | ACP mode set at session start |
| `agents.<name>.command` | `claude-agent-acp` / `codex-acp` | Adapter binary (relative to `adapters_dir`) |
| `agents.<name>.env` | codex `NO_BROWSER=1` | Extra env for the adapter |
| `approval_timeout` | `1800` | Seconds; an unanswered request is rejected |
| `idle_timeout` | `7200` | Seconds; stop an idle adapter process (the session resumes later) |
| `progress_interval` | `20` | Seconds between "working…" messages |
| `max_reply_chars` | `24000` | Longer replies are cut |

Modes:

- Claude: `default` (ask for edits and commands), `acceptEdits`, `plan`, `auto`, `bypassPermissions`.
- Codex: `read-only`, `workspace-write`, `agent` (an AI reviewer decides most approvals),
  `agent-full-access` (approval policy `never`, sandbox `danger-full-access`: **no approval
  cards at all**, full disk and network access).

See [docs/design.md](docs/design.md) for how it works and the known limits.

## Tests

```bash
~/.hermes/hermes-agent/venv/bin/python -m pytest tests -q
```

The unit tests do not start agent processes or the gateway. Feishu routing tests
use the installed Hermes Feishu adapter with network calls replaced by test stubs.
