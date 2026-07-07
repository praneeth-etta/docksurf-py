"""Docker context resolution and the DockSurf-local context override.

Persist the context selected in DockSurf so it survives restarts. Kept
separate from `~/.docker/config.json`, whose current-context is shared with
the Docker CLI and other terminals.
"""

import json
import logging
import os
from pathlib import Path

import docker

logger = logging.getLogger(__name__)

_DEFAULT_DOCKER_SOCK = "unix:///var/run/docker.sock"

_STATE_FILE = Path.home() / ".local/share/docksurf-py/state.json"


def _load_last_context() -> str | None:
    try:
        data = json.loads(_STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    name = data.get("context")
    return name if isinstance(name, str) and name else None


def _save_last_context(name: str) -> None:
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps({"context": name}))
    except OSError as e:
        logger.warning("Could not persist context selection: %s", e)


def _clear_last_context() -> None:
    try:
        _STATE_FILE.unlink()
    except OSError:
        pass


def _build_sdk_client_for_context(ctx) -> "docker.DockerClient":
    """Build an SDK client scoped to one `docker context` entry."""
    if not ctx.Host or ctx.Host == _DEFAULT_DOCKER_SOCK:
        return docker.from_env()
    kwargs: dict = {"base_url": ctx.Host, "tls": ctx.TLSConfig or False}
    if ctx.Host.startswith("ssh://"):
        kwargs["use_ssh_client"] = True
    return docker.DockerClient(**kwargs)


def _create_sdk_client() -> "docker.DockerClient":
    """Create the SDK client, honoring the active `docker context`.

    `docker.from_env()` only reads `DOCKER_HOST` (falling back to the default
    socket) — it ignores `docker context` entirely. Without this, DockSurf would
    silently talk to a *different daemon* than the user's `docker`/`docker
    compose` CLI whenever a non-default context is active (Docker Desktop
    alongside native docker, colima, a remote context, …), so its resource list
    wouldn't match theirs. Precedence matches the CLI: `DOCKER_HOST` >
    active context > default socket. The default-socket case still goes through
    `from_env()`, so existing setups (and its TLS-env handling) are unchanged.
    """
    if os.environ.get("DOCKER_HOST"):
        return docker.from_env()
    try:
        from docker.context import ContextAPI

        ctx = ContextAPI.get_current_context()
    except Exception:
        ctx = None
    if ctx is not None and ctx.Host and ctx.Host != _DEFAULT_DOCKER_SOCK:
        logger.info("Connecting via docker context %s → %s", ctx.Name, ctx.Host)
        return _build_sdk_client_for_context(ctx)
    return docker.from_env()
