"""WhaleScreen — a fun/polish easter egg: an animated ASCII whale breaching."""

from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

# Each frame is exactly 11 lines so the box never resizes as it cycles.
# Story: calm water -> a tail-tip breaks the surface -> the fluke spreads
# into a full breach (with the wordmark + spray, held) -> sinks back through
# the same shape -> splash rings -> back to calm.
_CALM = (
    "\n" * 8
    + "  [blue]~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~[/]\n"
    + " [blue]~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~[/]\n"
    + "[blue]~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~[/]"
)

_TIP = (
    "\n" * 6
    + "[bold white]                           /\\ [/]\n"
    + "[bold white]                          /  \\ [/]\n"
    + "  [blue]~~~~~~~~~~~~~~~~~~~~~~~~    ~~~~~~~~~~~~~~~~~~~~~~~[/]\n"
    + " [blue]~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~[/]\n"
    + "[blue]~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~[/]"
)

_LOBES = (
    "\n" * 3
    + "[bold white]                  __          __[/]\n"
    + "[bold white]                 /  \\        /  \\ [/]\n"
    + "[bold white]                |    \\      /    |[/]\n"
    + "[bold white]                 \\    \\    /    /[/]\n"
    + "[bold white]                  \\    `--`    /[/]\n"
    + "  [blue]~~~~~~~~~~~~~~~~~\\    ||    /~~~~~~~~~~~~~~~~~~~~~[/]\n"
    + " [blue]~~~~~~~~~~~~~~~~~~~~\\  ||  /~~~~~~~~~~~~~~~~~~~~~~~[/]\n"
    + "[blue]~~~~~~~~~~~~~~~~~~~~~~~\\||/~~~~~~~~~~~~~~~~~~~~~~~~~~[/]"
)

_BREACH = (
    "[cyan]     '        .                        .        ' [/]\n"
    + "[bold cyan]                       DOCKSURF                     [/]\n"
    + "[cyan]          .       '            '        .          [/]\n"
    + "[bold white]                  __          __[/]\n"
    + "[bold white]                 /  \\   '    /  \\   .[/]\n"
    + "[bold white]                |    \\      /    |[/]\n"
    + "[bold white]                 \\    \\    /    /[/]\n"
    + "[bold white]                  \\    `--`    /[/]\n"
    + "  [blue]~~~~~~~~~~~~~~~~~\\    ||    /~~~~~~~~~~~~~~~~~~~~~[/]\n"
    + " [blue]~~~~~~~~~~~~~~~~~~~~\\  ||  /~~~~~~~~~~~~~~~~~~~~~~~[/]\n"
    + "[blue]~~~~~~~~~~~~~~~~~~~~~~~\\||/~~~~~~~~~~~~~~~~~~~~~~~~~~[/]"
)

_SPLASH = (
    "\n" * 7
    + "[cyan]                    .   ·    .[/]\n"
    + "  [blue]~~~~~~~~~~~~~~~~( ~~~~~~ )~~~~~~~~~~~~~~~~~~~~~~~[/]\n"
    + " [blue]~~~~~~~~~~~~~~~~~( ~~~~~~~~ )~~~~~~~~~~~~~~~~~~~~~~[/]\n"
    + "[blue]~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~[/]"
)

WHALE_FRAMES: tuple[str, ...] = (_CALM, _TIP, _LOBES, _BREACH, _LOBES, _SPLASH)
_FRAME_SECONDS = 0.65


class WhaleScreen(ModalScreen):
    """`~`: a whale breaching in rolling waves. Pure fun/polish — no Docker
    I/O, nothing to configure, dismisses on Escape/`~`/Close like the other
    modals (`SystemDfScreen`, `HelpScreen`)."""

    _frame_index: int = 0
    _timer = None

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(WHALE_FRAMES[0], id="whale-canvas")
            yield Button("Close", variant="primary", id="whale-close")

    def on_mount(self) -> None:
        self._timer = self.set_interval(_FRAME_SECONDS, self._advance)

    def on_unmount(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def _advance(self) -> None:
        self._frame_index = (self._frame_index + 1) % len(WHALE_FRAMES)
        self.query_one("#whale-canvas", Static).update(WHALE_FRAMES[self._frame_index])

    def on_key(self, event) -> None:
        if event.key in ("escape", "~"):
            event.stop()
            self.dismiss()

    @on(Button.Pressed, "#whale-close")
    def _close(self) -> None:
        self.dismiss()
