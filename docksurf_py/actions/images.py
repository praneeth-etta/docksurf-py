"""ImageActionHandler — pull, tag, layer history, mark-all-dangling."""

import logging

from rich.markup import escape
from rich.table import Table
from textual import work
from textual.widgets import DataTable, TabbedContent

from docksurf_py.actions.common import _Base, _display_name
from docksurf_py.constants import DETAIL_PANE_ID, TabID
from docksurf_py.docker import format_size
from docksurf_py.models import CommandResult, Image, ImageLayer
from docksurf_py.widgets import (
    DetailPane,
    LayerHistoryScreen,
    PromptField,
    PromptScreen,
    PullProgressScreen,
)

logger = logging.getLogger(__name__)


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


def _render_layers(layers: list[ImageLayer]) -> Table:
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


class ImageActionHandler(_Base):
    """Image-tab actions: pull (with live progress), layer history, tag, and a
    one-key mark-all-dangling convenience that feeds the existing bulk delete.

    Each action guards the Images tab and notifies a hint elsewhere, mirroring
    how container actions guard on a focused container.
    """

    _IMAGE_TAB_HINT = "Switch to the Images tab and select an image"
    # Image id whose Architecture lookup is currently in flight, if any — see
    # _sync_image_architecture.
    _architecture_target: str | None = None

    def _on_images_tab(self) -> bool:
        return self.query_one(TabbedContent).active == TabID.IMAGES

    def _get_focused_image(self) -> Image | None:
        item = self._get_focused_resource(TabID.IMAGES)
        return item if isinstance(item, Image) else None

    def _sync_image_architecture(self) -> None:
        """Lazily fetch and cache the focused image's Architecture.

        `get_images()` (`docker/fetcher.py`) no longer inspects every image
        eagerly. Architecture is detail-pane only, so it's fetched once per
        image id here instead. Idempotent like
        `LiveStatsController._sync_stats`: no-ops once cached, and dedupes
        against a fetch already in flight for the same image.
        """
        image = self._get_focused_image()
        target = image.id if image is not None else None

        if target is None or target in self._image_architectures:
            return
        if target == self._architecture_target:
            return
        self._architecture_target = target
        self._fetch_image_architecture(target)

    @work(thread=True)
    def _fetch_image_architecture(self, image_id: str) -> None:
        architecture = self.docker.image_architecture(image_id)
        self.call_from_thread(self._apply_image_architecture, image_id, architecture)

    def _apply_image_architecture(
        self, image_id: str, architecture: str | None
    ) -> None:
        self._image_architectures[image_id] = architecture or "unknown"
        if self._architecture_target == image_id:
            self._architecture_target = None
        if not self._on_images_tab():
            return
        entry = self._resource_registry[TabID.IMAGES]
        table = self.query_one(f"#{entry.table_id}", DataTable)
        row = table.cursor_row
        if row is None:
            return
        pane = self.query_one(f"#{DETAIL_PANE_ID}", DetailPane)
        try:
            entry.show_details(pane, row)
        except IndexError:
            pass

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

    # TODO: Refactor to reduce complexity.
    @work(thread=True)
    def _execute_pull(
        self, screen: PullProgressScreen, repository: str, tag: str
    ) -> None:
        stream = self.docker.stream_pull(repository, tag)
        error: str | None = None
        aborted = False
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
                aborted = True
                break
        try:
            self.call_from_thread(
                self._finish_pull, screen, repository, tag, error, aborted
            )
        except Exception:
            pass

    def _finish_pull(
        self,
        screen: PullProgressScreen,
        repository: str,
        tag: str,
        error: str | None,
        aborted: bool = False,
    ) -> None:
        if aborted:
            logger.warning(
                "Pull progress display for %s:%s lost mid-stream — outcome unknown",
                repository,
                tag,
            )
            self.notify(
                f"Lost the progress display for {repository}:{tag} — check "
                "`docker images` for the result",
                severity="warning",
            )
            return
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
        table = _render_layers(layers)
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
