"""ComposeActionHandler — project-wide lifecycle actions for Docker Compose stacks."""

import asyncio
import logging

from rich.markup import escape
from textual import work

from docksurf_py.actions.common import _PROJECT_HINT, _Base
from docksurf_py.constants import TabID
from docksurf_py.models import CommandResult, ComposeProject, Container
from docksurf_py.widgets import BuildProgressScreen, ConfirmDialog

logger = logging.getLogger(__name__)


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
        if self.config.confirm_compose_down:
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

    @work
    async def action_rebuild_service(self) -> None:
        """Rebuild one Compose service's image and recreate its container.

        Active on a service container row, streams the build in a modal and on
        success refreshes and opens the freshly-rebuilt container's logs.
        Services with no `build:` section (image-only, e.g. `postgres`) are
        detected and skipped rather than offered a rebuild that can't work.
        """
        c = self._get_focused_container()
        if c is None or not c.is_compose:
            self.notify(
                "Select a Compose service container to rebuild", severity="warning"
            )
            return

        # Detect build-less services off the UI thread; None = couldn't
        # determine (docker missing / config error), in which case we proceed
        # and let the build itself surface any real failure.
        buildable = await asyncio.to_thread(
            self.docker.compose_buildable_services,
            c.compose_project,
            c.compose_config_files,
            c.compose_working_dir,
        )
        if buildable is not None and c.compose_service not in buildable:
            self.notify(
                f"No build defined for service '{escape(c.compose_service)}' — "
                "nothing to rebuild"
            )
            return

        if self.config.confirm_rebuild:
            confirmed = await self.push_screen_wait(
                ConfirmDialog(
                    f"Rebuild service '{escape(c.compose_service)}' in project "
                    f"'{escape(c.compose_project)}'? Rebuilds its image from "
                    "source and recreates the container."
                )
            )
            if not confirmed:
                logger.debug("Rebuild cancelled by user")
                return

        screen = BuildProgressScreen(f"Rebuilding {escape(c.compose_service)}")
        self.push_screen(screen)
        self._execute_rebuild(screen, c)

    @work(thread=True)
    def _execute_rebuild(
        self, screen: BuildProgressScreen, container: Container
    ) -> None:
        stream = self.docker.stream_compose_rebuild(
            container.compose_project,
            container.compose_service,
            container.compose_config_files,
            container.compose_working_dir,
        )
        aborted = False
        for line in stream:
            if not line:
                continue
            try:
                self.call_from_thread(screen.append, escape(line))
            except Exception:
                # The modal was dismissed mid-build — stop pumping; the build
                # keeps running to completion in the background.
                aborted = True
                break
        returncode = stream.returncode if stream.returncode is not None else 1
        try:
            self.call_from_thread(
                self._finish_rebuild, screen, container, returncode, aborted
            )
        except Exception:
            pass

    def _finish_rebuild(
        self,
        screen: BuildProgressScreen,
        container: Container,
        returncode: int,
        aborted: bool,
    ) -> None:
        if aborted:
            logger.warning(
                "Rebuild progress display for %s lost mid-stream — outcome unknown",
                container.compose_service,
            )
            self.notify(
                f"Lost the progress display for {escape(container.compose_service)} "
                "— check the container list for the result",
                severity="warning",
            )
            return
        if returncode != 0:
            screen.append(f"[red]✗ Rebuild failed (exit {returncode})[/]")
            self.notify(
                f"Rebuild failed for {escape(container.compose_service)}",
                severity="error",
            )
            return
        msg = f"Rebuilt {container.compose_service}"
        logger.info("%s", msg)
        screen.append("[green]✓ Rebuild complete[/]")
        self.notify(msg)
        # Success: dismiss the build modal, refresh the tables, and open the
        # recreated container's logs. The container id changes on recreate, so
        # open by its Compose *name* — `docker logs <name>` resolves whichever
        # container currently holds that (stable) name, i.e. the new one.
        screen.dismiss()
        self.start_refresh()
        self._open_container_logs(container.name, container.name)
