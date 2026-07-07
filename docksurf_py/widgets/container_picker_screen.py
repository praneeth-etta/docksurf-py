"""ContainerPickerScreen — pick one container (or generic id/label pair) from a list."""

from rich.markup import escape
from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, OptionList
from textual.widgets.option_list import Option

from docksurf_py.constants import BTN_PICKER_CANCEL_ID, PICKER_LIST_ID


class ContainerPickerScreen(ModalScreen):
    """Pick one container from a list — dismisses with its id or `None`.

    Used for connecting/disconnecting a container to/from a network. The caller
    passes `(container_id, display_label)` pairs (already filtered to the valid
    set for the operation) and builds them into an `OptionList`.
    """

    def __init__(self, title: str, options: list[tuple[str, str]]) -> None:
        super().__init__()
        self._title = title
        self._options = options

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"[b]{escape(self._title)}[/b]", id="picker-title")
            yield OptionList(id=PICKER_LIST_ID)
            yield Button("Cancel", variant="default", id=BTN_PICKER_CANCEL_ID)

    def on_mount(self) -> None:
        option_list = self.query_one(f"#{PICKER_LIST_ID}", OptionList)
        for container_id, label in self._options:
            option_list.add_option(Option(escape(label), id=container_id))
        option_list.focus()

    @on(OptionList.OptionSelected, f"#{PICKER_LIST_ID}")
    def _selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def on_key(self, event) -> None:
        if event.key == "escape":
            event.stop()
            self.dismiss(None)

    @on(Button.Pressed, f"#{BTN_PICKER_CANCEL_ID}")
    def _cancel(self) -> None:
        self.dismiss(None)
