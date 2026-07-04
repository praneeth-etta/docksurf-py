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


@dataclass(slots=True, frozen=True)
class HealthProbe:
    """One entry from `State.Health.Log` — the result of a single healthcheck run.

    `exit_code` is 0 for a passing probe; `output` is the (possibly multi-line)
    stdout/stderr the check emitted.
    """

    start: str
    exit_code: int
    output: str


# Docker Compose writes these labels onto every container it manages.
COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
COMPOSE_SERVICE_LABEL = "com.docker.compose.service"
COMPOSE_CONFIG_FILES_LABEL = "com.docker.compose.project.config_files"
COMPOSE_WORKING_DIR_LABEL = "com.docker.compose.project.working_dir"


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
    labels: dict[str, str]
    started_at: str
    restart_count: int
    health_log: list[HealthProbe]

    @property
    def compose_project(self) -> str:
        return self.labels.get(COMPOSE_PROJECT_LABEL, "")

    @property
    def compose_service(self) -> str:
        return self.labels.get(COMPOSE_SERVICE_LABEL, "")

    @property
    def compose_config_files(self) -> str:
        return self.labels.get(COMPOSE_CONFIG_FILES_LABEL, "")

    @property
    def compose_working_dir(self) -> str:
        return self.labels.get(COMPOSE_WORKING_DIR_LABEL, "")

    @property
    def is_compose(self) -> bool:
        return bool(self.compose_project)


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


@dataclass(slots=True)
class ComposeProject:
    """A group of containers sharing a `com.docker.compose.project` label.

    Acts as a header object in the Containers table's row-backing list — it
    occupies a real row index alongside the `Container`s it groups, so the
    focus/detail resolvers can tell a project header from a service row.
    """

    name: str
    containers: list[Container]
    config_files: str
    working_dir: str

    @property
    def total_count(self) -> int:
        return len(self.containers)

    @property
    def running_count(self) -> int:
        return sum(1 for c in self.containers if c.running)

    @property
    def all_running(self) -> bool:
        return bool(self.containers) and self.running_count == self.total_count


@dataclass(slots=True, frozen=True)
class ContainerStats:
    """One live-usage sample for a single container (from the SDK stats stream).

    Raw values; the renderer turns them into display strings. `cpu_percent` and
    `mem_percent` are 0–100 (or higher for multi-core CPU).
    """

    cpu_percent: float
    mem_used: int
    mem_limit: int
    mem_percent: float
    net_rx: int
    net_tx: int
    blk_read: int
    blk_write: int


@dataclass(slots=True, frozen=True)
class DiskUsageEntry:
    """One row of `docker system df` — a resource category's disk footprint."""

    kind: str  # "Images" / "Containers" / "Local Volumes" / "Build Cache"
    total_count: int
    active_count: int
    size_bytes: int
    reclaimable_bytes: int


@dataclass(slots=True, frozen=True)
class SystemDf:
    """Parsed `docker system df` result: per-category entries plus totals."""

    entries: list[DiskUsageEntry]
    total_size: int
    total_reclaimable: int
