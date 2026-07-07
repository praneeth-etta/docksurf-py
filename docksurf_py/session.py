"""session.py — cross-launch UI state (last active tab, table sort order).

Persisted separately from docker/context.py's state.json: that file is
Docker-connection state owned by the docker/ package, this is app/UI state
that belongs to no resource-fetching concern. Same JSON-file, silent-fail-safe
pattern as context.py's `_load_last_context`/`_save_last_context`.

Callers decide *whether* to persist (see `DockSurfApp`'s `persist_session`
flag) — this module only knows how to read/write the file.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from docksurf_py.constants import TabID

logger = logging.getLogger(__name__)

_SESSION_FILE = Path.home() / ".local/share/docksurf-py/session.json"


@dataclass(slots=True)
class SessionState:
    active_tab: str | None = None
    sort_state: dict[str, tuple[str, bool]] = field(default_factory=dict)


def _valid_tab(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        TabID(value)
    except ValueError:
        return None
    return value


def _valid_sort_entry(value: object) -> tuple[str, bool] | None:
    if (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], str)
        and isinstance(value[1], bool)
    ):
        return (value[0], value[1])
    return None


def load_session() -> SessionState:
    try:
        data = json.loads(_SESSION_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return SessionState()

    active_tab = _valid_tab(data.get("active_tab"))

    sort_state: dict[str, tuple[str, bool]] = {}
    raw_sort = data.get("sort_state")
    if isinstance(raw_sort, dict):
        for tab_value, entry in raw_sort.items():
            if _valid_tab(tab_value) is None:
                continue
            valid_entry = _valid_sort_entry(entry)
            if valid_entry is not None:
                sort_state[tab_value] = valid_entry

    return SessionState(active_tab=active_tab, sort_state=sort_state)


def save_session(state: SessionState) -> None:
    try:
        _SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SESSION_FILE.write_text(
            json.dumps({"active_tab": state.active_tab, "sort_state": state.sort_state})
        )
    except OSError as e:
        logger.warning("Could not persist session state: %s", e)
