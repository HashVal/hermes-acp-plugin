"""Bridge settings. Override any key in <BRIDGE_HOME>/config.json."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

from hermes_constants import get_hermes_home

from .store import BRIDGE_HOME

logger = logging.getLogger(__name__)

DEFAULTS: Dict[str, Any] = {
    # Platform user ids that may use the bridge. Empty = nobody (fail closed).
    "allowed_users": [],
    # Group chats are off: other members could click the approval buttons.
    "allow_groups": False,
    "codex_limit": 2,           # open Codex sessions, including idle/saved sessions
    "default_cwd": str(Path.home()),
    "adapters_dir": str(get_hermes_home() / "acp-adapters" / "node_modules" / ".bin"),
    "agents": {
        # mode: ACP session mode set after session start.
        #   claude: default | acceptEdits | plan | auto | bypassPermissions
        #   codex:  read-only | workspace-write | agent (AI auto review) | agent-full-access
        "claude": {"command": "claude-agent-acp", "label": "Claude", "mode": "default", "env": {}},
        "codex": {"command": "codex-acp", "label": "Codex", "mode": "workspace-write",
                  "env": {"NO_BROWSER": "1"}},
    },
    "approval_timeout": 1800,   # seconds; no answer = reject
    "progress_interval": 20,    # seconds between progress messages during a turn
    "idle_timeout": 7200,       # seconds; stop the idle adapter process, keep the session
    "max_reply_chars": 24000,
}


def load_config() -> Dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULTS))
    path = BRIDGE_HOME / "config.json"
    if not path.exists():
        return cfg
    try:
        user = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("agent-bridge: ignoring bad %s: %s", path, exc)
        return cfg
    for name, agent in (user.pop("agents", None) or {}).items():
        cfg["agents"].setdefault(name, {}).update(agent)
    cfg.update(user)
    if type(cfg["codex_limit"]) is not int or cfg["codex_limit"] < 1:
        logger.warning("agent-bridge: invalid codex_limit; using default %s",
                       DEFAULTS["codex_limit"])
        cfg["codex_limit"] = DEFAULTS["codex_limit"]
    return cfg
