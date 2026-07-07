"""config.py — user-editable settings loaded from ~/.config/docksurf/config.toml.

Leaf module (stdlib only, like models.py): nothing here imports from another
project module, so anything can depend on this without a layering cycle.
"""

import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path.home() / ".config/docksurf/config.toml"

_DEFAULT_TEMPLATE = """\
# DockSurf configuration.
# Delete any key to fall back to its default; delete the whole file to reset
# everything.

[logs]
# Lines to load when opening the log viewer. An integer, or "all".
default_tail = 500
# Only show logs from the last N seconds (0 = no --since window).
default_since_seconds = 0

[confirm]
# Ask before removing a container/image/volume/network (single or bulk).
delete = true
# Ask before `docker compose down` on a project.
compose_down = true
# Ask before any prune target.
prune = true
"""


@dataclass(frozen=True, slots=True)
class Config:
    """DockSurf's user-editable settings. All fields have safe defaults —
    a missing or unreadable config file behaves exactly like an empty one."""

    default_log_tail: int | None = 500
    default_log_since_seconds: int = 0
    confirm_delete: bool = True
    confirm_compose_down: bool = True
    confirm_prune: bool = True


def _coerce_bool(raw: object, default: bool, key: str) -> bool:
    if isinstance(raw, bool):
        return raw
    logger.warning("config: %s must be true/false, got %r — using default", key, raw)
    return default


def _coerce_since(raw: object, default: int, key: str) -> int:
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        return raw
    logger.warning(
        "config: %s must be a non-negative integer, got %r — using default",
        key,
        raw,
    )
    return default


def _coerce_tail(raw: object, default: int | None, key: str) -> int | None:
    if isinstance(raw, str) and raw.strip().lower() == "all":
        return None
    if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
        return raw
    logger.warning(
        'config: %s must be a positive integer or "all", got %r — using default',
        key,
        raw,
    )
    return default


def _ensure_default_file_exists(path: Path) -> None:
    """Scaffold a commented starter file at the conventional default path.

    Only ever called for `DEFAULT_CONFIG_PATH` — an explicit `--config` path
    that doesn't exist is left alone rather than surprise-created.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_DEFAULT_TEMPLATE)
    except OSError as e:
        logger.warning("Could not create default config file at %s: %s", path, e)


def load_config(path: Path | None = None) -> Config:
    """Load `Config` from `path` (default: `DEFAULT_CONFIG_PATH`).

    Missing file at the default path → scaffold a commented starter template,
    then return defaults. Missing file at an explicit path → defaults, no
    scaffold. Malformed TOML or a bad field value → warn and fall back to
    that field's (or the whole file's) default rather than crashing.
    """
    is_default_path = path is None
    target = path or DEFAULT_CONFIG_PATH

    if not target.exists():
        if is_default_path:
            _ensure_default_file_exists(target)
        return Config()

    try:
        with target.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        logger.warning("Could not parse config file %s: %s — using defaults", target, e)
        return Config()

    logs = data.get("logs") or {}
    confirm = data.get("confirm") or {}
    defaults = Config()

    return Config(
        default_log_tail=_coerce_tail(
            logs.get("default_tail", defaults.default_log_tail),
            defaults.default_log_tail,
            "logs.default_tail",
        ),
        default_log_since_seconds=_coerce_since(
            logs.get("default_since_seconds", defaults.default_log_since_seconds),
            defaults.default_log_since_seconds,
            "logs.default_since_seconds",
        ),
        confirm_delete=_coerce_bool(
            confirm.get("delete", defaults.confirm_delete),
            defaults.confirm_delete,
            "confirm.delete",
        ),
        confirm_compose_down=_coerce_bool(
            confirm.get("compose_down", defaults.confirm_compose_down),
            defaults.confirm_compose_down,
            "confirm.compose_down",
        ),
        confirm_prune=_coerce_bool(
            confirm.get("prune", defaults.confirm_prune),
            defaults.confirm_prune,
            "confirm.prune",
        ),
    )
