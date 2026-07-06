"""
actions.py — Container management and resource deletion mixins.

ContainerActionHandler and ResourceDeletionHandler are mixin classes
that compose into DockSurfApp via Python MRO.
"""

import asyncio
import json
import logging
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from rich.markup import escape
from rich.table import Table
from textual import on, work
from textual.coordinate import Coordinate
from textual.widgets import DataTable, TabbedContent

from docksurf_py.constants import (
    DETAIL_PANE_ID,
    LOG_PANE_ID,
    MARK_GLYPH,
    LogOptions,
    TabID,
)
from docksurf_py.docker import format_size
from docksurf_py.models import (
    CommandErrorKind,
    CommandResult,
    ComposeProject,
    Container,
    Image,
    ImageLayer,
    Network,
    Volume,
)
from docksurf_py.widgets import (
    ConfirmDialog,
    ContainerPickerScreen,
    DetailPane,
    InspectScreen,
    LayerHistoryScreen,
    LogOptionsScreen,
    LogPane,
    PromptField,
    PromptScreen,
    PruneScreen,
    PullProgressScreen,
)

if TYPE_CHECKING:
    from docksurf_py.app import AppContext

    _Base = AppContext
else:
    # Real runtime base is `object` — `AppContext` only exists for mypy to
    # check these mixins' bodies against; see app.py's `AppContext` docstring.
    _Base = object

logger = logging.getLogger(__name__)

EXEC_SHELL_CANDIDATES = ("bash", "sh")

_PROJECT_HINT = "Select a Compose project (or one of its containers) first"


def _write_log_export(name: str, text: str) -> Path:
    """Write a log buffer to ~/.local/share/docksurf-py/exports and return the path."""
    export_dir = Path.home() / ".local/share/docksurf-py/exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "logs"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = export_dir / f"{safe}-{stamp}.log"
    path.write_text(text, encoding="utf-8")
    return path


def select_exec_shell(
    candidates: tuple[str, ...], probe: Callable[[str], bool]
) -> str | None:
    """Return the first candidate shell for which `probe` succeeds, or None."""
    for shell in candidates:
        if probe(shell):
            return shell
    return None


def build_exec_argv(
    container_id: str, command: str, user: str = ""
) -> list[str] | None:
    """Build the `docker exec` argv for a custom command/user.

    Returns `None` (caller notifies) if `command` is blank or fails to parse
    as shell tokens (e.g. unbalanced quotes).
    """
    command = command.strip()
    if not command:
        return None
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if not tokens:
        return None

    argv = ["docker", "exec", "-it"]
    if user.strip():
        argv += ["-u", user.strip()]
    argv += [container_id, *tokens]
    return argv


def build_cp_paths(c: Container, src: str, dst: str) -> tuple[str, str] | None:
    """Resolve `PromptScreen` values into a `docker cp` src/dst pair.

    `PromptScreen` is pre-filled with `<name>:` on one side (per the caller);
    exactly one side must carry that prefix — the other is a plain host path.
    The container-name prefix is rewritten to the container id, since that's
    what `docker cp` (and `DockerClient.container_cp`) actually expects.
    Returns `None` (caller notifies) on blank input or if both/neither side
    is container-prefixed.
    """
    src, dst = src.strip(), dst.strip()
    if not src or not dst:
        return None

    prefix = f"{c.name}:"
    src_is_container = src.startswith(prefix)
    dst_is_container = dst.startswith(prefix)
    if src_is_container == dst_is_container:
        return None

    if src_is_container:
        src = f"{c.id}:{src[len(prefix) :]}"
    else:
        dst = f"{c.id}:{dst[len(prefix) :]}"
    return src, dst


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
        # Marked containers win over the focused row — bulk stop the set.
        if self._marked.get(TabID.CONTAINERS):
            self._run_bulk_container_verb(
                verb="Stop",
                command=self.docker.stop_container,
                include=lambda c: c.running,
            )
            return
        # On compose project header -> action on the whole project;
        # on standalone container -> action on the single container.
        if self._focused_is_project_header():
            self.action_compose_stop()
            return
        self._run_on_focused_container(
            command=self.docker.stop_container,
            success_msg=lambda c: f"Stopped {escape(c.name)}",
            guard=lambda c: (
                f"{escape(c.name)} is not running" if not c.running else None
            ),
        )

    def action_start_container(self) -> None:
        if self._marked.get(TabID.CONTAINERS):
            self._run_bulk_container_verb(
                verb="Start",
                command=self.docker.start_container,
                include=lambda c: not c.running,
            )
            return
        if self._focused_is_project_header():
            self.action_compose_start()
            return
        self._run_on_focused_container(
            command=self.docker.start_container,
            success_msg=lambda c: f"Started {escape(c.name)}",
            guard=lambda c: (
                f"{escape(c.name)} is already running" if c.running else None
            ),
        )

    def _run_bulk_container_verb(
        self,
        verb: str,
        command: Callable[[str], CommandResult],
        include: Callable[[Container], bool],
    ) -> None:
        """Build one bulk job per marked container that passes `include`,
        then hand off to `SelectionHandler._run_bulk`. Containers excluded
        by `include` (e.g. already stopped) are silently dropped from the
        batch — same tolerance as the single-container guard messages."""

        def bound(container_id: str) -> Callable[[], CommandResult]:
            return lambda: command(container_id)

        jobs: list[tuple[tuple[str, str], str, Callable[[], CommandResult]]] = []
        for c in self._marked_items(TabID.CONTAINERS):
            key = self._row_key(c)
            if include(c) and key is not None:
                jobs.append((key, c.name, bound(c.id)))

        if not jobs:
            self.notify(
                f"No marked containers eligible to {verb.lower()}", severity="warning"
            )
            self._marked[TabID.CONTAINERS] = set()
            self._rerender_active_table()
            return
        self._run_bulk(TabID.CONTAINERS, verb, jobs)

    def action_restart_container(self) -> None:
        if self._focused_is_project_header():
            self.action_compose_restart()
            return
        self._run_on_focused_container(
            command=self.docker.restart_container,
            success_msg=lambda c: f"Restarted {escape(c.name)}",
        )

    def action_pause_container(self) -> None:
        """Toggle pause: paused -> unpause, running -> pause.

        Container-only (no project-header handling, like exec) — pausing a
        whole Compose project isn't a `docker compose` verb.
        """
        c = self._get_focused_container()
        if c is None:
            self.notify(self._CONTAINER_TAB_HINT, severity="warning")
            return
        if c.state == "paused":
            self._run_on_focused_container(
                command=self.docker.unpause_container,
                success_msg=lambda c: f"Resumed {escape(c.name)}",
            )
        elif c.running:
            self._run_on_focused_container(
                command=self.docker.pause_container,
                success_msg=lambda c: f"Paused {escape(c.name)}",
            )
        else:
            self.notify(f"{escape(c.name)} is not running", severity="information")

    def action_kill_container(self) -> None:
        """SIGKILL the focused container — the escape hatch when `stop` hangs
        on its 10s timeout. No confirmation, matching `docker kill`."""
        self._run_on_focused_container(
            command=self.docker.kill_container,
            success_msg=lambda c: f"Killed {escape(c.name)}",
            guard=lambda c: (
                f"{escape(c.name)} is not running" if not c.running else None
            ),
        )

    def _exec_preflight(self) -> Container | None:
        """Shared guards for `e`/`E`: a focused, running container and the
        `docker` CLI on PATH. Notifies and returns `None` on any failure."""
        c = self._get_focused_container()
        if c is None:
            self.notify(self._CONTAINER_TAB_HINT, severity="warning")
            return None
        if not c.running:
            self.notify(f"{escape(c.name)} is not running", severity="warning")
            return None
        if shutil.which("docker") is None:
            logger.error("Exec aborted — docker CLI not found on PATH")
            self.notify(
                "docker CLI not found on PATH — cannot open exec shell",
                severity="error",
            )
            return None
        return c

    def _run_interactive_exec(self, c: Container, argv: list[str]) -> None:
        """Suspend the TUI and run an interactive `docker exec` session."""
        logger.info("Exec argv=%s in container %s (%s)", argv, c.name, c.id)
        with self.suspend():
            result = subprocess.run(argv)

        if result.returncode != 0:
            logger.warning(
                "Exec session for %s exited with code %d", c.name, result.returncode
            )
            self.notify(
                f"Exec session for {escape(c.name)} exited with code "
                f"{result.returncode}",
                severity="warning",
            )

    def action_exec_container(self) -> None:
        c = self._exec_preflight()
        if c is None:
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

        self._run_interactive_exec(c, ["docker", "exec", "-it", c.id, shell])

    @work
    async def action_exec_custom(self) -> None:
        """`E` — prompt for a custom command and optional user before exec'ing.

        Pre-fills the command field with the same auto-detected shell `e`
        would use, so Enter-Enter reproduces `e`'s behavior.
        """
        c = self._exec_preflight()
        if c is None:
            return

        default_shell = await asyncio.to_thread(
            select_exec_shell,
            EXEC_SHELL_CANDIDATES,
            lambda sh: self._container_has_shell(c.id, sh),
        )
        values = await self.push_screen_wait(
            PromptScreen(
                f"Exec in {escape(c.name)}",
                [
                    PromptField("Command", value=default_shell or "sh"),
                    PromptField("User (uid or name, optional)"),
                ],
            )
        )
        if values is None:
            logger.debug("Custom exec cancelled by user")
            return

        command, user = values
        argv = build_exec_argv(c.id, command, user)
        if argv is None:
            self.notify("Enter a command to run", severity="warning")
            return
        self._run_interactive_exec(c, argv)

    @staticmethod
    def _container_has_shell(container_id: str, shell: str) -> bool:
        probe = subprocess.run(
            ["docker", "exec", container_id, "which", shell],
            capture_output=True,
        )
        return probe.returncode == 0

    @work
    async def action_copy_files(self) -> None:
        """`C` — `docker cp` in/out of a container via a path prompt.

        Unlike exec, `docker cp` works on a stopped container too, so there's
        no running guard here — just a focused container.
        """
        c = self._get_focused_container()
        if c is None:
            self.notify(self._CONTAINER_TAB_HINT, severity="warning")
            return

        values = await self.push_screen_wait(
            PromptScreen(
                f"Copy files — {escape(c.name)}",
                [
                    PromptField(
                        f"Source (prefix with '{c.name}:' for the container side)",
                        value=f"{c.name}:",
                    ),
                    PromptField(
                        f"Destination (prefix with '{c.name}:' for the container side)",
                        value=".",
                    ),
                ],
            )
        )
        if values is None:
            logger.debug("Copy files cancelled by user")
            return

        resolved = build_cp_paths(c, values[0], values[1])
        if resolved is None:
            self.notify(
                f"Prefix exactly one side with '{escape(c.name)}:' — the "
                "other is a plain host path",
                severity="warning",
            )
            return

        src, dst = resolved
        self.notify(f"Copying {escape(src)} → {escape(dst)}…")
        self._execute_copy(src, dst)

    @work(thread=True)
    def _execute_copy(self, src: str, dst: str) -> None:
        result = self.docker.container_cp(src, dst)
        self.call_from_thread(self._handle_copy_result, result)

    def _handle_copy_result(self, result: CommandResult) -> None:
        if result.ok:
            logger.info("%s", result.message)
            self.notify(result.message)
        else:
            logger.warning("Copy failed: %s", result.message)
            self.notify(f"Error: {result.message}", severity="error")

    def action_view_logs(self) -> None:
        log_pane = self.query_one(f"#{LOG_PANE_ID}", LogPane)
        if log_pane.display:
            self.action_close_logs()
            return
        # On a project header, open interleaved logs across the whole project.
        if self._focused_is_project_header():
            self._open_project_logs(log_pane)
            return
        c = self._get_focused_container()
        if c is None:
            self.notify(self._CONTAINER_TAB_HINT, severity="warning")
            return
        logger.info("Opening log pane for container %s (%s)", c.name, c.id)
        log_pane.load(c.id, c.name, self.docker.stream_logs)
        self.query_one(f"#{DETAIL_PANE_ID}", DetailPane).display = False
        log_pane.display = True

    def _open_project_logs(self, log_pane: LogPane) -> None:
        project = self._get_focused_project()
        if project is None:
            self.notify(_PROJECT_HINT, severity="warning")
            return
        specs = [(c.compose_service or c.name, c.id) for c in project.containers]
        logger.info(
            "Opening aggregated logs for project %s (%d containers)",
            project.name,
            len(specs),
        )
        log_pane.load(
            project.name,
            f"project: {project.name}",
            lambda _key, opts: self.docker.stream_project_logs(specs, opts),
        )
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

    def action_toggle_timestamps(self) -> None:
        log_pane = self.query_one(f"#{LOG_PANE_ID}", LogPane)
        if not log_pane.display:
            return
        log_pane.toggle_timestamps()

    def action_toggle_log_wrap(self) -> None:
        log_pane = self.query_one(f"#{LOG_PANE_ID}", LogPane)
        if not log_pane.display:
            return
        log_pane.toggle_wrap()

    def action_next_match(self) -> None:
        log_pane = self.query_one(f"#{LOG_PANE_ID}", LogPane)
        if not log_pane.display:
            return
        log_pane.jump(1)

    def action_prev_match(self) -> None:
        log_pane = self.query_one(f"#{LOG_PANE_ID}", LogPane)
        if not log_pane.display:
            return
        log_pane.jump(-1)

    def action_log_top(self) -> None:
        log_pane = self.query_one(f"#{LOG_PANE_ID}", LogPane)
        if not log_pane.display:
            return
        log_pane.jump_home()

    def action_log_bottom(self) -> None:
        log_pane = self.query_one(f"#{LOG_PANE_ID}", LogPane)
        if not log_pane.display:
            return
        log_pane.jump_end()

    def action_export_logs(self) -> None:
        log_pane = self.query_one(f"#{LOG_PANE_ID}", LogPane)
        if not log_pane.display:
            return
        try:
            path = _write_log_export(log_pane.log_title, log_pane.export_text())
        except OSError as e:
            logger.warning("Log export failed: %s", e)
            self.notify(f"Export failed: {e}", severity="error")
            return
        logger.info("Exported logs to %s", path)
        self.notify(f"Logs exported to {path}")

    def action_log_options(self) -> None:
        log_pane = self.query_one(f"#{LOG_PANE_ID}", LogPane)
        if not log_pane.display:
            return

        def _apply(options: LogOptions | None) -> None:
            if options is not None:
                log_pane.set_options(options)

        self.push_screen(LogOptionsScreen(log_pane.options), _apply)

    @on(LogPane.ToggleExpand)
    def on_log_pane_toggle_expand(self) -> None:
        self.action_toggle_log_expand()

    def _set_log_expanded(self, log_pane: LogPane, expanded: bool) -> None:
        self.query_one(TabbedContent).display = not expanded
        log_pane.set_expanded(expanded)


class ComposeActionHandler(_Base):
    """Project-wide lifecycle actions for Docker Compose stacks.

    Wraps `DockerClient.compose_action` (a sanctioned `docker compose`
    subprocess exception) in the same threaded-worker pattern container actions
    use, so the UI stays responsive and refreshes when the command finishes.
    The project is resolved from whatever is focused — a project header or one
    of its service rows — via `_get_focused_project`.
    """

    def _run_compose(self, verb: str, gerund: str) -> None:
        project = self._get_focused_project()
        if project is None:
            self.notify(_PROJECT_HINT, severity="warning")
            return
        self.notify(f"{gerund} project {escape(project.name)}…")
        self._execute_compose_action(project, verb)

    @work(thread=True)
    def _execute_compose_action(self, project: ComposeProject, verb: str) -> None:
        result = self.docker.compose_action(
            project.name,
            verb,
            config_files=project.config_files,
            working_dir=project.working_dir,
        )
        self.call_from_thread(self._handle_compose_result, project, result)

    def _handle_compose_result(
        self, project: ComposeProject, result: CommandResult
    ) -> None:
        if result.ok:
            logger.info("%s", result.message)
            self.notify(result.message)
            self.start_refresh()
        else:
            logger.warning(
                "Compose action failed on %s: %s", project.name, result.message
            )
            self.notify(f"Error: {result.message}", severity="error")

    def action_compose_up(self) -> None:
        self._run_compose("up", "Bringing up")

    def action_compose_stop(self) -> None:
        self._run_compose("stop", "Stopping")

    def action_compose_start(self) -> None:
        self._run_compose("start", "Starting")

    def action_compose_restart(self) -> None:
        self._run_compose("restart", "Restarting")

    @work
    async def action_compose_down(self) -> None:
        project = self._get_focused_project()
        if project is None:
            self.notify(_PROJECT_HINT, severity="warning")
            return
        confirmed = await self.push_screen_wait(
            ConfirmDialog(
                f"Compose down '{escape(project.name)}'? This stops and removes "
                f"all {project.total_count} container(s) in the project."
            )
        )
        if not confirmed:
            logger.debug("Compose down cancelled by user")
            return
        self.notify(f"Bringing down project {escape(project.name)}…")
        self._execute_compose_action(project, "down")

    def action_toggle_group(self) -> None:
        item = self._get_focused_resource(TabID.CONTAINERS)
        if not isinstance(item, ComposeProject):
            return
        if item.name in self._collapsed_projects:
            self._collapsed_projects.discard(item.name)
        else:
            self._collapsed_projects.add(item.name)
        self._rerender_containers()


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

        if self._marked.get(active):
            await self._bulk_delete(active)
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

    async def _bulk_delete(self, tab_id: TabID) -> None:
        """Delete every marked resource on `tab_id` behind one confirm dialog.

        Reuses each item's existing `plan_delete` for its confirm wording/
        force-flag logic; items whose plan is `None` (in-use volume, built-in
        network) keep their guard-notify from `plan_delete` and are silently
        excluded from the batch.
        """
        entry = self._resource_registry[tab_id]
        jobs: list[tuple[tuple[str, str], str, Callable[[], CommandResult]]] = []
        names: list[str] = []
        for item in self._marked_items(tab_id):
            key = self._row_key(item)
            plan = entry.plan_delete(item)
            if key is None or plan is None:
                continue
            name = _display_name(item)
            jobs.append((key, name, plan.command))
            names.append(name)

        if not jobs:
            self.notify(
                f"No marked {entry.label}s eligible to delete", severity="warning"
            )
            self._marked[tab_id] = set()
            self._rerender_active_table()
            return

        preview = ", ".join(escape(n) for n in names[:8])
        if len(names) > 8:
            preview += f", and {len(names) - 8} more"
        confirmed = await self.push_screen_wait(
            ConfirmDialog(f"Delete {len(names)} {entry.label}(s)? {preview}")
        )
        if not confirmed:
            logger.debug("Bulk delete cancelled by user")
            return
        self._run_bulk(tab_id, "Deleted", jobs)


def _display_name(item: Any) -> str:
    """Human-readable name for an inspect-modal title/notification."""
    if isinstance(item, Image):
        return f"{item.repository}:{item.tag}"
    return getattr(item, "name", str(item))


def _parse_labels(raw: str) -> dict[str, str]:
    """Parse a `k=v,k2=v2` prompt string into a labels dict (blank pairs skipped)."""
    labels: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        key, _, value = pair.partition("=")
        key = key.strip()
        if key:
            labels[key] = value.strip()
    return labels


def _format_pull_chunk(chunk: dict) -> str | None:
    """Format one `docker pull` progress dict into a display line, or None.

    Layer-scoped chunks (`id` present) are prefixed with the short layer id;
    top-level status lines (Pulling from…, Digest…, Status…) are bolded.
    """
    status = chunk.get("status")
    if not status:
        return None
    layer = chunk.get("id")
    if layer:
        return f"[cyan]{escape(str(layer))}[/]  {escape(str(status))}"
    return f"[b]{escape(str(status))}[/]"


def _render_layers(image_ref: str, layers: list[ImageLayer]) -> Table:
    """Build the `docker history` layer table for `LayerHistoryScreen`."""
    table = Table(box=None, expand=True)
    table.add_column("Size", justify="right", style="cyan", width=12)
    table.add_column("Created by")
    for layer in layers:
        command = layer.created_by or "—"
        # docker history prefixes real build steps with "/bin/sh -c #(nop) " for
        # metadata ops and "/bin/sh -c " for RUN — trim the noise for readability.
        command = command.replace("/bin/sh -c #(nop) ", "").replace(
            "/bin/sh -c ", "RUN "
        )
        table.add_row(format_size(layer.size_bytes), command)
    return table


class InspectHandler(_Base):
    """The `docker inspect` escape hatch — full raw JSON for any resource on
    any tab, in a scrollable/searchable modal (see `InspectScreen`)."""

    def action_inspect(self) -> None:
        active = self.query_one(TabbedContent).active
        item = self._get_focused_resource(active)
        if item is None:
            self.notify("Nothing selected to inspect", severity="warning")
            return
        if isinstance(item, ComposeProject):
            self.notify(
                "Select a container within the project to inspect",
                severity="warning",
            )
            return
        key = self._row_key(item)
        if key is None:
            self.notify("Nothing selected to inspect", severity="warning")
            return
        kind, ref = key
        self._execute_inspect(kind, ref, _display_name(item))

    @work(thread=True)
    def _execute_inspect(self, kind: str, ref: str, name: str) -> None:
        attrs = self.docker.inspect_resource(kind, ref)
        if attrs is None:
            self.call_from_thread(
                self.notify,
                f"Could not inspect {kind} {escape(name)}",
                severity="error",
            )
            return
        text = json.dumps(attrs, indent=2, default=str)
        self.call_from_thread(
            self.push_screen, InspectScreen(f"Inspect — {kind}: {name}", text)
        )


# target -> (confirm message, DockerClient method name)
_PRUNE_SPECS: dict[str, tuple[str, str]] = {
    "containers": ("Remove all STOPPED containers?", "prune_containers"),
    "images": ("Remove all dangling (untagged) images?", "prune_images"),
    "volumes": ("Remove all unused anonymous volumes?", "prune_volumes"),
    "networks": ("Remove all unused networks?", "prune_networks"),
    "system": (
        "System prune: removes stopped containers, unused networks, "
        "dangling images, and build cache where supported. Continue?",
        "prune_system",
    ),
}


class PruneHandler(_Base):
    """`docker system prune`-family cleanup: a target picker, then a confirm
    dialog, before anything is actually removed — see `PruneScreen`."""

    @work
    async def action_prune(self) -> None:
        target = await self.push_screen_wait(PruneScreen())
        if target is None:
            logger.debug("Prune cancelled — no target chosen")
            return

        confirm_message, method_name = _PRUNE_SPECS[target]
        confirmed = await self.push_screen_wait(ConfirmDialog(confirm_message))
        if not confirmed:
            logger.debug("Prune cancelled by user")
            return

        self.notify("Pruning…")
        self._execute_prune(method_name)

    @work(thread=True)
    def _execute_prune(self, method_name: str) -> None:
        method: Callable[[], CommandResult] = getattr(self.docker, method_name)
        result = method()
        self.call_from_thread(self._handle_prune_result, result)

    def _handle_prune_result(self, result: CommandResult) -> None:
        if result.ok:
            logger.info("%s", result.message)
            self.notify(result.message)
            self.start_refresh()
        else:
            logger.warning("Prune failed: %s", result.message)
            self.notify(f"Error: {result.message}", severity="error")


class ContextActionHandler(_Base):
    """`D` — list and switch Docker contexts in-app.

    Never calls `docker context use`/`ContextAPI.set_current_context` (that
    writes `~/.docker/config.json`, repointing every other terminal too) —
    `DockerClient.switch_context` builds its own scoped SDK client instead.
    Reuses `ContainerPickerScreen` as a generic `(id, label)` picker (it isn't
    container-specific under the hood) and `ImageActionHandler`'s
    `_handle_write_result` rather than duplicating either.
    """

    @work
    async def action_switch_context(self) -> None:
        contexts = await asyncio.to_thread(self.docker.list_contexts)
        if not contexts:
            self.notify("No Docker contexts found", severity="warning")
            return
        options = [
            (c.name, f"{c.name}{' (current)' if c.is_current else ''}  —  {c.host}")
            for c in contexts
        ]
        name = await self.push_screen_wait(
            ContainerPickerScreen("Switch Docker context", options)
        )
        if name is None:
            logger.debug("Context switch cancelled by user")
            return
        self.notify(f"Switching to context '{escape(name)}'…")
        self._execute_switch_context(name)

    @work(thread=True)
    def _execute_switch_context(self, name: str) -> None:
        result = self.docker.switch_context(name)
        self.call_from_thread(self._handle_write_result, result)


class ImageActionHandler(_Base):
    """Image-tab actions: pull (with live progress), layer history, tag, and a
    one-key mark-all-dangling convenience that feeds the existing bulk delete.

    Each action guards the Images tab and notifies a hint elsewhere, mirroring
    how container actions guard on a focused container.
    """

    _IMAGE_TAB_HINT = "Switch to the Images tab and select an image"

    def _on_images_tab(self) -> bool:
        return self.query_one(TabbedContent).active == TabID.IMAGES

    def _get_focused_image(self) -> Image | None:
        item = self._get_focused_resource(TabID.IMAGES)
        return item if isinstance(item, Image) else None

    def _handle_write_result(self, result: CommandResult) -> None:
        """Shared success/failure handling for the simple create/tag/connect
        writes (also used by Volume/Network handlers via the composed app)."""
        if result.ok:
            logger.info("%s", result.message)
            self.notify(result.message)
            self.start_refresh()
        else:
            logger.warning("Action failed: %s", result.message)
            self.notify(f"Error: {result.message}", severity="error")

    @work
    async def action_pull_image(self) -> None:
        if not self._on_images_tab():
            self.notify(self._IMAGE_TAB_HINT, severity="warning")
            return
        values = await self.push_screen_wait(
            PromptScreen(
                "Pull image",
                [PromptField("Image (name:tag)", placeholder="e.g. alpine:latest")],
            )
        )
        if values is None:
            return
        ref = values[0].strip()
        if not ref:
            self.notify("No image specified", severity="warning")
            return
        repository, _, tag = ref.partition(":")
        tag = tag or "latest"
        screen = PullProgressScreen(f"Pulling {repository}:{tag}")
        self.push_screen(screen)
        self._execute_pull(screen, repository, tag)

    @work(thread=True)
    def _execute_pull(
        self, screen: PullProgressScreen, repository: str, tag: str
    ) -> None:
        stream = self.docker.stream_pull(repository, tag)
        error: str | None = None
        last: dict[str, str] = {}
        for chunk in stream:
            if not isinstance(chunk, dict):
                continue
            if chunk.get("error"):
                error = str(chunk["error"])
                line: str | None = f"[red]{escape(error)}[/]"
            else:
                layer = str(chunk.get("id") or "")
                status = str(chunk.get("status") or "")
                if not status or last.get(layer) == status:
                    continue
                last[layer] = status
                line = _format_pull_chunk(chunk)
            if not line:
                continue
            try:
                self.call_from_thread(screen.append, line)
            except Exception:
                break
        try:
            self.call_from_thread(self._finish_pull, screen, repository, tag, error)
        except Exception:
            pass

    def _finish_pull(
        self,
        screen: PullProgressScreen,
        repository: str,
        tag: str,
        error: str | None,
    ) -> None:
        if error:
            self.notify(f"Pull failed: {error}", severity="error")
        else:
            msg = f"Pulled {repository}:{tag}"
            logger.info("%s", msg)
            screen.append("[green]✓ Pull complete[/]")
            self.notify(msg)
            self.start_refresh()

    @work
    async def action_tag_image(self) -> None:
        if not self._on_images_tab():
            self.notify(self._IMAGE_TAB_HINT, severity="warning")
            return
        image = self._get_focused_image()
        if image is None:
            self.notify(self._IMAGE_TAB_HINT, severity="warning")
            return
        values = await self.push_screen_wait(
            PromptScreen(
                f"Tag {escape(image.repository)}:{escape(image.tag)}",
                [
                    PromptField("Repository", value=image.repository),
                    PromptField("Tag", value="latest"),
                ],
            )
        )
        if values is None:
            return
        repository, tag = (v.strip() for v in values)
        if not repository:
            self.notify("Repository is required", severity="warning")
            return
        self._execute_tag(image.id, repository, tag or "latest")

    @work(thread=True)
    def _execute_tag(self, image_id: str, repository: str, tag: str) -> None:
        result = self.docker.tag_image(image_id, repository, tag)
        self.call_from_thread(self._handle_write_result, result)

    def action_image_history(self) -> None:
        if not self._on_images_tab():
            self.notify(self._IMAGE_TAB_HINT, severity="warning")
            return
        image = self._get_focused_image()
        if image is None:
            self.notify(self._IMAGE_TAB_HINT, severity="warning")
            return
        self._execute_history(image.id, _display_name(image))

    @work(thread=True)
    def _execute_history(self, image_id: str, name: str) -> None:
        layers = self.docker.image_history(image_id)
        if layers is None:
            self.call_from_thread(
                self.notify,
                f"Could not load history for {escape(name)}",
                severity="error",
            )
            return
        table = _render_layers(name, layers)
        self.call_from_thread(
            self.push_screen, LayerHistoryScreen(f"History — {name}", table)
        )

    def action_mark_all_dangling(self) -> None:
        if not self._on_images_tab():
            self.notify(self._IMAGE_TAB_HINT, severity="warning")
            return
        if not self.snapshot:
            return
        dangling = [i for i in self.snapshot.images if i.is_dangling]
        if not dangling:
            self.notify("No dangling images")
            return
        marked = self._marked[TabID.IMAGES]
        for img in dangling:
            key = self._row_key(img)
            if key is not None:
                marked.add(key)
        self._rerender_active_table()
        self.notify(f"Marked {len(dangling)} dangling image(s) — press d to remove")


class VolumeActionHandler(_Base):
    """Volume-tab actions: create, and on-demand per-volume size on disk."""

    _VOLUME_TAB_HINT = "Switch to the Volumes tab"

    def _on_volumes_tab(self) -> bool:
        return self.query_one(TabbedContent).active == TabID.VOLUMES

    @work
    async def action_create_volume(self) -> None:
        if not self._on_volumes_tab():
            self.notify(self._VOLUME_TAB_HINT, severity="warning")
            return
        values = await self.push_screen_wait(
            PromptScreen(
                "Create volume",
                [
                    PromptField("Name", placeholder="leave blank for anonymous"),
                    PromptField("Driver", value="local"),
                    PromptField("Labels (k=v,k=v)"),
                ],
            )
        )
        if values is None:
            return
        name, driver, labels_raw = values
        self._execute_create_volume(
            name.strip(), driver.strip() or "local", _parse_labels(labels_raw)
        )

    @work(thread=True)
    def _execute_create_volume(
        self, name: str, driver: str, labels: dict[str, str]
    ) -> None:
        result = self.docker.create_volume(name, driver, labels)
        self.call_from_thread(self._handle_write_result, result)

    def action_volume_size(self) -> None:
        if not self._on_volumes_tab():
            self.notify(self._VOLUME_TAB_HINT, severity="warning")
            return
        self.notify("Computing volume sizes…")
        self._execute_volume_sizes()

    @work(thread=True)
    def _execute_volume_sizes(self) -> None:
        sizes = self.docker.volume_sizes()
        self.call_from_thread(self._apply_volume_sizes, sizes)

    def _apply_volume_sizes(self, sizes: dict[str, int]) -> None:
        self._volume_sizes = sizes
        if self.query_one(TabbedContent).active != TabID.VOLUMES:
            return
        # Re-render the focused volume's detail so the Size row appears now.
        entry = self._resource_registry[TabID.VOLUMES]
        table = self.query_one(f"#{entry.table_id}", DataTable)
        row = table.cursor_row
        if row is not None:
            pane = self.query_one(f"#{DETAIL_PANE_ID}", DetailPane)
            try:
                entry.show_details(pane, row)
            except IndexError:
                pass
        self.notify("Volume sizes updated")


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


class SelectionHandler(_Base):
    """Multi-select marking and the shared bulk-execution machinery.

    Marks are keyed by `_row_key` (kind, id) tuples in `self._marked[tab]`
    (per-tab sets, initialized in `TableRenderer.setup_tables`) so they
    survive refresh/filter/collapse — `SnapshotManager._apply_snapshot` prunes
    vanished keys and every populate method re-renders the mark glyph on each
    repaint. `ContainerActionHandler`/`ResourceDeletionHandler` build the
    per-domain job lists (what to run, and its guard/plan logic); this mixin
    only knows how to toggle a mark and run a batch of jobs sequentially.
    """

    def action_toggle_mark(self) -> None:
        active = self.query_one(TabbedContent).active
        if self._resource_registry.get(active) is None:
            return
        # A project header has no mark of its own — space still collapses it.
        if self._focused_is_project_header():
            self.action_toggle_group()
            return
        item = self._get_focused_resource(active)
        if item is None:
            return
        key = self._row_key(item)
        if key is None:
            return

        table_id = self._resource_registry[active].table_id
        table = self.query_one(f"#{table_id}", DataTable)
        row = table.cursor_row
        if row is None:
            return
        marked = self._marked[active]
        if key in marked:
            marked.discard(key)
        else:
            marked.add(key)
        table.update_cell_at(Coordinate(row, 0), MARK_GLYPH if key in marked else "")

        # Advance the cursor — mark-and-move, k9s-style rapid selection.
        if row + 1 < table.row_count:
            table.move_cursor(row=row + 1)

    def action_clear_marks(self) -> None:
        active = self.query_one(TabbedContent).active
        if not self._marked.get(active):
            return
        self._marked[active].clear()
        self._rerender_active_table()

    def _marked_items(self, tab_id: TabID) -> list[Any]:
        """Resolve a tab's marked keys back to live objects from the snapshot."""
        if not self.snapshot:
            return []
        entry = self._resource_registry.get(tab_id)
        keys = self._marked.get(tab_id)
        if entry is None or not keys:
            return []
        return [
            item
            for item in entry.snapshot_items(self.snapshot)
            if self._row_key(item) in keys
        ]

    def _run_bulk(
        self,
        tab_id: TabID,
        verb: str,
        jobs: list[tuple[tuple[str, str], str, Callable[[], CommandResult]]],
    ) -> None:
        """Protocol-facing entry point for `ContainerActionHandler`/
        `ResourceDeletionHandler` — dispatches to the threaded worker below.

        Kept separate from `_execute_bulk` because `@work` gives a decorated
        method a Textual-generated wrapper signature at runtime, which mypy
        rejects as an incompatible override of the same name declared in
        `AppContext`. This thin, undecorated method is what the Protocol
        declares instead.
        """
        self._execute_bulk(tab_id, verb, jobs)

    @work(thread=True)
    def _execute_bulk(
        self,
        tab_id: TabID,
        verb: str,
        jobs: list[tuple[tuple[str, str], str, Callable[[], CommandResult]]],
    ) -> None:
        """Run each (key, name, command) job sequentially and summarize.

        Sequential, not parallel — these are direct Docker API/CLI calls, and
        running them one at a time keeps error attribution unambiguous (which
        job failed) without adding a thread pool for what's normally a
        handful of items.
        """
        ok_count = 0
        failures: list[tuple[str, str]] = []
        executed_keys: set[tuple[str, str]] = set()
        for key, name, command in jobs:
            result = command()
            executed_keys.add(key)
            if result.ok:
                ok_count += 1
            else:
                failures.append((name, result.message))
        self.call_from_thread(
            self._handle_bulk_result,
            tab_id,
            executed_keys,
            verb,
            ok_count,
            len(jobs),
            failures,
        )

    def _handle_bulk_result(
        self,
        tab_id: TabID,
        executed_keys: set[tuple[str, str]],
        verb: str,
        ok_count: int,
        total: int,
        failures: list[tuple[str, str]],
    ) -> None:
        if failures:
            shown = ", ".join(f"{escape(n)} ({m})" for n, m in failures[:3])
            more = f", +{len(failures) - 3} more" if len(failures) > 3 else ""
            logger.warning(
                "Bulk %s: %d/%d failed — %s%s", verb, len(failures), total, shown, more
            )
            self.notify(
                f"{verb} {ok_count}/{total} — failed: {shown}{more}",
                severity="error",
            )
        else:
            logger.info("Bulk %s: %d/%d succeeded", verb, ok_count, total)
            self.notify(f"{verb} {ok_count}/{total}")
        self._marked[tab_id] -= executed_keys
        self.start_refresh()
