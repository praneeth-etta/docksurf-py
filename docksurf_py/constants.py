from enum import StrEnum


# Tab & Table IDs
class TabID(StrEnum):
    CONTAINERS = "tab-containers"
    IMAGES = "tab-images"
    VOLUMES = "tab-volumes"
    NETWORKS = "tab-networks"


class TableID(StrEnum):
    CONTAINERS = "table-containers"
    IMAGES = "table-images"
    VOLUMES = "table-volumes"
    NETWORKS = "table-networks"


# Generic widget IDs
LOG_PANE_ID = "log-pane"
LOG_PANE_VIEW_ID = "log-pane-view"
LOG_PANE_HEADER_ID = "log-pane-header"
LOG_PANE_TOOLBAR_ID = "log-pane-toolbar"
BTN_EXPAND_ID = "expand-btn"

DETAIL_PANE_ID = "detail-pane"
SEARCH_BAR_ID = "search-bar"
STATUS_BAR_ID = "status-bar"
MAIN_CONTAINER_ID = "main-container"
REFRESH_LOADING_ID = "refresh-loading"

# Confirm-dialog button IDs
BTN_CONFIRM_ID = "confirm"
BTN_CANCEL_ID = "cancel"


STATUS_GREEN = "green"
STATUS_RED = "red"
STATUS_YELLOW = "yellow"


class SafeMarkup(str):
    """A str subclass that marks text as already-constructed Rich markup.

    Render helpers (DetailPane, _safe_row) will pass SafeMarkup through
    unchanged and escape every plain str they receive.
    """


def markup_green(text: str) -> SafeMarkup:
    return SafeMarkup(f"[{STATUS_GREEN}]{text}[/]")


def markup_red(text: str) -> SafeMarkup:
    return SafeMarkup(f"[{STATUS_RED}]{text}[/]")


def markup_yellow(text: str) -> SafeMarkup:
    return SafeMarkup(f"[{STATUS_YELLOW}]{text}[/]")
