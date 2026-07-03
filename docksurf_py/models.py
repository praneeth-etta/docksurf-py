from dataclasses import dataclass
from enum import Enum


class CommandErrorKind(Enum):
    """Categorises why a management command failed, so callers can react
    differently (e.g. re-fetch on a stale not-found) instead of pattern
    matching on an error string.
    """

    NOT_FOUND = "not_found"
    IN_USE = "in_use"
    DAEMON_UNREACHABLE = "daemon_unreachable"
    UNKNOWN = "unknown"


@dataclass(slots=True, frozen=True)
class CommandResult:
    ok: bool
    message: str
    kind: CommandErrorKind | None = None

    @classmethod
    def success(cls, message: str = "OK") -> "CommandResult":
        return cls(ok=True, message=message)

    @classmethod
    def failure(
        cls, message: str, kind: CommandErrorKind = CommandErrorKind.UNKNOWN
    ) -> "CommandResult":
        return cls(ok=False, message=message, kind=kind)


@dataclass(slots=True, frozen=True)
class PortBinding:
    """One container-port entry from `NetworkSettings.Ports`.

    `host_ip`/`host_port` are empty when the port isn't published to the
    host.
    """

    container_port: str  # Docker's raw "80/tcp" form
    host_ip: str = ""
    host_port: str = ""


@dataclass(slots=True)
class Container:
    id: str
    name: str
    image_id: str
    image_name: str
    status: str
    state: str
    running: bool
    exit_code: int
    health: str
    ports: list[PortBinding]
    mounts: list[str]
    networks: list[str]
    created: str
    env: list[str]


@dataclass(slots=True)
class Image:
    id: str
    repository: str
    tag: str
    size_bytes: int
    is_dangling: bool
    used_by: list[str]
    created: str
    architecture: str


@dataclass(slots=True)
class Volume:
    name: str
    driver: str
    mountpoint: str
    used_by: list[str]
    labels: dict[str, str]


@dataclass(slots=True)
class Network:
    id: str
    name: str
    driver: str
    subnet: str
    gateway: str
    scope: str
    used_by: list[str]


@dataclass(slots=True)
class DockerSnapshot:
    containers: list[Container]
    images: list[Image]
    volumes: list[Volume]
    networks: list[Network]
