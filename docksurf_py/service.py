"""
service.py — DockerService protocol.

Implemented by `DockerClient`, can be replaced in tests or with other
implementations.
"""

from typing import Protocol

from docksurf_py.connection import ConnectionState
from docksurf_py.docker import EventStream, LogStream, MergedLogStream, StatsStream
from docksurf_py.models import CommandResult, DockerSnapshot, SystemDf


class DockerService(Protocol):
    @property
    def connection(self) -> ConnectionState: ...

    @property
    def is_connected(self) -> bool: ...

    def fetch_snapshot(self) -> DockerSnapshot: ...

    def stream_logs(self, container_id: str) -> LogStream: ...

    def stream_project_logs(self, specs: list[tuple[str, str]]) -> MergedLogStream: ...

    def stream_stats(self, container_id: str) -> StatsStream: ...

    def stream_events(self) -> EventStream: ...

    def system_df(self) -> SystemDf: ...

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
