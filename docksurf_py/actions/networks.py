"""NetworkActionHandler — create network, connect/disconnect a container."""

from rich.markup import escape
from textual import work
from textual.widgets import TabbedContent

from docksurf_py.actions.common import _Base
from docksurf_py.constants import TabID
from docksurf_py.models import Network
from docksurf_py.widgets import ContainerPickerScreen, PromptField, PromptScreen


class NetworkActionHandler(_Base):
    """Network-tab actions: create, and connect/disconnect a container."""

    _NETWORK_TAB_HINT = "Switch to the Networks tab and select a network"

    def _get_focused_network(self) -> Network | None:
        item = self._get_focused_resource(TabID.NETWORKS)
        return item if isinstance(item, Network) else None

    def _require_network(self) -> Network | None:
        if self.query_one(TabbedContent).active != TabID.NETWORKS:
            self.notify(self._NETWORK_TAB_HINT, severity="warning")
            return None
        net = self._get_focused_network()
        if net is None:
            self.notify(self._NETWORK_TAB_HINT, severity="warning")
        return net

    @work
    async def action_create_network(self) -> None:
        if self.query_one(TabbedContent).active != TabID.NETWORKS:
            self.notify(self._NETWORK_TAB_HINT, severity="warning")
            return
        values = await self.push_screen_wait(
            PromptScreen(
                "Create network",
                [
                    PromptField("Name"),
                    PromptField("Driver", value="bridge"),
                    PromptField("Subnet (optional)", placeholder="e.g. 172.30.0.0/16"),
                ],
            )
        )
        if values is None:
            return
        name, driver, subnet = (v.strip() for v in values)
        if not name:
            self.notify("Network name is required", severity="warning")
            return
        self._execute_create_network(name, driver or "bridge", subnet)

    @work(thread=True)
    def _execute_create_network(self, name: str, driver: str, subnet: str) -> None:
        result = self.docker.create_network(name, driver, subnet)
        self.call_from_thread(self._handle_write_result, result)

    @work
    async def action_network_connect(self) -> None:
        net = self._require_network()
        if net is None:
            return
        attached = {ep.container_name for ep in net.endpoints}
        containers = self.snapshot.containers if self.snapshot else []
        options = [(c.id, c.name) for c in containers if c.name not in attached]
        if not options:
            self.notify(
                f"All containers already attached to {escape(net.name)}",
                severity="information",
            )
            return
        container = await self.push_screen_wait(
            ContainerPickerScreen(f"Connect a container to {net.name}", options)
        )
        if container is None:
            return
        self._execute_net_membership(net.name, container, connect=True)

    @work
    async def action_network_disconnect(self) -> None:
        net = self._require_network()
        if net is None:
            return
        if not net.endpoints:
            self.notify(
                f"No containers attached to {escape(net.name)}",
                severity="information",
            )
            return
        options = [(ep.container_name, ep.container_name) for ep in net.endpoints]
        container = await self.push_screen_wait(
            ContainerPickerScreen(f"Disconnect a container from {net.name}", options)
        )
        if container is None:
            return
        self._execute_net_membership(net.name, container, connect=False)

    @work(thread=True)
    def _execute_net_membership(
        self, network_name: str, container: str, connect: bool
    ) -> None:
        if connect:
            result = self.docker.connect_container(network_name, container)
        else:
            result = self.docker.disconnect_container(network_name, container)
        self.call_from_thread(self._handle_write_result, result)
