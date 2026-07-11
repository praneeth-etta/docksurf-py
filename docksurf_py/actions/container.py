"""ContainerActionHandler — stop/start/restart/exec/log actions for containers."""

import asyncio
import logging
import platform
import re
import shlex
import shutil
import subprocess
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Callable

from rich.markup import escape
from textual import work
from textual.widgets import DataTable, TabbedContent

from docksurf_py.actions.common import _PROJECT_HINT, _Base
from docksurf_py.constants import DETAIL_PANE_ID, LOG_PANE_ID, LogOptions, TabID
from docksurf_py.models import CommandErrorKind, CommandResult, Container, PortBinding
from docksurf_py.paths import DATA_DIR
from docksurf_py.widgets import (
    ContainerPickerScreen,
    DetailPane,
    LogOptionsScreen,
    LogPane,
    PromptField,
    PromptScreen,
)

logger = logging.getLogger(__name__)

EXEC_SHELL_CANDIDATES = ("bash", "sh")


def _write_log_export(name: str, text: str) -> Path:
    """Write a log buffer to the exports subdir of DATA_DIR and return the path."""
    export_dir = DATA_DIR / "exports"
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


def _is_wsl() -> bool:
    try:
        return "microsoft" in platform.uname().release.lower()
    except OSError:
        return False


def _open_in_browser(url: str) -> bool:
    """Best-effort browser launch — stdlib `webbrowser`, not a Docker call.

    `webbrowser.open()` can report success even when nothing actually opened
    (e.g. on Linux it shells out to `gio`/`xdg-open` and doesn't check their
    exit status), and WSL in particular has neither by default. On WSL, shell
    to `explorer.exe` (the standard way to reach the Windows host browser)
    first; fall back to `webbrowser.open()` everywhere else.
    """
    if _is_wsl() and shutil.which("explorer.exe"):
        try:
            # explorer.exe exits non-zero even on success when given a URL —
            # only a launch failure (e.g. missing binary) is a real error.
            subprocess.run(["explorer.exe", url], check=False)
            return True
        except OSError:
            return False
    return webbrowser.open(url)


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

    def _marked_containers_pending(self) -> bool:
        """True only when there are marked containers AND the Containers tab
        is active. Without the tab guard, pressing a bulk verb while on
        another tab would silently act on marks left over from Containers."""
        return bool(self._marked.get(TabID.CONTAINERS)) and (
            self.query_one(TabbedContent).active == TabID.CONTAINERS
        )

    def action_stop_container(self) -> None:
        # Marked containers win over the focused row — bulk stop the set.
        if self._marked_containers_pending():
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
        if self._marked_containers_pending():
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
        if self._marked_containers_pending():
            self._run_bulk_container_verb(
                verb="Restart",
                command=self.docker.restart_container,
                include=lambda c: True,
            )
            return
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

    @work
    async def action_exec_container(self) -> None:
        c = self._exec_preflight()
        if c is None:
            return

        shell = await asyncio.to_thread(
            select_exec_shell,
            EXEC_SHELL_CANDIDATES,
            lambda sh: self._container_has_shell(c.id, sh),
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

    def _set_log_expanded(self, log_pane: LogPane, expanded: bool) -> None:
        self.query_one(TabbedContent).display = not expanded
        log_pane.set_expanded(expanded)

    @work
    async def action_open_port(self) -> None:
        c = self._get_focused_container()
        if c is None:
            self.notify(self._CONTAINER_TAB_HINT, severity="warning")
            return
        published = [p for p in c.ports if p.host_port]
        if not published:
            self.notify(f"{escape(c.name)} has no published ports", severity="warning")
            return
        if len(published) == 1:
            self._open_port_url(published[0])
            return
        # Keyed by index, not host_port — two entries can publish the same
        # host port (e.g. one TCP, one UDP), and OptionList requires unique
        # option ids.
        options = [
            (str(i), f"{p.host_port} → {p.container_port}")
            for i, p in enumerate(published)
        ]
        chosen = await self.push_screen_wait(
            ContainerPickerScreen(f"Open port — {escape(c.name)}", options)
        )
        if chosen is not None:
            self._open_port_url(published[int(chosen)])

    def _open_port_url(self, port: PortBinding) -> None:
        host = "localhost" if port.host_ip in ("", "0.0.0.0", "::") else port.host_ip
        url = f"http://{host}:{port.host_port}"
        if _open_in_browser(url):
            self.notify(f"Opening {url}")
        else:
            self.notify(
                f"Couldn't launch a browser — copy this URL: {url}",
                severity="warning",
                timeout=10,
            )

    def action_toggle_secrets(self) -> None:
        """`R` — reveal or re-mask secret-looking env var values."""
        self._reveal_secrets: bool = not self._reveal_secrets
        if self.query_one(TabbedContent).active != TabID.CONTAINERS:
            return
        entry = self._resource_registry[TabID.CONTAINERS]
        table = self.query_one(f"#{entry.table_id}", DataTable)
        row = table.cursor_row
        if row is None:
            return
        pane = self.query_one(f"#{DETAIL_PANE_ID}", DetailPane)
        try:
            entry.show_details(pane, row)
        except IndexError:
            pass
