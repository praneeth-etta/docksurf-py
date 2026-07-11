"""LogPane — inline log viewer, plus LogOptionsScreen for tail/since picking."""

import re
import threading
from collections import deque
from typing import Callable, Iterable, Iterator, Protocol

from rich.markup import escape
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Button, Input, Label, RichLog, Select

from docksurf_py.constants import (
    BTN_EXPAND_ID,
    BTN_LOG_OPTIONS_CANCEL_ID,
    BTN_LOG_OPTIONS_OK_ID,
    LOG_OPTIONS_SINCE_ID,
    LOG_OPTIONS_TAIL_ID,
    LOG_PANE_HEADER_ID,
    LOG_PANE_SEARCH_ID,
    LOG_PANE_TOOLBAR_ID,
    LOG_PANE_VIEW_ID,
    LogLine,
    LogOptions,
    SafeMarkup,
)

# Cap LogPane._line_buffer so tailing a noisy container can't grow memory
# without bound. Export only includes the most recent `_LOG_BUFFER_MAXLEN` lines.
_LOG_BUFFER_MAXLEN = 20_000

# How often the UI drains queued log lines. Batching avoids per-line
# cross-thread dispatch under heavy log traffic, keeping the UI responsive at
# the cost of up to ~50 ms of display latency.
_DRAIN_INTERVAL_SECONDS = 0.05


def _highlight_match(line: str, term: str) -> str:
    """Return a Rich markup string with every occurrence of term highlighted."""
    if isinstance(line, SafeMarkup):
        return line
    if not term:
        return escape(line)
    parts = re.split(f"({re.escape(term)})", line, flags=re.IGNORECASE)
    return "".join(
        f"[bold yellow]{escape(p)}[/]" if p.lower() == term.lower() else escape(p)
        for p in parts
    )


def _render_log_line(line: LogLine, term: str, show_ts: bool) -> str:
    """Build a Rich-markup string for one `LogLine`.

    Composes an optional colour-coded service prefix (merged project logs), an
    optional dim timestamp, the message with search matches highlighted, and a
    dim-red wrap for stderr lines. Search-highlight is layered *inside* the
    stderr style so matches stay visible on error lines.
    """
    prefix = f"[{line.color}]{escape(line.service):>14}[/] │ " if line.service else ""
    ts = f"[dim]{escape(line.ts)}[/] " if show_ts and line.ts else ""
    body = _highlight_match(line.text, term)
    if line.stream == "stderr":
        body = f"[dim red]{body}[/]"
    return f"{prefix}{ts}{body}"


def _buffer_to_text(lines: Iterable[LogLine], show_ts: bool = True) -> str:
    """Flatten a log buffer to plain text for export (timestamps + markers)."""
    out = []
    for line in lines:
        parts = []
        if show_ts and line.ts:
            parts.append(line.ts)
        if line.service:
            parts.append(f"{line.service} |")
        if line.stream == "stderr":
            parts.append("[stderr]")
        parts.append(line.text)
        out.append(" ".join(parts))
    return "\n".join(out)


class LogSource(Protocol):
    """Structural contract for whatever a log stream factory returns.

    Matches `docker.LogStream` without importing it, so the view layer stays
    a leaf module.
    """

    def __iter__(self) -> Iterator[LogLine]: ...
    def stop(self) -> None: ...


class LogPane(Widget):
    """Inline log viewer that lives in the right panel, expandable to full width."""

    class ToggleExpand(Message):
        """Posted when the user clicks the expand/collapse button."""

    def __init__(self, default_options: LogOptions | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._container_id: str = ""
        self._container_name: str = ""
        self._following = False
        self._paused = False
        self._generation: int = 0
        self._log_stream: LogSource | None = None
        self._expanded = False
        self._stream_factory: Callable[[str, LogOptions], LogSource] | None = None
        self._line_buffer: deque[LogLine] = deque(maxlen=_LOG_BUFFER_MAXLEN)
        # Lines the pump thread has queued since the last UI-thread drain.
        # Swapped out for a fresh list under `_pending_lock` on each drain —
        # the pump thread only ever appends, so the lock's critical sections
        # stay tiny.
        self._pending: list[LogLine] = []
        self._pending_lock = threading.Lock()
        self._drain_timer: Timer | None = None
        # Cached at mount so the pump-thread drain (and every other hot-path
        # write) skips a `query_one` per call.
        self._log_view: RichLog | None = None
        self._filter: str = ""
        self._filter_timer: Timer | None = None
        self._options = default_options or LogOptions()
        self._show_timestamps = False
        self._wrap = False
        # When a filter is active every visible line is a match, so the k-th
        # match sits at rendered row k (no-wrap). `_match_count` is that visible
        # count; `_match_cursor` is the -1-until-navigated jump position.
        self._match_count = 0
        self._match_cursor = -1

    def compose(self) -> ComposeResult:
        with Horizontal(id=LOG_PANE_TOOLBAR_ID):
            yield Label("", id=LOG_PANE_HEADER_ID)
            yield Button("⛶ Expand", id=BTN_EXPAND_ID)
        yield Input(placeholder="Filter logs... (Esc to clear)", id=LOG_PANE_SEARCH_ID)
        yield RichLog(id=LOG_PANE_VIEW_ID, markup=True, highlight=False)

    def on_mount(self) -> None:
        self.query_one(f"#{LOG_PANE_SEARCH_ID}", Input).display = False
        self._log_view = self.query_one(f"#{LOG_PANE_VIEW_ID}", RichLog)

    @on(Button.Pressed, f"#{BTN_EXPAND_ID}")
    def _on_expand_pressed(self) -> None:
        self.post_message(self.ToggleExpand())

    def set_expanded(self, expanded: bool) -> None:
        self._expanded = expanded
        btn = self.query_one(f"#{BTN_EXPAND_ID}", Button)
        if expanded:
            self.add_class("expanded")
            btn.label = "⊡ Collapse"
        else:
            self.remove_class("expanded")
            btn.label = "⛶ Expand"

    @property
    def log_title(self) -> str:
        """Human-readable name of the current source (for the export filename)."""
        return self._container_name

    @property
    def _view(self) -> RichLog:
        """The log `RichLog`, cached at mount — avoids a `query_one` on every
        drained batch (the hot path a chatty stream exercises repeatedly)."""
        if self._log_view is None:
            self._log_view = self.query_one(f"#{LOG_PANE_VIEW_ID}", RichLog)
        return self._log_view

    def load(
        self,
        container_id: str,
        container_name: str,
        stream_factory: Callable[[str, LogOptions], LogSource],
    ) -> None:
        self.stop_follow()
        self._container_id = container_id
        self._container_name = container_name
        self._stream_factory = stream_factory
        self._line_buffer.clear()
        self._filter = ""
        self._match_count = 0
        self._match_cursor = -1
        search_bar = self.query_one(f"#{LOG_PANE_SEARCH_ID}", Input)
        search_bar.value = ""
        search_bar.display = False
        self._view.clear()
        self._start_follow()
        self._update_header()

    def _update_header(self) -> None:
        if self._filter:
            if self._match_count == 0:
                match = "  |  [dim]no matches[/]"
            elif self._match_cursor >= 0:
                match = f"  |  match {self._match_cursor + 1}/{self._match_count}"
            else:
                match = f"  |  {self._match_count} matches"
            filter_hint = f"  |  filter: [bold]{escape(self._filter)}[/]{match}"
        else:
            filter_hint = ""
        if self._following and self._paused:
            state = "  |  [bold yellow][PAUSED][/]"
        elif self._following:
            state = "  |  [bold green][FOLLOWING][/]"
        else:
            state = "  |  [dim]stream ended[/]  |  l to close"
        self.query_one(f"#{LOG_PANE_HEADER_ID}", Label).update(
            f"Logs: {escape(self._container_name)}{state}{filter_hint}"
        )

    def toggle_follow(self) -> None:
        if self._following:
            was_paused = self._paused
            self._paused = not self._paused
            if was_paused and not self._paused:
                self._render_to_view()
        else:
            self._start_follow()
        self._update_header()

    def clear_log(self) -> None:
        self._line_buffer.clear()
        self._view.clear()

    def toggle_search(self) -> None:
        search_bar = self.query_one(f"#{LOG_PANE_SEARCH_ID}", Input)
        if search_bar.display:
            self._close_search()
        else:
            search_bar.display = True
            search_bar.focus()

    def _close_search(self) -> None:
        search_bar = self.query_one(f"#{LOG_PANE_SEARCH_ID}", Input)
        search_bar.display = False
        search_bar.value = ""
        self._filter = ""
        self._match_cursor = -1
        self._render_to_view()
        self._update_header()

    @on(Input.Changed, f"#{LOG_PANE_SEARCH_ID}")
    def _on_filter_changed(self, event: Input.Changed) -> None:
        self._filter = event.value
        # A new term resets the jump cursor; the count is recomputed on render.
        self._match_cursor = -1
        if self._filter_timer is not None:
            self._filter_timer.stop()
        self._filter_timer = self.set_timer(0.2, self._render_to_view)

    @on(Input.Submitted, f"#{LOG_PANE_SEARCH_ID}")
    def _on_filter_submitted(self, event: Input.Submitted) -> None:
        self._view.focus()

    def on_key(self, event) -> None:
        search_bar = self.query_one(f"#{LOG_PANE_SEARCH_ID}", Input)
        if event.key == "slash" and not search_bar.display:
            event.stop()
            self.toggle_search()
        elif event.key == "escape" and search_bar.display:
            event.stop()
            self._close_search()

    @staticmethod
    def _matches(line: LogLine, term: str) -> bool:
        # Search matches on the message only, ignoring timestamp/service prefix.
        return not term or term.lower() in line.text.lower()

    def _render_to_view(self) -> None:
        log_view = self._view
        log_view.clear()
        term = self._filter
        count = 0
        for line in self._line_buffer:
            if self._matches(line, term):
                log_view.write(_render_log_line(line, term, self._show_timestamps))
                if term:
                    count += 1
        self._match_count = count
        if self._match_cursor >= count:
            self._match_cursor = count - 1
        self._update_header()

    def _store_line(self, line: LogLine) -> None:
        return self._line_buffer.append(line)

    def _render_line(self, line: LogLine) -> None:
        if self._matches(line, self._filter):
            rendered = _render_log_line(line, self._filter, self._show_timestamps)
            self._view.write(rendered)
            if self._filter:
                self._match_count += 1

    def _ingest_batch(self, batch: list[LogLine]) -> None:
        """Apply one drained batch: store every line, render only if not
        paused — mirrors what per-line `_store_line`/`_render_line` did, just
        amortized over a batch instead of one `call_from_thread` hop each."""
        for line in batch:
            self._store_line(line)
        if not self._paused:
            for line in batch:
                self._render_line(line)

    def toggle_timestamps(self) -> None:
        self._show_timestamps = not self._show_timestamps
        self._render_to_view()

    def toggle_wrap(self) -> None:
        self._wrap = not self._wrap
        # Set before re-rendering: RichLog decides wrapping as each line is
        # written, and _render_to_view rewrites the whole buffer.
        self._view.wrap = self._wrap
        self._render_to_view()

    def jump(self, delta: int) -> None:
        """Move the search cursor to the next/prev match and scroll to it.

        Only meaningful while a filter is active (every visible line is then a
        match). Wraps around the ends like a pager. Under `wrap` on, the scroll
        target is approximate (a wrapped match spans several rows)."""
        if self._match_count == 0:
            return
        if self._match_cursor < 0:
            self._match_cursor = 0 if delta > 0 else self._match_count - 1
        else:
            self._match_cursor = (self._match_cursor + delta) % self._match_count
        self._view.scroll_to(y=self._match_cursor, animate=False)
        self._update_header()

    def jump_home(self) -> None:
        self._view.scroll_home(animate=False)

    def jump_end(self) -> None:
        self._view.scroll_end(animate=False)

    def export_text(self) -> str:
        """The full buffer as plain text (timestamps + stderr markers)."""
        return _buffer_to_text(self._line_buffer, show_ts=True)

    def set_options(self, options: LogOptions) -> None:
        """Apply new tail/since options — re-subscribes the stream from scratch."""
        self._options = options
        self.stop_follow()
        self._line_buffer.clear()
        self._match_cursor = -1
        self._match_count = 0
        self._view.clear()
        self._start_follow()
        self._update_header()

    @property
    def options(self) -> LogOptions:
        return self._options

    def _start_follow(self) -> None:
        if not self._stream_factory:
            return
        self._generation += 1
        generation = self._generation
        log_stream = self._stream_factory(self._container_id, self._options)
        self._log_stream = log_stream
        self._following = True
        self._paused = False
        with self._pending_lock:
            self._pending = []
        threading.Thread(
            target=self._stream_logs, args=(generation, log_stream), daemon=True
        ).start()
        self._drain_timer = self.set_interval(
            _DRAIN_INTERVAL_SECONDS, self._drain_pending
        )

    def _stream_logs(self, generation: int, log_stream: LogSource) -> None:
        """Pump thread: only ever appends to `_pending` — no UI-thread hop per
        line. `_drain_pending` (a UI-side timer) and `_on_stream_ended` (below)
        are the sole consumers, both running on the UI thread."""
        for line in log_stream:
            if not self._following or self._generation != generation:
                break
            with self._pending_lock:
                self._pending.append(line)
        if self._generation == generation:
            self._following = False
            try:
                self.app.call_from_thread(self._on_stream_ended)
            except Exception:
                pass

    def _drain_pending(self) -> None:
        """UI-thread timer callback: flush whatever the pump thread queued
        since the last tick, in one pass."""
        with self._pending_lock:
            if not self._pending:
                return
            batch = self._pending
            self._pending = []
        self._ingest_batch(batch)
        self._update_header()

    def _on_stream_ended(self) -> None:
        """The stream is done (or was superseded) — flush any residual lines
        before reflecting "stream ended" in the header, so nothing queued
        right before the end is silently dropped. Also stops the drain timer:
        `_start_follow`/`stop_follow` are the only other places that would
        otherwise stop it, so a naturally-finished stream would leave it
        ticking at 20Hz for no reason until the pane is reloaded or closed."""
        if self._drain_timer is not None:
            self._drain_timer.stop()
            self._drain_timer = None
        self._drain_pending()
        self._update_header()

    def stop_follow(self) -> None:
        self._following = False
        self._paused = False
        if self._drain_timer is not None:
            self._drain_timer.stop()
            self._drain_timer = None
        if self._log_stream is not None:
            self._log_stream.stop()
            self._log_stream = None
        self._drain_pending()

    def on_unmount(self) -> None:
        self.stop_follow()


class LogOptionsScreen(ModalScreen):
    """Pick log tail depth and `--since` window; dismisses with a `LogOptions`.

    Display-only picker (same convention as the other modals): it takes the
    current `LogOptions`, pre-selects the matching choices, and hands back a new
    `LogOptions` on OK (or `None` on Cancel/Escape). The caller re-subscribes
    the stream. Timestamps/wrap are instant view toggles, so they stay as their
    own keys rather than living here.
    """

    _TAIL_CHOICES = (
        ("100 lines", "100"),
        ("500 lines", "500"),
        ("5000 lines", "5000"),
        ("All", "all"),
    )
    _SINCE_CHOICES = (
        ("Off", "0"),
        ("Last 5 min", "300"),
        ("Last 15 min", "900"),
        ("Last hour", "3600"),
        ("Last 6 hours", "21600"),
    )

    def __init__(self, options: LogOptions) -> None:
        super().__init__()
        self._options = options

    def compose(self) -> ComposeResult:
        tail_val = "all" if self._options.tail is None else str(self._options.tail)
        with Vertical():
            yield Label("[b]Log options[/b]", id="log-options-title")
            yield Label("Tail (lines to load)")
            yield Select(
                self._TAIL_CHOICES,
                value=tail_val,
                allow_blank=False,
                id=LOG_OPTIONS_TAIL_ID,
            )
            yield Label("Since (time window)")
            yield Select(
                self._SINCE_CHOICES,
                value=str(self._options.since_seconds),
                allow_blank=False,
                id=LOG_OPTIONS_SINCE_ID,
            )
            with Horizontal():
                yield Button("OK", variant="primary", id=BTN_LOG_OPTIONS_OK_ID)
                yield Button("Cancel", id=BTN_LOG_OPTIONS_CANCEL_ID)

    def on_key(self, event) -> None:
        if event.key == "escape":
            event.stop()
            self.dismiss(None)

    @on(Button.Pressed, f"#{BTN_LOG_OPTIONS_OK_ID}")
    def _ok(self) -> None:
        tail_raw = self.query_one(f"#{LOG_OPTIONS_TAIL_ID}", Select).value
        since_raw = self.query_one(f"#{LOG_OPTIONS_SINCE_ID}", Select).value
        tail = None if tail_raw == "all" else int(str(tail_raw))
        self.dismiss(LogOptions(tail=tail, since_seconds=int(str(since_raw))))

    @on(Button.Pressed, f"#{BTN_LOG_OPTIONS_CANCEL_ID}")
    def _cancel(self) -> None:
        self.dismiss(None)
