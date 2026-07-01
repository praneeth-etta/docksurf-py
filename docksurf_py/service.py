"""
service.py — DockerService protocol.

Implemented by `DockerClient`, can be replaced in tests or with other
implementations.
"""

from typing import Protocol

from docksurf_py.connection import ConnectionState
from docksurf_py.docker import LogStream
from docksurf_py.models import CommandResult, DockerSnapshot


class DockerService(Protocol):
    @property
    def connection(self) -> ConnectionState: ...

    @property
    def is_connected(self) -> bool: ...

    def fetch_snapshot(self) -> DockerSnapshot: ...

    def stream_logs(self, container_id: str) -> LogStream: ...

    def stop_container(self, container_id: str) -> CommandResult: ...

    def start_container(self, container_id: str) -> CommandResult: ...

    def restart_container(self, container_id: str) -> CommandResult: ...

    def remove_container(
        self, container_id: str, force: bool = False
    ) -> CommandResult: ...

    def remove_image(self, image_id: str, force: bool = False) -> CommandResult: ...

    def remove_volume(self, volume_name: str) -> CommandResult: ...

    def remove_network(self, network_name: str) -> CommandResult: ...
