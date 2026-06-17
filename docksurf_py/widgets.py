from rich.panel import Panel
from rich.table import Table
from textual.widgets import Static


class DetailPane(Static):
    """A custom widget that displays a beautiful key-value table."""

    def update_details(self, title: str, data: dict) -> None:
        # Create a borderless table
        table = Table(show_header=False, expand=True, box=None)
        table.add_column("Property", style="cyan", justify="right", width=15)
        table.add_column("Value")

        for key, value in data.items():
            table.add_row(f"[b]{key}[/b]", str(value))

        # Wrap it in a panel and tell the Textual Static widget to update itself
        self.update(Panel(table, title=f"[b]{title}[/b]", border_style="blue"))

    def clear_details(self) -> None:
        self.update(Panel("Select an item to view details.", border_style="dim"))
