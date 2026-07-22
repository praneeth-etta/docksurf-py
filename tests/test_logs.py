"""Unit tests for the log-viewer data path (docker.py) and render helpers
(widgets.py) — no running app, no real Docker daemon."""

import unittest

from docksurf_py.constants import LogLine, LogOptions
from docksurf_py.docker import LogStream, _split_timestamp, _strip_ansi
from docksurf_py.widgets import _buffer_to_text, _render_log_line
from docksurf_py.widgets.log_pane import _LOG_BUFFER_MAXLEN, LogPane


class FakeContainer:
    def __init__(self, status="running", frames=()):
        self.status = status
        self._frames = frames
        self.logs_calls: list[dict] = []

    def logs(self, **kwargs):
        self.logs_calls.append(kwargs)
        return iter(self._frames)


class FakeSDK:
    def __init__(self, container):
        self._container = container

        class _Containers:
            def get(_self, _cid):
                return container

        self.containers = _Containers()


class SplitTimestampTests(unittest.TestCase):
    def test_splits_rfc3339_utc(self) -> None:
        ts, text = _split_timestamp("2024-01-01T12:00:00.000000000Z hello world")
        self.assertEqual(ts, "2024-01-01T12:00:00.000000000Z")
        self.assertEqual(text, "hello world")

    def test_splits_offset_timezone(self) -> None:
        ts, text = _split_timestamp("2024-01-01T12:00:00+05:30 payload")
        self.assertEqual(ts, "2024-01-01T12:00:00+05:30")
        self.assertEqual(text, "payload")

    def test_line_without_timestamp(self) -> None:
        ts, text = _split_timestamp("Container abc not found")
        self.assertEqual(ts, "")
        self.assertEqual(text, "Container abc not found")


class StripAnsiTests(unittest.TestCase):
    def test_strips_sgr_reset(self) -> None:
        self.assertEqual(
            _strip_ansi("Running boot step rabbit\x1b[0m"), "Running boot step rabbit"
        )

    def test_strips_sgr_color_codes(self) -> None:
        self.assertEqual(_strip_ansi("\x1b[36minfo\x1b[0m message"), "info message")

    def test_leaves_plain_text_untouched(self) -> None:
        self.assertEqual(
            _strip_ansi("plain text, no escapes"), "plain text, no escapes"
        )


class LogStreamTests(unittest.TestCase):
    def _collect(self, container, options=None):
        stream = LogStream("cid", FakeSDK(container), options)
        return list(stream), container.logs_calls[0]

    def test_requests_timestamps_and_options(self) -> None:
        container = FakeContainer(
            frames=[
                b"2024-01-01T00:00:00Z hello\n",
                b"2024-01-01T00:00:01Z world\n",
            ]
        )
        lines, kwargs = self._collect(
            container, LogOptions(tail=100, since_seconds=300)
        )
        self.assertTrue(kwargs["timestamps"])
        self.assertEqual(kwargs["tail"], 100)
        self.assertIn("since", kwargs)
        self.assertEqual(
            [(line.text, line.stream) for line in lines],
            [("hello", "stdout"), ("world", "stdout")],
        )
        self.assertEqual(lines[0].ts, "2024-01-01T00:00:00Z")

    def test_tail_all_and_no_since(self) -> None:
        container = FakeContainer(frames=[])
        _, kwargs = self._collect(container, LogOptions(tail=None, since_seconds=0))
        self.assertEqual(kwargs["tail"], "all")
        self.assertNotIn("since", kwargs)

    def test_stopped_container_does_not_follow(self) -> None:
        container = FakeContainer(status="exited", frames=[])
        _, kwargs = self._collect(container)
        self.assertFalse(kwargs["follow"])

    def test_strips_ansi_from_raw_frames(self) -> None:
        container = FakeContainer(
            frames=[b"2024-01-01T00:00:00Z \x1b[36mRunning boot step\x1b[0m\n"]
        )
        lines, _ = self._collect(container)
        self.assertEqual(lines[0].text, "Running boot step")
        self.assertEqual(lines[0].ts, "2024-01-01T00:00:00Z")


class RenderHelperTests(unittest.TestCase):
    def test_plain_line(self) -> None:
        out = _render_log_line(LogLine(text="hello"), "", False)
        self.assertEqual(out, "hello")

    def test_stderr_is_dim_red(self) -> None:
        out = _render_log_line(LogLine(text="oops", stream="stderr"), "", False)
        self.assertEqual(out, "[dim red]oops[/]")

    def test_timestamp_shown_and_hidden(self) -> None:
        line = LogLine(text="hi", ts="2024-01-01T00:00:00Z")
        self.assertIn("[dim]2024-01-01T00:00:00Z[/]", _render_log_line(line, "", True))
        self.assertNotIn("2024", _render_log_line(line, "", False))

    def test_service_prefix(self) -> None:
        line = LogLine(text="hi", service="web", color="cyan")
        out = _render_log_line(line, "", False)
        self.assertIn("[cyan]", out)
        self.assertIn("web", out)
        self.assertIn("│", out)

    def test_search_highlight(self) -> None:
        out = _render_log_line(LogLine(text="hello world"), "world", False)
        self.assertIn("[bold yellow]world[/]", out)


class BufferToTextTests(unittest.TestCase):
    def test_includes_ts_service_and_stderr_marker(self) -> None:
        lines = [
            LogLine(text="up", ts="2024-01-01T00:00:00Z"),
            LogLine(text="err", ts="2024-01-01T00:00:01Z", stream="stderr"),
            LogLine(text="hi", service="web", color="cyan"),
        ]
        text = _buffer_to_text(lines, show_ts=True)
        self.assertEqual(
            text.splitlines(),
            [
                "2024-01-01T00:00:00Z up",
                "2024-01-01T00:00:01Z [stderr] err",
                "web | hi",
            ],
        )

    def test_omits_timestamps_when_disabled(self) -> None:
        lines = [LogLine(text="up", ts="2024-01-01T00:00:00Z")]
        self.assertEqual(_buffer_to_text(lines, show_ts=False), "up")


class LogBufferBoundTests(unittest.TestCase):
    def test_buffer_drops_oldest_lines_past_maxlen(self) -> None:
        pane = LogPane()
        total = _LOG_BUFFER_MAXLEN + 100
        for i in range(total):
            pane._store_line(LogLine(text=f"line-{i}"))

        self.assertEqual(len(pane._line_buffer), _LOG_BUFFER_MAXLEN)
        # The oldest 100 lines (0..99) were dropped; the buffer now starts
        # right where they left off.
        self.assertEqual(pane._line_buffer[0].text, "line-100")
        self.assertEqual(pane._line_buffer[-1].text, f"line-{total - 1}")

    def test_clear_log_resets_but_keeps_the_bound(self) -> None:
        pane = LogPane()
        for i in range(10):
            pane._store_line(LogLine(text=f"line-{i}"))
        pane._line_buffer.clear()

        self.assertEqual(len(pane._line_buffer), 0)
        self.assertEqual(pane._line_buffer.maxlen, _LOG_BUFFER_MAXLEN)


if __name__ == "__main__":
    unittest.main()
