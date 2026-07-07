"""SearchBar — Input subclass that closes on Escape."""

from textual.widgets import Input


class SearchBar(Input):
    """Search bar that closes itself on Escape."""

    BINDINGS = [("escape", "close", "Close")]

    def action_close(self) -> None:
        self.display = False
        self.value = ""
