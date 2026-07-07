"""PruneHandler — `docker system prune`-family cleanup."""

import logging
from typing import Callable

from textual import work

from docksurf_py.actions.common import _Base
from docksurf_py.models import CommandResult
from docksurf_py.widgets import ConfirmDialog, PruneScreen

logger = logging.getLogger(__name__)

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
        if self.config.confirm_prune:
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
