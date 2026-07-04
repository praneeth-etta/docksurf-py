import os
from dataclasses import dataclass
from enum import Enum


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
    return "unix:///var/run/docker.sock"


def _classify_docker_error(exc: Exception) -> ConnectionState:
    err = str(exc).lower()
    context = _get_docker_context()
    host = _get_docker_host()

    if "permission denied" in err:
        return ConnectionState(
            status=ConnectionStatus.PERMISSION_DENIED,
            message="Permission denied — cannot access Docker socket",
            hint="Run: sudo usermod -aG docker $USER  (then log out and back in)",
            context=context,
            host=host,
        )
    unavailable_keywords = ("connection refused", "no such file", "cannot connect")
    if any(kw in err for kw in unavailable_keywords):
        return ConnectionState(
            status=ConnectionStatus.DAEMON_UNAVAILABLE,
            message="Docker daemon is not running",
            hint="Start Docker Desktop, or run: sudo systemctl start docker",
            context=context,
            host=host,
        )
    return ConnectionState(
        status=ConnectionStatus.API_ERROR,
        message=f"Docker API error: {exc}",
        hint="Check daemon logs: journalctl -u docker  or  docker info",
        context=context,
        host=host,
    )
