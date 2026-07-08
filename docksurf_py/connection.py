import os
import platform
import sys
from dataclasses import dataclass
from enum import Enum

# Docker SDK's default host. Duplicated instead of imported to preserve the
# module dependency direction. Used only as a fallback display value if
# context lookup fails.
_DEFAULT_DOCKER_SOCK = (
    "npipe:////./pipe/docker_engine"
    if sys.platform == "win32"
    else "unix:///var/run/docker.sock"
)


class ConnectionStatus(Enum):
    NOT_CONNECTED = "not_connected"
    CONNECTED = "connected"
    DAEMON_UNAVAILABLE = "daemon_unavailable"
    PERMISSION_DENIED = "permission_denied"
    API_ERROR = "api_error"
    NOT_INSTALLED = "not_installed"


@dataclass(slots=True)
class ConnectionState:
    status: ConnectionStatus
    message: str
    hint: str
    context: str
    host: str


def _get_docker_context() -> str:
    if ctx := os.environ.get("DOCKER_CONTEXT"):
        return ctx
    try:
        import docker.context as ctx_mod

        return ctx_mod.ContextAPI.get_current_context().name
    except Exception:
        return "default"


def _get_docker_host() -> str:
    # Mirror the connection precedence in docker._create_sdk_client so the
    # status bar shows the endpoint we actually talk to: DOCKER_HOST wins,
    # otherwise the active context's host, else the default socket.
    if host := os.environ.get("DOCKER_HOST"):
        return host
    try:
        import docker.context as ctx_mod

        ctx = ctx_mod.ContextAPI.get_current_context()
        if ctx and ctx.Host:
            return ctx.Host
    except Exception:
        pass
    return _DEFAULT_DOCKER_SOCK


def _is_wsl() -> bool:
    try:
        return "microsoft" in platform.uname().release.lower()
    except OSError:
        return False


def _permission_denied_hint() -> str:
    """Docker Desktop (Windows/macOS/WSL) has no user/group model for socket
    access — the fixes below are all Linux-daemon-specific and don't apply
    there."""
    if sys.platform == "win32":
        return (
            "Add your Windows user to the 'docker-users' group, then sign "
            "out and back in"
        )
    if sys.platform == "darwin":
        return "Make sure Docker Desktop is fully started, then try again"
    if _is_wsl():
        return (
            "Enable WSL integration for this distro: Docker Desktop → "
            "Settings → Resources → WSL Integration"
        )
    return "Run: sudo usermod -aG docker $USER  (then log out and back in)"


def _daemon_unavailable_hint() -> str:
    """`systemctl`/`journalctl` only make sense against a systemd-managed
    dockerd — Docker Desktop (Windows/macOS/WSL) manages the daemon itself."""
    if sys.platform == "win32":
        return "Start Docker Desktop"
    if sys.platform == "darwin":
        return "Start Docker Desktop"
    if _is_wsl():
        return "Start Docker Desktop on Windows (with WSL integration enabled)"
    return "Start Docker Desktop, or run: sudo systemctl start docker"


def _api_error_hint() -> str:
    if sys.platform in ("win32", "darwin") or _is_wsl():
        return "Check Docker Desktop's logs, or run: docker info"
    return "Check daemon logs: journalctl -u docker  or  docker info"


def _classify_docker_error(exc: Exception) -> ConnectionState:
    err = str(exc).lower()
    context = _get_docker_context()
    host = _get_docker_host()

    if "permission denied" in err:
        return ConnectionState(
            status=ConnectionStatus.PERMISSION_DENIED,
            message="Permission denied — cannot access Docker socket",
            hint=_permission_denied_hint(),
            context=context,
            host=host,
        )
    unavailable_keywords = ("connection refused", "no such file", "cannot connect")
    if any(kw in err for kw in unavailable_keywords):
        return ConnectionState(
            status=ConnectionStatus.DAEMON_UNAVAILABLE,
            message="Docker daemon is not running",
            hint=_daemon_unavailable_hint(),
            context=context,
            host=host,
        )
    return ConnectionState(
        status=ConnectionStatus.API_ERROR,
        message=f"Docker API error: {exc}",
        hint=_api_error_hint(),
        context=context,
        host=host,
    )
