"""DetailPaneRenderer — formats and pushes resource details into the side pane."""

from docksurf_py.constants import TabID, markup_green, markup_red, markup_yellow
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
    _project_status_markup,
    _status_markup,
)
from docksurf_py.widgets import DetailPane


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

        details = {
            "ID": c.id,
            "Image": c.image_name,
        }
        if c.is_compose:
            details["Project"] = c.compose_project
            details["Service"] = c.compose_service
        details.update(
            {
                "Image SHA": c.image_id,
                "Status": _status_markup(c),
                "Health": _health_markup(c),
                "Uptime": format_uptime(c.started_at),
                "Restarts": str(c.restart_count),
                "Exit Code": "—" if c.running else str(c.exit_code),
                "Created": format_relative_time(c.created),
                "Ports": format_ports(c.ports) if c.ports else "None",
                "Networks": "\n".join(c.networks) if c.networks else "None",
            }
        )
        pane.update_details(
            f"Container: {c.name}",
            details,
            env_text=format_env(c.env, reveal=self._reveal_secrets),
            env_masked=not self._reveal_secrets,
            health_log=_format_health_log(c.health_log),
        )

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
        pane.update_details(f"Project: {project.name}", details)

    def _show_image_details(self, pane: DetailPane, row: int) -> None:
        images = self._current.get(TabID.IMAGES, [])
        if row >= len(images):
            return
        image = images[row]

        if image.used_by:
            status = markup_green("In Use")
        elif image.is_dangling:
            status = markup_red("Dangling (safe to delete)")
        else:
            status = markup_yellow("Unused (not referenced by any container)")

        # Fetched lazily on row-select and cached by
        # ImageActionHandler._sync_image_architecture; "…" until it resolves.
        architecture = self._image_architectures.get(image.id, "…")

        details = {
            "ID": image.id.removeprefix("sha256:")[:12] if image.id else "N/A",
            "Size": format_size(image.size_bytes),
            "Created": format_relative_time(image.created),
            "Architecture": architecture,
            "Used By": "\n".join(image.used_by) if image.used_by else "None",
            "Status": status,
        }
        pane.update_details(f"Image: {image.repository}:{image.tag}", details)

    def _show_volume_details(self, pane: DetailPane, row: int) -> None:
        volumes = self._current.get(TabID.VOLUMES, [])
        if row >= len(volumes):
            return
        volume = volumes[row]

        details = {
            "Mountpoint": volume.mountpoint,
            "Driver": volume.driver,
            "Labels": format_labels(volume.labels) if volume.labels else "None",
            "Used By": (
                "\n".join(volume.used_by)
                if volume.used_by
                else markup_yellow("Orphaned (safe to delete)")
            ),
        }
        # Size is fetched on-demand (`b`); once known it's cached and shown here.
        size = self._volume_sizes.get(volume.name)
        if size is not None:
            details["Size on disk"] = format_size(size)
        pane.update_details(f"Volume: {volume.name}", details)

    def _show_network_details(self, pane: DetailPane, row: int) -> None:
        networks = self._current.get(TabID.NETWORKS, [])
        if row >= len(networks):
            return
        network = networks[row]

        if network.endpoints:
            attached = "\n".join(
                f"{ep.container_name}: {ep.ipv4 or '—'}"
                + (f" / {ep.mac}" if ep.mac else "")
                for ep in network.endpoints
            )
        else:
            attached = "None"
        details = {
            "ID": network.id,
            "Driver": network.driver,
            "Scope": network.scope,
            "Subnet": network.subnet,
            "Gateway": network.gateway,
            "Attached": attached,
        }
        pane.update_details(f"Network: {network.name}", details)
