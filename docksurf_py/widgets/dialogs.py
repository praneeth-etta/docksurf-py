"""ConfirmDialog, PromptField, PromptScreen — generic confirm/prompt modals."""

from dataclasses import dataclass

from rich.markup import escape
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label

from docksurf_py.constants import (
    BTN_CANCEL_ID,
    BTN_CONFIRM_ID,
    BTN_PROMPT_CANCEL_ID,
    BTN_PROMPT_OK_ID,
    CONFIRM_FORCE_CHECKBOX_ID,
)


class ConfirmDialog(ModalScreen):
    """A modal confirmation dialog.

    Dismisses with a plain `bool` when `force_default` is left `None` — the
    behavior every existing caller (Compose down, bulk-delete preview,
    prune, etc.) still gets. Passing a `bool` for `force_default` adds a
    "Force" checkbox (pre-checked to that default) and the dialog instead
    dismisses with `(confirmed, force)`.
    """

    def __init__(self, message: str, force_default: bool | None = None) -> None:
        super().__init__()
        self._message = message
        self._force_default = force_default

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self._message)
            if self._force_default is not None:
                yield Checkbox(
                    "Force",
                    value=self._force_default,
                    id=CONFIRM_FORCE_CHECKBOX_ID,
                )
            with Horizontal():
                yield Button("Confirm", variant="error", id=BTN_CONFIRM_ID)
                yield Button("Cancel", variant="default", id=BTN_CANCEL_ID)

    def _result(self, confirmed: bool) -> bool | tuple[bool, bool]:
        if self._force_default is None:
            return confirmed
        force = self.query_one(f"#{CONFIRM_FORCE_CHECKBOX_ID}", Checkbox).value
        return (confirmed, force)

    @on(Button.Pressed, f"#{BTN_CONFIRM_ID}")
    def _confirm(self) -> None:
        self.dismiss(self._result(True))

    @on(Button.Pressed, f"#{BTN_CANCEL_ID}")
    def _cancel(self) -> None:
        self.dismiss(self._result(False))


@dataclass(frozen=True)
class PromptField:
    """One labeled text input in a `PromptScreen`."""

    label: str
    value: str = ""
    placeholder: str = ""


class PromptScreen(ModalScreen):
    """A small multi-field text-input modal.

    One `Label` + `Input` per field, pre-filled from `PromptField.value`.
    Enter on any field but the last moves focus to the next one; Enter on the
    last field (or the OK button) dismisses with `[input.value, ...]` in
    field order. Escape or Cancel dismisses with `None`.
    """

    def __init__(self, title: str, fields: list[PromptField]) -> None:
        super().__init__()
        self._title = title
        self._fields = fields
        self._inputs: list[Input] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"[b]{escape(self._title)}[/b]", id="prompt-title")
            for i, field in enumerate(self._fields):
                yield Label(field.label)
                yield Input(
                    value=field.value,
                    placeholder=field.placeholder,
                    id=f"prompt-input-{i}",
                )
            with Horizontal():
                yield Button("OK", variant="primary", id=BTN_PROMPT_OK_ID)
                yield Button("Cancel", variant="default", id=BTN_PROMPT_CANCEL_ID)

    def on_mount(self) -> None:
        self._inputs = list(self.query(Input))
        if self._inputs:
            self._inputs[0].focus()

    def _submit(self) -> None:
        self.dismiss([i.value for i in self._inputs])

    @on(Input.Submitted)
    def _on_input_submitted(self, event: Input.Submitted) -> None:
        if not self._inputs:
            return
        if event.input is self._inputs[-1]:
            self._submit()
        else:
            idx = self._inputs.index(event.input)
            self._inputs[idx + 1].focus()

    @on(Button.Pressed, f"#{BTN_PROMPT_OK_ID}")
    def _on_ok(self) -> None:
        self._submit()

    @on(Button.Pressed, f"#{BTN_PROMPT_CANCEL_ID}")
    def _on_cancel(self) -> None:
        self.dismiss(None)

    def on_key(self, event) -> None:
        if event.key == "escape":
            event.stop()
            self.dismiss(None)
