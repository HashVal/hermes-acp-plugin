"""Persistent map: Hermes session key -> bridged agent session record."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from hermes_constants import get_hermes_home

BRIDGE_HOME = Path(os.environ.get("AGENT_BRIDGE_HOME") or get_hermes_home() / "agent-bridge")


class SessionStore:
    def __init__(self, path: Path = BRIDGE_HOME / "sessions.json"):
        self._path = path
        self._lock = threading.Lock()
        self._data: Dict[str, Dict[str, Any]] = {}
        try:
            self._data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass
        except Exception:
            # Keep the bad file for inspection and start empty.
            path.replace(path.with_suffix(".corrupt"))

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            rec = self._data.get(key)
            return dict(rec) if rec else None

    def all(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in self._data.items()}

    def put(self, key: str, rec: Dict[str, Any]) -> None:
        with self._lock:
            self._data[key] = dict(rec)
            self._flush()

    def pop(self, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            rec = self._data.pop(key, None)
            self._flush()
            return rec

    def _flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._path)
