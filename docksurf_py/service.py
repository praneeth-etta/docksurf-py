"""
service.py — DockerService protocol.

Implemented by `DockerClient`, can be replaced in tests or with other
implementations.
"""

from typing import Protocol

from docksurf_py.connection import ConnectionState
from docksurf_py.constants import LogOptions
from docksurf_py.docker import (
    EventStream,
    LogStream,
    MergedLogStream,
    PullStream,
    StatsStream,
)
from docksurf_py.models import (
    CommandResult,
    ContainerTop,
    DockerSnapshot,
    ImageLayer,
    SystemDf,
)


class DockerService(Protocol):
    @property
    def connection(self) -> ConnectionState: ...

    @property
    def is_connected(self) -> bool: ...

    def fetch_snapshot(self) -> DockerSnapshot: ...

    def stream_logs(
        self, container_id: str, options: LogOptions | None = None
    ) -> LogStream: ...

    def stream_project_logs(
        self, specs: list[tuple[str, str]], options: LogOptions | None = None
    ) -> MergedLogStream: ...

    def stream_stats(self, container_id: str) -> StatsStream: ...

    def stream_events(self) -> EventStream: ...

    def stream_pull(self, repository: str, tag: str = "latest") -> PullStream: ...

    def system_df(self) -> SystemDf: ...

    def image_history(self, image_id: str) -> list[ImageLayer] | None: ...

    def volume_sizes(self) -> dict[str, int]: ...

    def compose_action(
        self,
        project: str,
        verb: str,
        config_files: str = "",
        working_dir: str = "",
    ) -> CommandResult: ...

    def stop_container(self, container_id: str) -> CommandResult: ...

    def start_container(self, container_id: str) -> CommandResult: ...

    def restart_container(self, container_id: str) -> CommandResult: ...

    def remove_container(
        self, container_id: str, force: bool = False
    ) -> CommandResult: ...

    def remove_image(self, image_id: str, force: bool = False) -> CommandResult: ...

    def remove_volume(self, volume_name: str) -> CommandResult: ...

    def remove_network(self, network_name: str) -> CommandResult: ...

    def tag_image(
        self, image_id: str, repository: str, tag: str = "latest"
    ) -> CommandResult: ...

    def create_volume(
        self,
        name: str,
        driver: str = "local",
        labels: dict[str, str] | None = None,
    ) -> CommandResult: ...

    def create_network(
        self, name: str, driver: str = "bridge", subnet: str = ""
    ) -> CommandResult: ...

    def connect_container(
        self, network_name: str, container_id: str
    ) -> CommandResult: ...

    def disconnect_container(
        self, network_name: str, container_id: str, force: bool = True
    ) -> CommandResult: ...

    def pause_container(self, container_id: str) -> CommandResult: ...

    def unpause_container(self, container_id: str) -> CommandResult: ...

    def kill_container(self, container_id: str) -> CommandResult: ...

    def prune_containers(self) -> CommandResult: ...

    def prune_images(self) -> CommandResult: ...

    def prune_volumes(self) -> CommandResult: ...

    def prune_networks(self) -> CommandResult: ...

    def prune_system(self) -> CommandResult: ...

    def inspect_resource(self, kind: str, ref: str) -> dict | None: ...

    def container_top(self, container_id: str) -> ContainerTop | None: ...

    def container_cp(self, src: str, dst: str) -> CommandResult: ...
