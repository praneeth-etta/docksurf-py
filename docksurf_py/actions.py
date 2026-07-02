"""
actions.py — Container management and resource deletion mixins.

ContainerActionHandler and ResourceDeletionHandler are mixin classes
that compose into DockSurfApp via Python MRO.
"""

import logging
import subprocess
from typing import Callable

from rich.markup import escape
from textual import on, work
from textual.widgets import TabbedContent

from docksurf_py.constants import DETAIL_PANE_ID, LOG_PANE_ID, TabID
from docksurf_py.models import Container
from docksurf_py.widgets import ConfirmDialog, DetailPane, LogPane

logger = logging.getLogger(__name__)


class ContainerActionHandler:
    """Start, stop, restart, exec, and log actions scoped to containers."""

    _CONTAINER_TAB_HINT = "Switch to the Containers tab and select a container"

    def _run_on_focused_container(
        self,
        command: Callable[[str], tuple[bool, str]],
        success_msg: Callable[[Container], str],
        guard: Callable[[Container], str | None] = lambda _: None,
    ) -> None:
        c = self._get_focused_container()
        if c is None:
            self.notify(self._CONTAINER_TAB_HINT, severity="warning")
            return
        if reason := guard(c):
            self.notify(reason, severity="information")
            return
        ok, err = command(c.id)
        if ok:
            msg = success_msg(c)
            logger.info("%s", msg)
            self.notify(msg)
            self.populate_tables()
        else:
            logger.warning("Container action failed on %s: %s", c.name, err)
            self.notify(f"Error: {err}", severity="error")

    def action_stop_container(self) -> None:
        self._run_on_focused_container(
            command=self.docker.stop_container,
            success_msg=lambda c: f"Stopped {escape(c.name)}",
            guard=lambda c: (
                f"{escape(c.name)} is not running" if not c.running else None
            ),
        )

    def action_start_container(self) -> None:
        self._run_on_focused_container(
            command=self.docker.start_container,
            success_msg=lambda c: f"Started {escape(c.name)}",
            guard=lambda c: (
                f"{escape(c.name)} is already running" if c.running else None
            ),
        )

    def action_restart_container(self) -> None:
        self._run_on_focused_container(
            command=self.docker.restart_container,
            success_msg=lambda c: f"Restarted {escape(c.name)}",
        )

    def action_exec_container(self) -> None:
        c = self._get_focused_container()
        if c is None:
            self.notify(self._CONTAINER_TAB_HINT, severity="warning")
            return
        if not c.running:
            self.notify(f"{escape(c.name)} is not running", severity="warning")
            return
        logger.info("Exec shell in container %s (%s)", c.name, c.id)
        with self.suspend():
            subprocess.run(["docker", "exec", "-it", c.id, "sh"])

    def action_view_logs(self) -> None:
        log_pane = self.query_one(f"#{LOG_PANE_ID}", LogPane)
        if log_pane.display:
            self.action_close_logs()
            return
        c = self._get_focused_container()
        if c is None:
            self.notify(self._CONTAINER_TAB_HINT, severity="warning")
            return
        logger.info("Opening log pane for container %s (%s)", c.name, c.id)
        log_pane.load(c.id, c.name, self.docker.stream_logs)
        self.query_one(f"#{DETAIL_PANE_ID}", DetailPane).display = False
        log_pane.display = True

    def action_close_logs(self) -> None:
        log_pane = self.query_one(f"#{LOG_PANE_ID}", LogPane)
        if not log_pane.display:
            return
        log_pane.stop_follow()
        if log_pane.has_class("expanded"):
            self._set_log_expanded(log_pane, False)
        log_pane.display = False
        self.query_one(f"#{DETAIL_PANE_ID}", DetailPane).display = True

    def action_follow_logs(self) -> None:
        log_pane = self.query_one(f"#{LOG_PANE_ID}", LogPane)
        if not log_pane.display:
            return
        log_pane.toggle_follow()

    def action_toggle_log_expand(self) -> None:
        log_pane = self.query_one(f"#{LOG_PANE_ID}", LogPane)
        if not log_pane.display:
            return
        self._set_log_expanded(log_pane, not log_pane.has_class("expanded"))

    def action_clear_logs(self) -> None:
        log_pane = self.query_one(f"#{LOG_PANE_ID}", LogPane)
        if not log_pane.display:
            return
        log_pane.clear_log()

    def action_toggle_log_search(self) -> None:
        log_pane = self.query_one(f"#{LOG_PANE_ID}", LogPane)
        if not log_pane.display:
            return
        log_pane.toggle_search()

    @on(LogPane.ToggleExpand)
    def on_log_pane_toggle_expand(self) -> None:
        self.action_toggle_log_expand()

    def _set_log_expanded(self, log_pane: LogPane, expanded: bool) -> None:
        self.query_one(TabbedContent).display = not expanded
        log_pane.set_expanded(expanded)


class ResourceDeletionHandler:
    """Confirmation dialogs and dispatched remove calls for all resource types."""

    def _apply_if_confirmed(
        self, confirmed: bool, command_fn, success_msg: str
    ) -> None:
        if not confirmed:
            logger.debug("Deletion cancelled by user")
            return
        ok, err = command_fn()
        if ok:
            logger.info("%s", success_msg)
            self.notify(success_msg)
            self.populate_tables()
        else:
            logger.warning("Delete failed: %s", err)
            self.notify(f"Error: {err}", severity="error")

    @work
    async def action_delete(self) -> None:
        if not self.snapshot:
            return
        active = self.query_one(TabbedContent).active

        if active == TabID.CONTAINERS:
            c = self._get_focused_container()
            if c is None:
                self.notify("No container selected", severity="warning")
                return
            is_running = c.running
            msg = (
                f"Force-remove RUNNING container '{escape(c.name)}'?"
                if is_running
                else f"Remove container '{escape(c.name)}'?"
            )
            confirmed = await self.push_screen_wait(ConfirmDialog(msg))
            self._apply_if_confirmed(
                confirmed,
                lambda: self.docker.remove_container(c.id, force=is_running),
                f"Removed container: {escape(c.name)}",
            )

        elif active == TabID.IMAGES:
            img = self._get_focused_image()
            if img is None:
                self.notify("No image selected", severity="warning")
                return
            in_use = bool(img.used_by)
            img_label = f"{escape(img.repository)}:{escape(img.tag)}"
            msg = (
                f"Force-remove IN-USE image '{img_label}'?"
                if in_use
                else f"Remove image '{img_label}'?"
            )
            confirmed = await self.push_screen_wait(ConfirmDialog(msg))
            self._apply_if_confirmed(
                confirmed,
                lambda: self.docker.remove_image(img.id, force=in_use),
                f"Removed image {img_label}",
            )

        elif active == TabID.VOLUMES:
            vol = self._get_focused_volume()
            if vol is None:
                self.notify("No volume selected", severity="warning")
                return
            if vol.used_by:
                self.notify(
                    f"Volume '{escape(vol.name)}' is in use — stop containers first",
                    severity="warning",
                )
                return
            confirmed = await self.push_screen_wait(
                ConfirmDialog(f"Remove volume '{escape(vol.name)}'?")
            )
            self._apply_if_confirmed(
                confirmed,
                lambda: self.docker.remove_volume(vol.name),
                f"Removed volume {escape(vol.name)}",
            )

        elif active == TabID.NETWORKS:
            net = self._get_focused_network()
            if net is None:
                self.notify("No network selected", severity="warning")
                return
            if net.name in ("bridge", "host", "none"):
                self.notify(
                    f"Cannot remove built-in network '{escape(net.name)}'",
                    severity="warning",
                )
                return
            confirmed = await self.push_screen_wait(
                ConfirmDialog(f"Remove network '{escape(net.name)}'?")
            )
            self._apply_if_confirmed(
                confirmed,
                lambda: self.docker.remove_network(net.name),
                f"Removed network {escape(net.name)}",
            )
