"""DetailPaneRenderer — formats and pushes resource details into the side pane."""

from rich.markup import escape as _escape

from docksurf_py.constants import (
    STATUS_GREEN,
    STATUS_RED,
    STATUS_YELLOW,
    SafeMarkup,
    TabID,
    markup_green,
    markup_red,
    markup_yellow,
)
from docksurf_py.docker import (
    format_env,
    format_labels,
    format_ports,
    format_relative_time,
    format_size,
    format_uptime,
)
from docksurf_py.models import ComposeProject
from docksurf_py.renderer.common import _Base
from docksurf_py.renderer.table_renderer import (
    _format_health_log,
    _health_markup,
    _project_status_color,
    _project_status_markup,
    _status_color,
    _status_markup,
)
from docksurf_py.topology import network_topology
from docksurf_py.widgets import DetailPane


def _dim(value: object) -> SafeMarkup:
    """Mute a low-signal value (a raw ID/SHA) so it doesn't compete for
    attention with the fields a user actually scans for."""
    return SafeMarkup(f"[dim]{_escape(str(value))}[/]")


def _flag(value: object, *, when: bool, color: str) -> str | SafeMarkup:
    """Highlight `value` in `color` when `when` is True; otherwise plain text —
    used to draw the eye to an abnormal restart count / non-zero exit code."""
    return SafeMarkup(f"[{color}]{_escape(str(value))}[/]") if when else str(value)


class DetailPaneRenderer(_Base):
    """Formats and pushes resource details into the side pane on row highlight."""

    def _show_container_details(self, pane: DetailPane, row: int) -> None:
        items = self._current.get(TabID.CONTAINERS, [])
        if row >= len(items):
            return
        item = items[row]
        if isinstance(item, ComposeProject):
            self._show_project_details(pane, item)
            return
        c = item
        # env/health-log/started-at/restart-count aren't in the list summary.
        # They're fetched lazily on selection, cached by container id, and remain
        # `None` until that fetch succeeds (or fails).
        detail = self._container_details.get(c.id)

        identity = {"ID": _dim(c.id), "Image": c.image_name}
        if c.is_compose:
            identity["Project"] = c.compose_project
            identity["Service"] = c.compose_service
        identity["Image SHA"] = _dim(c.image_id)

        uptime = format_uptime(detail.started_at) if detail else (c.uptime_hint or "—")
        restarts = (
            _flag(
                detail.restart_count, when=detail.restart_count > 0, color=STATUS_YELLOW
            )
            if detail
            else SafeMarkup("[dim]…[/]")
        )
        runtime = {
            "Status": _status_markup(c),
            "Health": _health_markup(c),
            "Uptime": uptime,
            "Restarts": restarts,
            "Exit Code": (
                "—"
                if c.running
                else _flag(c.exit_code, when=c.exit_code != 0, color=STATUS_RED)
            ),
            "Created": format_relative_time(c.created),
        }

        network = {
            "Ports": format_ports(c.ports) if c.ports else "None",
            "Networks": "\n".join(c.networks) if c.networks else "None",
        }

        pane.update_details(
            f"Container: {c.name}",
            {"Identity": identity, "Runtime": runtime, "Network": network},
            env_text=format_env(detail.env, reveal=self._reveal_secrets)
            if detail
            else "…",
            env_masked=not self._reveal_secrets,
            health_log=_format_health_log(detail.health_log) if detail else None,
            border_style=_status_color(c),
        )
        pane.clear_topology()

    def _show_project_details(self, pane: DetailPane, project: ComposeProject) -> None:
        services = "\n".join(
            f"{c.compose_service or c.name}: {'running' if c.running else c.status}"
            for c in project.containers
        )
        details = {
            "Services": _project_status_markup(project),
            "Config File": project.config_files or "—",
            "Working Dir": project.working_dir or "—",
            "Containers": services or "None",
        }
        pane.update_details(
            f"Project: {project.name}",
            {"": details},
            border_style=_project_status_color(project),
        )
        pane.clear_topology()

    def _show_image_details(self, pane: DetailPane, row: int) -> None:
        images = self._current.get(TabID.IMAGES, [])
        if row >= len(images):
            return
        image = images[row]

        if image.used_by:
            status = markup_green("In Use")
            border_style = STATUS_GREEN
        elif image.is_dangling:
            status = markup_red("Dangling (safe to delete)")
            border_style = STATUS_RED
        else:
            status = markup_yellow("Unused (not referenced by any container)")
            border_style = STATUS_YELLOW

        # Fetched lazily on row-select and cached by
        # ImageActionHandler._sync_image_architecture; "…" until it resolves.
        architecture = self._image_architectures.get(image.id, "…")

        identity = {
            "ID": _dim(image.id.removeprefix("sha256:")[:12] if image.id else "N/A"),
            "Architecture": architecture,
        }
        info = {
            "Size": format_size(image.size_bytes),
            "Created": format_relative_time(image.created),
        }
        usage = {
            "Status": status,
            "Used By": "\n".join(image.used_by) if image.used_by else "None",
        }
        pane.update_details(
            f"Image: {image.repository}:{image.tag}",
            {"Identity": identity, "Details": info, "Usage": usage},
            border_style=border_style,
        )
        pane.clear_topology()

    def _show_volume_details(self, pane: DetailPane, row: int) -> None:
        volumes = self._current.get(TabID.VOLUMES, [])
        if row >= len(volumes):
            return
        volume = volumes[row]

        identity = {
            "Mountpoint": volume.mountpoint,
            "Driver": volume.driver,
            "Labels": format_labels(volume.labels) if volume.labels else "None",
        }
        usage = {
            "Used By": (
                "\n".join(volume.used_by)
                if volume.used_by
                else markup_yellow("Orphaned (safe to delete)")
            ),
        }
        # Size is fetched on-demand (`b`); once known it's cached and shown here.
        size = self._volume_sizes.get(volume.name)
        if size is not None:
            usage["Size on disk"] = format_size(size)
        pane.update_details(
            f"Volume: {volume.name}",
            {"Identity": identity, "Usage": usage},
            border_style=STATUS_GREEN if volume.used_by else STATUS_YELLOW,
        )
        pane.clear_topology()

    def _show_network_details(self, pane: DetailPane, row: int) -> None:
        networks = self._current.get(TabID.NETWORKS, [])
        if row >= len(networks):
            return
        network = networks[row]

        identity = {
            "ID": _dim(network.id),
            "Driver": network.driver,
            "Scope": network.scope,
        }
        addressing = {
            "Subnet": network.subnet,
            "Gateway": network.gateway,
        }

        pane.update_details(
            f"Network: {network.name}",
            {"Identity": identity, "Addressing": addressing},
        )
        # The attached-containers list is drawn as the topology diagram below
        # the panel (endpoints joined with snapshot containers), replacing the
        # old plaintext "Attached" section.
        containers = self.snapshot.containers if self.snapshot else []
        pane.update_topology(network_topology(network, containers))
