"""
actions.py — Container management and resource deletion mixins.

ContainerActionHandler and ResourceDeletionHandler are mixin classes
that compose into DockSurfApp via Python MRO.
"""

import logging
import shutil
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from rich.markup import escape
from textual import on, work
from textual.widgets import TabbedContent

from docksurf_py.constants import DETAIL_PANE_ID, LOG_PANE_ID
from docksurf_py.models import (
    CommandErrorKind,
    CommandResult,
    Container,
    Image,
    Network,
    Volume,
)
from docksurf_py.widgets import ConfirmDialog, DetailPane, LogPane

if TYPE_CHECKING:
    from docksurf_py.app import AppContext

    _Base = AppContext
else:
    # Real runtime base is `object` — `AppContext` only exists for mypy to
    # check these mixins' bodies against; see app.py's `AppContext` docstring.
    _Base = object

logger = logging.getLogger(__name__)

EXEC_SHELL_CANDIDATES = ("bash", "sh")


def select_exec_shell(
    candidates: tuple[str, ...], probe: Callable[[str], bool]
) -> str | None:
    """Return the first candidate shell for which `probe` succeeds, or None."""
    for shell in candidates:
        if probe(shell):
            return shell
    return None


class ContainerActionHandler(_Base):
    """Start, stop, restart, exec, and log actions scoped to containers."""

    _CONTAINER_TAB_HINT = "Switch to the Containers tab and select a container"

    def _run_on_focused_container(
        self,
        command: Callable[[str], CommandResult],
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

        self._execute_container_action(c, command, success_msg)

    @work(thread=True)
    def _execute_container_action(
        self,
        c: Container,
        command: Callable[[str], CommandResult],
        success_msg: Callable[[Container], str],
    ) -> None:
        result = command(c.id)

        self.call_from_thread(
            self._handle_container_action_result,
            c,
            result,
            success_msg,
        )

    def _handle_container_action_result(
        self,
        c: Container,
        result: CommandResult,
        success_msg: Callable[[Container], str],
    ) -> None:
        if result.ok:
            msg = success_msg(c)
            logger.info("%s", msg)
            self.notify(msg)
            self.start_refresh()
        else:
            logger.warning("Container action failed on %s: %s", c.name, result.message)
            self.notify(f"Error: {result.message}", severity="error")
            if result.kind is CommandErrorKind.NOT_FOUND:
                # Our snapshot is stale — the container is already gone.
                self.start_refresh()

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
        if shutil.which("docker") is None:
            logger.error("Exec aborted — docker CLI not found on PATH")
            self.notify(
                "docker CLI not found on PATH — cannot open exec shell",
                severity="error",
            )
            return

        shell = select_exec_shell(
            EXEC_SHELL_CANDIDATES, lambda sh: self._container_has_shell(c.id, sh)
        )
        if shell is None:
            logger.warning("Exec aborted — no usable shell found in %s", c.id)
            self.notify(
                f"No usable shell ({'/'.join(EXEC_SHELL_CANDIDATES)}) found in "
                f"{escape(c.name)}",
                severity="error",
            )
            return

        logger.info("Exec shell (%s) in container %s (%s)", shell, c.name, c.id)
        with self.suspend():
            result = subprocess.run(["docker", "exec", "-it", c.id, shell])

        if result.returncode != 0:
            logger.warning(
                "Exec session for %s (%s) exited with code %d",
                c.name,
                shell,
                result.returncode,
            )
            self.notify(
                f"Exec session for {escape(c.name)} exited with code "
                f"{result.returncode}",
                severity="warning",
            )

    @staticmethod
    def _container_has_shell(container_id: str, shell: str) -> bool:
        probe = subprocess.run(
            ["docker", "exec", container_id, "which", shell],
            capture_output=True,
        )
        return probe.returncode == 0

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


@dataclass(frozen=True)
class DeletePlan:
    """What to confirm and run to delete one focused resource."""

    confirm_message: str
    command: Callable[[], CommandResult]
    success_message: str


class ResourceDeletionHandler(_Base):
    """Confirmation dialogs and dispatched remove calls for all resource types.

    Per-resource behavior (the confirm message, force-flag logic, and any
    pre-condition guard) lives in the `_plan_*_delete` methods below, wired
    into `self._resource_registry` by `DockSurfApp` — `action_delete` itself
    no longer branches on which tab is active.
    """

    def _apply_if_confirmed(
        self,
        confirmed: bool,
        command_fn: Callable[[], CommandResult],
        success_msg: str,
    ) -> None:
        if not confirmed:
            logger.debug("Deletion cancelled by user")
            return
        result = command_fn()
        if result.ok:
            logger.info("%s", success_msg)
            self.notify(success_msg)
            self.start_refresh()
        else:
            logger.warning("Delete failed: %s", result.message)
            self.notify(f"Error: {result.message}", severity="error")
            if result.kind is CommandErrorKind.NOT_FOUND:
                # Our snapshot is stale — the resource is already gone.
                self.start_refresh()

    def _plan_container_delete(self, c: Container) -> DeletePlan | None:
        is_running = c.running
        msg = (
            f"Force-remove RUNNING container '{escape(c.name)}'?"
            if is_running
            else f"Remove container '{escape(c.name)}'?"
        )
        return DeletePlan(
            confirm_message=msg,
            command=lambda: self.docker.remove_container(c.id, force=is_running),
            success_message=f"Removed container: {escape(c.name)}",
        )

    def _plan_image_delete(self, img: Image) -> DeletePlan | None:
        in_use = bool(img.used_by)
        img_label = f"{escape(img.repository)}:{escape(img.tag)}"
        msg = (
            f"Force-remove IN-USE image '{img_label}'?"
            if in_use
            else f"Remove image '{img_label}'?"
        )
        return DeletePlan(
            confirm_message=msg,
            command=lambda: self.docker.remove_image(img.id, force=in_use),
            success_message=f"Removed image {img_label}",
        )

    def _plan_volume_delete(self, vol: Volume) -> DeletePlan | None:
        if vol.used_by:
            self.notify(
                f"Volume '{escape(vol.name)}' is in use — stop containers first",
                severity="warning",
            )
            return None
        return DeletePlan(
            confirm_message=f"Remove volume '{escape(vol.name)}'?",
            command=lambda: self.docker.remove_volume(vol.name),
            success_message=f"Removed volume {escape(vol.name)}",
        )

    def _plan_network_delete(self, net: Network) -> DeletePlan | None:
        if net.name in ("bridge", "host", "none"):
            self.notify(
                f"Cannot remove built-in network '{escape(net.name)}'",
                severity="warning",
            )
            return None
        return DeletePlan(
            confirm_message=f"Remove network '{escape(net.name)}'?",
            command=lambda: self.docker.remove_network(net.name),
            success_message=f"Removed network {escape(net.name)}",
        )

    @work
    async def action_delete(self) -> None:
        if not self.snapshot:
            return
        active = self.query_one(TabbedContent).active
        entry = self._resource_registry.get(active)
        if entry is None:
            return

        item = self._get_focused_resource(active)
        if item is None:
            self.notify(f"No {entry.label} selected", severity="warning")
            return

        plan = entry.plan_delete(item)
        if plan is None:
            return

        confirmed = await self.push_screen_wait(ConfirmDialog(plan.confirm_message))
        self._apply_if_confirmed(confirmed, plan.command, plan.success_message)
