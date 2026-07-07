"""ComposeActionHandler — project-wide lifecycle actions for Docker Compose stacks."""

import logging

from rich.markup import escape
from textual import work

from docksurf_py.actions.common import _PROJECT_HINT, _Base
from docksurf_py.constants import TabID
from docksurf_py.models import CommandResult, ComposeProject
from docksurf_py.widgets import ConfirmDialog

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
