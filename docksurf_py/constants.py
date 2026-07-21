from dataclasses import dataclass
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
LOG_PANE_SEARCH_ID = "log-pane-search"
BTN_EXPAND_ID = "expand-btn"

# LogOptionsScreen widget IDs
LOG_OPTIONS_TAIL_ID = "log-options-tail"
LOG_OPTIONS_SINCE_ID = "log-options-since"
BTN_LOG_OPTIONS_OK_ID = "log-options-ok"
BTN_LOG_OPTIONS_CANCEL_ID = "log-options-cancel"

# InspectScreen widget IDs
INSPECT_VIEW_ID = "inspect-view"
INSPECT_SEARCH_ID = "inspect-search"
BTN_INSPECT_CLOSE_ID = "inspect-close"

# PullProgressScreen widget IDs
PULL_PROGRESS_VIEW_ID = "pull-progress-view"
BTN_PULL_PROGRESS_CLOSE_ID = "pull-progress-close"

# BuildProgressScreen widget IDs
BUILD_PROGRESS_VIEW_ID = "build-progress-view"
BTN_BUILD_PROGRESS_CLOSE_ID = "build-progress-close"

# LayerHistoryScreen widget IDs
BTN_LAYER_HISTORY_CLOSE_ID = "layer-history-close"

# ContainerPickerScreen widget IDs
PICKER_LIST_ID = "picker-list"
BTN_PICKER_CANCEL_ID = "picker-cancel"

# Prune target keys shared between PruneScreen (widgets.py) and PruneHandler
# (actions.py) — dismissing PruneScreen with one of these selects the
# matching `DockerClient.prune_*` method. Also doubles as each target
# button's ID suffix ("prune-<target>").
PRUNE_TARGETS: tuple[str, ...] = (
    "containers",
    "images",
    "volumes",
    "networks",
    "system",
)
BTN_PRUNE_CANCEL_ID = "prune-cancel"

DETAIL_PANE_ID = "detail-pane"
SEARCH_BAR_ID = "search-bar"
STATUS_BAR_ID = "status-bar"
MAIN_CONTAINER_ID = "main-container"
REFRESH_LOADING_ID = "refresh-loading"

# Confirm-dialog button IDs
BTN_CONFIRM_ID = "confirm"
BTN_CANCEL_ID = "cancel"
CONFIRM_FORCE_CHECKBOX_ID = "confirm-force"

# Empty-state placeholder IDs — one per resource table, shown instead of the
# table itself whenever it has zero rows (see TableRenderer._update_empty_state).
EMPTY_STATE_IDS: dict[TabID, str] = {
    TabID.CONTAINERS: "empty-containers",
    TabID.IMAGES: "empty-images",
    TabID.VOLUMES: "empty-volumes",
    TabID.NETWORKS: "empty-networks",
}

# Shown when a tab's table is genuinely empty (no active search, Docker
# connected) — a nudge toward the command that would populate it.
EMPTY_STATE_HINTS: dict[TabID, str] = {
    TabID.CONTAINERS: "No containers — try `docker run hello-world`",
    TabID.IMAGES: "No images — try `docker pull nginx`",
    TabID.VOLUMES: "No volumes — try `docker volume create my-volume`",
    TabID.NETWORKS: "No networks — try `docker network create my-network`",
}

# Persistent "Docker unreachable" banner, docked under the Header for as long
# as the connection stays down (the StatusBar segment + toast already exist
# but the toast expires after 12s — see
# SnapshotManager._maybe_notify_connection_change).
CONNECTION_BANNER_ID = "connection-banner"

# Small, always visible connected/disconnected readout docked under the Header
CONNECTION_INDICATOR_ID = "connection-indicator"

# PromptScreen button IDs
BTN_PROMPT_OK_ID = "prompt-ok"
BTN_PROMPT_CANCEL_ID = "prompt-cancel"


STATUS_GREEN = "green"
STATUS_RED = "red"
STATUS_YELLOW = "yellow"

# Palette cycled per service in the aggregated (merged) project log view, so
# each service's lines are colour-coded like `docker compose logs -f`.
LOG_SERVICE_COLORS = (
    "cyan",
    "magenta",
    "green",
    "yellow",
    "blue",
    "bright_red",
    "bright_cyan",
    "bright_magenta",
)


@dataclass(frozen=True, slots=True)
class LogOptions:
    """Stream-shaping options for the log viewer.

    Only covers what changes *what the daemon sends* (so a change requires
    re-subscribing the stream). Display-only toggles (timestamps, wrap) live on
    the `LogPane` widget, since they never re-fetch. `tail=None` means "all";
    `since_seconds=0` means no `--since` window.
    """

    tail: int | None = 500
    since_seconds: int = 0


@dataclass(frozen=True, slots=True)
class LogLine:
    """One rendered log line, carrying everything the view needs to style it.

    Replaces the old flat `list[str]` buffer so timestamps and per-service
    (merged-project) labels survive from the stream layer to the renderer
    without pre-formatted-markup special cases. `text` is the message only
    (timestamp split out into `ts`). `stream` is `"stdout"` for container
    output (docker-py can't demux logs, so real lines are always stdout) and
    `"stderr"` for our own stream-error messages, which the view styles
    dim-red. `service`/`color` are set only for merged project streams.
    """

    text: str
    ts: str = ""
    stream: str = "stdout"
    service: str = ""
    color: str = ""


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


# Multi-select mark glyph — the leading-column cell for a marked row. Shared
# between renderer.py (row population) and actions.py (single-cell update on
# toggle) so both draw the same glyph without one importing the other.
MARK_GLYPH = SafeMarkup("[bold cyan]●[/]")
