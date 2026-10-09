"""Load the plugin package from plugin/agent-bridge with Hermes on sys.path.

Set HERMES_AGENT_DIR if Hermes is not at ~/.hermes/hermes-agent.
Run with Hermes' venv: ~/.hermes/hermes-agent/venv/bin/python -m pytest tests
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugin" / "agent-bridge"
HERMES = Path(os.environ.get("HERMES_AGENT_DIR", Path.home() / ".hermes" / "hermes-agent"))
sys.path.insert(0, str(HERMES))

# Isolated store; must be set before the plugin is imported.
os.environ["AGENT_BRIDGE_HOME"] = tempfile.mkdtemp(prefix="agent-bridge-test-")

_spec = importlib.util.spec_from_file_location(
    "agent_bridge_ut", PLUGIN / "__init__.py", submodule_search_locations=[str(PLUGIN)])
_mod = importlib.util.module_from_spec(_spec)
sys.modules["agent_bridge_ut"] = _mod
_spec.loader.exec_module(_mod)


@pytest.fixture
def ab():
    """The plugin's submodules."""
    import types
    return types.SimpleNamespace(
        bridge=sys.modules["agent_bridge_ut.bridge"],
        approvals=sys.modules["agent_bridge_ut.approvals"],
        config=sys.modules["agent_bridge_ut.config"],
        store=sys.modules["agent_bridge_ut.store"],
    )
