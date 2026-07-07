"""PruneScreen — prune-target picker."""

from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label

from docksurf_py.constants import BTN_PRUNE_CANCEL_ID, PRUNE_TARGETS


class PruneScreen(ModalScreen):
    """Prune-target picker — dismisses with the chosen target key or `None`.

    One button per target (stopped containers / dangling images / unused
    volumes / unused networks / system-wide), plus Cancel. Digits 1-5 are
    shortcuts matching button order; Escape cancels. This screen only picks
    *what* to prune — the confirm dialog and the actual pruning happen
    afterward, driven by the caller.
    """

    _LABELS = {
        "containers": "1. Stopped containers",
        "images": "2. Dangling images",
        "volumes": "3. Unused volumes",
        "networks": "4. Unused networks",
        "system": "5. System-wide prune",
    }

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("[b]Prune[/b]", id="prune-title")
            for target in PRUNE_TARGETS:
                yield Button(self._LABELS[target], id=f"prune-{target}")
            yield Button("Cancel", variant="default", id=BTN_PRUNE_CANCEL_ID)

    def on_key(self, event) -> None:
        if event.key == "escape":
            event.stop()
            self.dismiss(None)
            return
        if event.key.isdigit():
            index = int(event.key) - 1
            if 0 <= index < len(PRUNE_TARGETS):
                event.stop()
                self.dismiss(PRUNE_TARGETS[index])

    @on(Button.Pressed)
    def _on_button(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == BTN_PRUNE_CANCEL_ID:
            self.dismiss(None)
            return
        target = button_id.removeprefix("prune-")
        if target in PRUNE_TARGETS:
            self.dismiss(target)
