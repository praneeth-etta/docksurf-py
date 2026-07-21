"""Live streaming sources: logs, stats, daemon events, pull progress."""

import logging
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import replace
from typing import Iterator

import requests.exceptions
from docker.errors import APIError, DockerException, NotFound

from docksurf_py.constants import LOG_SERVICE_COLORS, LogLine, LogOptions
from docksurf_py.models import ContainerStats

logger = logging.getLogger(__name__)


def _safe_close(generator) -> None:
    """Close a stream generator, tolerating a cross-thread mid-read close.

    `stop()` runs on the UI thread while the pump thread may be blocked inside
    the generator; CPython raises "generator already executing" in that race.
    The stream's `_active` flag already stops consumption, so closing (which
    just unblocks a pending read) is best-effort.
    """
    if generator is not None and hasattr(generator, "close"):
        try:
            generator.close()
        except Exception:
            pass


def _split_timestamp(raw: str) -> tuple[str, str]:
    """Split a docker `timestamps=True` log line into (timestamp, message).

    Docker prefixes each line with an RFC3339 timestamp and a single space,
    e.g. `2024-01-01T12:00:00.000000000Z hello`. The timestamp is stored
    separately so the view can show/hide it without a re-fetch and so the
    search filter matches on the message only. Lines without a recognisable
    timestamp (our own error strings) come back as `("", raw)`.
    """
    head, sep, rest = raw.partition(" ")
    looks_like_ts = "T" in head and (
        head.endswith("Z") or "+" in head or head[-6:-5] in "+-"
    )
    if sep and looks_like_ts:
        return head, rest
    return "", raw


class LogStream:
    """Wraps docker SDK log generator and exposes it as a `LogLine` iterator."""

    def __init__(
        self, container_id: str, sdk_client, options: LogOptions | None = None
    ) -> None:
        self._container_id = container_id
        self._client = sdk_client
        self._options = options or LogOptions()
        self._active = False
        self._generator: Iterator | None = None

    def _logs_kwargs(self, follow: bool) -> dict:
        # docker-py 7.x `logs()` has no `demux`, so stdout/stderr come back
        # combined (both default True) — there's no per-line origin to recover.
        # We request timestamps and honour the tail/since options.
        kwargs: dict = {
            "stream": True,
            "follow": follow,
            "timestamps": True,
            "tail": self._options.tail if self._options.tail is not None else "all",
        }
        if self._options.since_seconds > 0:
            kwargs["since"] = int(time.time()) - self._options.since_seconds
        return kwargs

    def __iter__(self) -> Iterator[LogLine]:
        if not self._client:
            return

        self._active = True
        logger.info("Log stream started for container %s", self._container_id)
        try:
            container = self._client.containers.get(self._container_id)
            follow = container.status == "running"
            self._generator = container.logs(**self._logs_kwargs(follow))

            for raw_line in self._generator:
                if not self._active:
                    break
                ts, text = _split_timestamp(
                    raw_line.decode("utf-8", errors="replace").rstrip()
                )
                yield LogLine(text=text, ts=ts)
        except NotFound:
            logger.warning("Log stream: container %s not found", self._container_id)
            yield LogLine(
                text=f"Container {self._container_id} not found", stream="stderr"
            )
        except Exception as e:
            logger.exception("Log stream error for %s: %s", self._container_id, e)
            yield LogLine(text=f"Log stream error: {e}", stream="stderr")
        finally:
            self.stop()

    def stop(self) -> None:
        if self._active:
            logger.info("Log stream stopped for container %s", self._container_id)
        self._active = False
        _safe_close(self._generator)


def _assign_service_colors(services: list[str]) -> dict[str, str]:
    """Map each distinct service name to a colour, cycling the palette."""
    colors: dict[str, str] = {}
    for service in services:
        if service not in colors:
            colors[service] = LOG_SERVICE_COLORS[len(colors) % len(LOG_SERVICE_COLORS)]
    return colors


class MergedLogStream:
    """Interleaves several containers' logs into one `LogLine` iterator.

    Each child line is tagged with its service label and a cycled colour, so
    `LogPane` can render a `docker compose logs -f`-style colour-coded prefix.
    Presentation lives in the view — this class only sets `service`/`color` on
    the child `LogLine`s. Satisfies the `LogSource` structural protocol
    (`__iter__` + `stop()`) the same way `LogStream` does.
    """

    def __init__(
        self,
        specs: list[tuple[str, str]],
        sdk_client,
        options: LogOptions | None = None,
    ) -> None:
        # specs: list of (service_name, container_id)
        self._specs = specs
        self._client = sdk_client
        self._streams = [LogStream(cid, sdk_client, options) for _, cid in specs]
        self._active = False
        self._queue: queue.Queue = queue.Queue()

    def __iter__(self) -> Iterator[LogLine]:
        if not self._client or not self._specs:
            return

        self._active = True
        logger.info("Merged log stream started for %d containers", len(self._specs))
        colors = _assign_service_colors([service for service, _ in self._specs])
        sentinel = object()

        def pump(service: str, stream: LogStream) -> None:
            color = colors[service]
            try:
                for line in stream:
                    if not self._active:
                        break
                    self._queue.put(replace(line, service=service, color=color))
            finally:
                self._queue.put(sentinel)

        for (service, _cid), stream in zip(self._specs, self._streams):
            threading.Thread(target=pump, args=(service, stream), daemon=True).start()

        remaining = len(self._specs)
        while remaining > 0:
            item = self._queue.get()
            if item is sentinel:
                remaining -= 1
                continue
            if not self._active:
                break
            yield item
        self.stop()

    def stop(self) -> None:
        self._active = False
        for stream in self._streams:
            stream.stop()


def _parse_stats(sample: dict) -> ContainerStats:
    """Turn one raw SDK stats sample into a typed `ContainerStats`.

    CPU% is derived from the in-sample `cpu_stats`/`precpu_stats` delta the same
    way `docker stats` computes it; memory subtracts the reclaimable page cache
    (`inactive_file`) to match the CLI's used figure.
    """
    cpu = sample.get("cpu_stats") or {}
    precpu = sample.get("precpu_stats") or {}
    cpu_usage = (cpu.get("cpu_usage") or {}).get("total_usage", 0)
    precpu_usage = (precpu.get("cpu_usage") or {}).get("total_usage", 0)
    cpu_delta = cpu_usage - precpu_usage
    system_delta = cpu.get("system_cpu_usage", 0) - precpu.get("system_cpu_usage", 0)
    online = (
        cpu.get("online_cpus")
        or len((cpu.get("cpu_usage") or {}).get("percpu_usage") or [])
        or 1
    )
    cpu_percent = (
        (cpu_delta / system_delta) * online * 100.0
        if system_delta > 0 and cpu_delta > 0
        else 0.0
    )

    mem = sample.get("memory_stats") or {}
    mem_limit = mem.get("limit", 0) or 0
    cache = (mem.get("stats") or {}).get("inactive_file", 0)
    mem_used = max((mem.get("usage", 0) or 0) - cache, 0)
    mem_percent = (mem_used / mem_limit * 100.0) if mem_limit else 0.0

    net_rx = net_tx = 0
    for iface in (sample.get("networks") or {}).values():
        net_rx += iface.get("rx_bytes", 0)
        net_tx += iface.get("tx_bytes", 0)

    blk_read = blk_write = 0
    for entry in (sample.get("blkio_stats") or {}).get(
        "io_service_bytes_recursive"
    ) or []:
        op = (entry.get("op") or "").lower()
        if op == "read":
            blk_read += entry.get("value", 0)
        elif op == "write":
            blk_write += entry.get("value", 0)

    return ContainerStats(
        cpu_percent=cpu_percent,
        mem_used=mem_used,
        mem_limit=mem_limit,
        mem_percent=mem_percent,
        net_rx=net_rx,
        net_tx=net_tx,
        blk_read=blk_read,
        blk_write=blk_write,
    )


class StatsStream:
    """Streams live `ContainerStats` for one container — mirrors `LogStream`.

    Wraps the SDK's `container.stats(stream=True)` generator; `__iter__` yields a
    parsed `ContainerStats` per sample (~1/sec) and `stop()` unblocks it.
    """

    def __init__(self, container_id: str, sdk_client) -> None:
        self._container_id = container_id
        self._client = sdk_client
        self._active = False
        self._generator: Iterator[dict] | None = None

    def __iter__(self) -> Iterator[ContainerStats]:
        if not self._client:
            return
        self._active = True
        logger.info("Stats stream started for container %s", self._container_id)
        try:
            container = self._client.containers.get(self._container_id)
            self._generator = container.stats(stream=True, decode=True)
            for sample in self._generator:
                if not self._active:
                    break
                yield _parse_stats(sample)
        except NotFound:
            logger.warning("Stats stream: container %s not found", self._container_id)
        except Exception as e:
            logger.exception("Stats stream error for %s: %s", self._container_id, e)
        finally:
            self.stop()

    def stop(self) -> None:
        if self._active:
            logger.info("Stats stream stopped for container %s", self._container_id)
        self._active = False
        _safe_close(self._generator)


class EventStream:
    """Streams decoded Docker daemon events — mirrors `LogStream`.

    Filtered to the resource types the UI renders; `__iter__` yields each event
    dict and `stop()` unblocks the (otherwise indefinitely blocking) generator.
    """

    _FILTERS = {"type": ["container", "image", "volume", "network"]}

    def __init__(self, sdk_client) -> None:
        self._client = sdk_client
        self._active = False
        self._generator: Iterator[dict] | None = None
        # Populated if iteration ends because of an unexpected error rather than
        # a deliberate stop(), since this iterator never propagates exceptions.
        self.error: Exception | None = None

    def __iter__(self) -> Iterator[dict]:
        if not self._client:
            return
        self._active = True
        logger.info("Event stream started")
        try:
            self._generator = self._client.events(decode=True, filters=self._FILTERS)
            for event in self._generator:
                if not self._active:
                    break
                yield event
        except Exception as e:
            logger.exception("Event stream error: %s", e)
            self.error = e
        finally:
            self.stop()

    def stop(self) -> None:
        if self._active:
            logger.info("Event stream stopped")
        self._active = False
        _safe_close(self._generator)


class PullStream:
    """Streams `docker pull` progress for one image — mirrors `EventStream`.

    Wraps the low-level `api.pull(stream=True, decode=True)` generator; `__iter__`
    yields each raw progress dict (`{"status", "id", "progress", "error", ...}`)
    and `stop()` unblocks it. Consumers format the dicts (kept untyped like
    `EventStream`, since the shape is display-only). A pull that fails mid-stream
    yields a dict carrying an `"error"` key rather than raising.
    """

    def __init__(self, repository: str, tag: str, sdk_client) -> None:
        self._repository = repository
        self._tag = tag
        self._client = sdk_client
        self._active = False
        self._generator: Iterator[dict] | None = None

    def __iter__(self) -> Iterator[dict]:
        if not self._client:
            return
        self._active = True
        ref = f"{self._repository}:{self._tag}"
        logger.info("Pull stream started for %s", ref)
        try:
            self._generator = self._client.api.pull(
                self._repository, tag=self._tag, stream=True, decode=True
            )
            for chunk in self._generator:
                if not self._active:
                    break
                yield chunk
        except (APIError, DockerException, requests.exceptions.RequestException) as e:
            logger.warning("Pull stream error for %s: %s", ref, e)
            yield {"error": str(e)}
        except Exception as e:  # noqa: BLE001 - surface any unexpected failure
            logger.exception("Pull stream error for %s: %s", ref, e)
            yield {"error": str(e)}
        finally:
            self.stop()

    def stop(self) -> None:
        if self._active:
            logger.info("Pull stream stopped for %s:%s", self._repository, self._tag)
        self._active = False
        _safe_close(self._generator)


class ComposeBuildStream:
    """Streams `docker compose up --build` output for one service.

    A subprocess variant of the SDK-based streams here: docker-py has no Compose
    support (the sanctioned Compose subprocess exception — see CLAUDE.md), so
    this wraps a `docker compose` process rather than an SDK generator.
    `__iter__` yields combined stdout/stderr lines as the build/recreate runs;
    `returncode` is set when the process exits (like `EventStream.error`, this
    iterator never raises). `stop()` terminates the process, so closing the
    progress screen mid-build actually stops the build.
    """

    def __init__(
        self,
        cmd: list[str],
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._cmd = cmd
        self._cwd = cwd or None
        # Environment for the subprocess (e.g. BUILDKIT_PROGRESS=plain to keep
        # build output line-oriented). None inherits the parent environment.
        self._env = env
        self._active = False
        self._process: subprocess.Popen | None = None
        # None until the process exits; consumers read it to tell success from
        # failure after the line loop ends.
        self.returncode: int | None = None

    def __iter__(self) -> Iterator[str]:
        self._active = True
        logger.info(
            "Compose build stream started: %s (cwd=%s)",
            " ".join(self._cmd),
            self._cwd,
        )
        if shutil.which(self._cmd[0]) is None:
            self.returncode = 1
            yield "docker CLI not found on PATH — cannot rebuild"
            self._active = False
            return
        try:
            self._process = subprocess.Popen(
                self._cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=self._cwd,
                env=self._env,
            )
            assert self._process.stdout is not None
            for raw_line in self._process.stdout:
                if not self._active:
                    break
                yield raw_line.rstrip("\n")
            self._process.stdout.close()
            self.returncode = self._process.wait()
        except Exception as e:  # noqa: BLE001 - surface any unexpected failure
            logger.exception("Compose build stream error: %s", e)
            if self.returncode is None:
                self.returncode = 1
            yield f"Build stream error: {e}"
        finally:
            self.stop()

    def stop(self) -> None:
        if self._active:
            logger.info("Compose build stream stopped")
        self._active = False
        proc = self._process
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except Exception:
                pass
