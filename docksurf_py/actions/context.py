"""ContextActionHandler — list and switch Docker contexts in-app."""

import asyncio
import logging

from rich.markup import escape
from textual import work

from docksurf_py.actions.common import _Base
from docksurf_py.widgets import ContainerPickerScreen

logger = logging.getLogger(__name__)


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
