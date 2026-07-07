"""
docker/client.py — DockerClient: the sole Docker-facing object the app imports.
"""

import logging
import shutil
import subprocess
from dataclasses import replace
from typing import Callable

import docker
import requests.exceptions
from docker.errors import APIError, DockerException, NotFound
from docker.types import IPAMConfig, IPAMPool

from docksurf_py.connection import (
    ConnectionState,
    ConnectionStatus,
    _classify_docker_error,
    _get_docker_context,
    _get_docker_host,
)
from docksurf_py.constants import LogOptions
from docksurf_py.docker.context import (
    _DEFAULT_DOCKER_SOCK,
    _build_sdk_client_for_context,
    _clear_last_context,
    _create_sdk_client,
    _load_last_context,
    _save_last_context,
)
from docksurf_py.docker.fetcher import DockerResourceFetcher
from docksurf_py.docker.format import _parse_system_df, format_size
from docksurf_py.docker.streams import (
    EventStream,
    LogStream,
    MergedLogStream,
    PullStream,
    StatsStream,
)
from docksurf_py.models import (
    CommandErrorKind,
    CommandResult,
    ContainerTop,
    ContextInfo,
    DockerSnapshot,
    ImageLayer,
    SystemDf,
)

logger = logging.getLogger(__name__)


class DockerClient:
    """
    The only Docker-facing object the rest of the app should import.

    Owns the SDK client lifecycle, delegates reads to DockerResourceFetcher,
    and handles all write/management commands directly.
    """

    def __init__(self) -> None:
        self._sdk: docker.DockerClient | None = None
        self._fetcher: DockerResourceFetcher | None = None
        # Context picked via the in-app switcher (see `switch_context`),
        # persisted across restarts, takes precedence over the ambient
        # `docker context` on every (re)connect while set.
        self._context_override: str | None = _load_last_context()
        self.connection: ConnectionState = ConnectionState(
            status=ConnectionStatus.NOT_CONNECTED,
            message="Not yet connected",
            hint="",
            context=self._context_override or _get_docker_context(),
            host=_get_docker_host(),
        )

    def ensure_connected(self) -> ConnectionState:
        """(Re)attempt to connect if not already connected.

        Called lazily from the read/write methods below instead of blocking
        the constructor on the daemon round-trip. Safe to call on every
        operation — once connected it's a no-op, and while disconnected it
        naturally retries on the next call, which is what makes reconnecting
        after the daemon comes back up "just work".
        """
        if not self.is_connected:
            self.connection = self._connect()
        return self.connection

    def _connect(self) -> ConnectionState:
        ctx = None
        if self._context_override:
            try:
                from docker.context import ContextAPI

                ctx = ContextAPI.get_context(self._context_override)
            except Exception:
                ctx = None
            if ctx is None:
                logger.warning(
                    "Saved context %r no longer exists — using ambient default",
                    self._context_override,
                )
                self._context_override = None
                _clear_last_context()

        context = ctx.Name if ctx is not None else _get_docker_context()
        host = (
            (ctx.Host or _DEFAULT_DOCKER_SOCK)
            if ctx is not None
            else _get_docker_host()
        )
        try:
            sdk = (
                _build_sdk_client_for_context(ctx)
                if ctx is not None
                else _create_sdk_client()
            )
            sdk.ping()  # real round-trip — surfaces connection errors at init time
            self._sdk = sdk
            self._fetcher = DockerResourceFetcher(sdk)
            logger.info("Connected to Docker — context=%s host=%s", context, host)
            return ConnectionState(
                status=ConnectionStatus.CONNECTED,
                message="Connected",
                hint="",
                context=context,
                host=host,
            )
        except DockerException as e:
            logger.exception("Docker connection failed: %s", e)
            return replace(_classify_docker_error(e), context=context, host=host)
        except FileNotFoundError as e:
            logger.exception("Docker executable not found: %s", e)
            return ConnectionState(
                status=ConnectionStatus.NOT_INSTALLED,
                message="Docker is not installed on PATH",
                hint="Install docker: https://docs.docker.com/get-docker/",
                context=context,
                host=host,
            )
        except Exception as e:
            logger.exception("Unexpected error connecting to Docker: %s", e)
            return ConnectionState(
                status=ConnectionStatus.API_ERROR,
                message="Unexpected error connecting to Docker",
                hint="Install Docker: https://docs.docker.com/get-docker/",
                context=context,
                host=host,
            )

    @property
    def is_connected(self) -> bool:
        return self.connection.status == ConnectionStatus.CONNECTED

    def mark_disconnected(self, exc: Exception) -> None:
        """Flip connection state to disconnected after a live call fails.

        `ensure_connected()` only reattempts `_connect()` while
        `not self.is_connected` — without this, a daemon that dies mid-session
        would leave `self.connection` stuck at `CONNECTED` forever (nothing
        else ever un-sets it), so every later `ensure_connected()` call would
        no-op and the app would never reconnect. Resetting `_sdk`/`_fetcher`
        also ensures the *next* connect attempt builds a fresh client rather
        than reusing a possibly-stale one. No-ops if already disconnected.
        """
        if not self.is_connected:
            return
        self._sdk = None
        self._fetcher = None
        classified = _classify_docker_error(exc)
        # Preserve the context/host we were actually using — `_classify_docker_error`
        # recomputes them from the ambient environment, which would be wrong
        # once an in-app context override (see switch_context) is active.
        self.connection = replace(
            classified, context=self.connection.context, host=self.connection.host
        )
        logger.warning("Docker connection lost: %s", exc)

    # --- Reads ---

    def fetch_snapshot(self) -> DockerSnapshot:
        self.ensure_connected()
        if not self._fetcher:
            return DockerSnapshot([], [], [], [])
        try:
            return self._fetcher.fetch_snapshot()
        except (DockerException, requests.exceptions.RequestException) as e:
            self.mark_disconnected(e)
            return DockerSnapshot([], [], [], [])

    def fetch_logs(self, container_id: str, tail: int = 500) -> str:
        if not self._sdk:
            return "Docker client not initialized"
        try:
            container = self._sdk.containers.get(container_id)
            logs = container.logs(tail=tail, stdout=True, stderr=True)
            return logs.decode("utf-8", errors="replace").strip()
        except Exception as e:
            return f"Error fetching logs: {e}"

    def stream_logs(
        self, container_id: str, options: LogOptions | None = None
    ) -> LogStream:
        return LogStream(container_id, self._sdk, options)

    def stream_project_logs(
        self, specs: list[tuple[str, str]], options: LogOptions | None = None
    ) -> MergedLogStream:
        """Build a merged log stream over a project's (service, id) pairs."""
        return MergedLogStream(specs, self._sdk, options)

    def stream_stats(self, container_id: str) -> StatsStream:
        """Live resource-usage stream for one container."""
        return StatsStream(container_id, self._sdk)

    def stream_events(self) -> EventStream:
        """Live stream of Docker daemon events (for auto-refresh)."""
        return EventStream(self._sdk)

    def stream_pull(self, repository: str, tag: str = "latest") -> PullStream:
        """Progress stream for pulling one image (`docker pull repo:tag`)."""
        return PullStream(repository, tag, self._sdk)

    def list_contexts(self) -> list[ContextInfo]:
        """All configured `docker context`s — for the in-app context switcher.

        `is_current` compares against `self.connection.context` (whatever
        DockSurf is actually connected through right now, override or
        ambient) rather than `ContextAPI.get_current_context()`, which only
        reflects the OS-level ambient context. Works even while disconnected,
        since listing contexts is local disk I/O, not a daemon round-trip.
        """
        try:
            from docker.context import ContextAPI

            contexts = ContextAPI.contexts()
        except Exception as e:
            logger.warning("Failed to list docker contexts: %s", e)
            return []
        return [
            ContextInfo(
                name=ctx.Name,
                host=ctx.Host or _DEFAULT_DOCKER_SOCK,
                is_current=(ctx.Name == self.connection.context),
            )
            for ctx in contexts
        ]

    def image_history(self, image_id: str) -> list[ImageLayer] | None:
        """Layer history for one image (`docker history`) — `None` on error.

        Read-only, so it mirrors `container_top`'s try/except shape rather than
        the write-path `_run_management_op`.
        """
        if not self._sdk:
            return None
        try:
            raw = self._sdk.images.get(image_id).history()
        except NotFound:
            logger.warning("History: image %s not found", image_id)
            return None
        except (APIError, DockerException, requests.exceptions.RequestException) as e:
            logger.warning("History failed for %s: %s", image_id, e)
            return None
        layers: list[ImageLayer] = []
        for entry in raw:
            layers.append(
                ImageLayer(
                    created_by=(entry.get("CreatedBy") or "").strip(),
                    size_bytes=entry.get("Size", 0) or 0,
                    created=str(entry.get("Created", "")),
                )
            )
        return layers

    def volume_sizes(self) -> dict[str, int]:
        """Per-volume on-disk size, keyed by volume name (`docker system df -v`).

        Slow (the daemon walks each volume), so callers run it off the UI thread
        and on-demand only — never on the snapshot path. Returns an empty dict
        if disconnected or the daemon reports no usage data.
        """
        if not self._sdk:
            return {}
        try:
            raw = self._sdk.df()
        except (APIError, DockerException, requests.exceptions.RequestException) as e:
            logger.warning("volume_sizes failed: %s", e)
            return {}
        sizes: dict[str, int] = {}
        for v in raw.get("Volumes") or []:
            name = v.get("Name")
            if name:
                sizes[name] = (v.get("UsageData") or {}).get("Size", 0) or 0
        return sizes

    def system_df(self) -> SystemDf:
        """Disk usage per resource category (`docker system df`).

        Slow on the daemon side (it sums layer/volume sizes), so callers should
        run this off the UI thread and never on the snapshot path.
        """
        if not self._sdk:
            return SystemDf(entries=[], total_size=0, total_reclaimable=0)
        raw = self._sdk.df()
        return _parse_system_df(raw)

    # --- Writes ---

    def switch_context(self, name: str) -> CommandResult:
        """Switch DockSurf's own Docker connection to another context.

        In-app only: unlike `docker context use`, this never touches
        `~/.docker/config.json`, so every other terminal keeps whatever
        context it already had. Builds and pings a fresh client scoped to
        the target context *before* touching `self._sdk`/`self.connection`,
        so a failed switch (e.g. an unreachable remote host) leaves the
        current working connection untouched. On success the choice is
        persisted so it survives an app restart.
        """
        try:
            from docker.context import ContextAPI

            ctx = ContextAPI.get_context(name)
        except Exception as e:
            return CommandResult.failure(f"Could not load context '{name}': {e}")
        if ctx is None:
            return CommandResult.failure(
                f"Context '{name}' not found", kind=CommandErrorKind.NOT_FOUND
            )

        try:
            sdk = _build_sdk_client_for_context(ctx)
            sdk.ping()
        except Exception as e:
            logger.warning("Switch to context %s failed: %s", name, e)
            return CommandResult.failure(
                f"Could not connect via context '{name}': {e}",
                kind=CommandErrorKind.DAEMON_UNREACHABLE,
            )

        self._sdk = sdk
        self._fetcher = DockerResourceFetcher(sdk)
        self._context_override = name
        self.connection = ConnectionState(
            status=ConnectionStatus.CONNECTED,
            message="Connected",
            hint="",
            context=ctx.Name,
            host=ctx.Host or _DEFAULT_DOCKER_SOCK,
        )
        _save_last_context(name)
        logger.info("Switched Docker context to %s (%s)", name, ctx.Host)
        return CommandResult.success(f"Switched to context '{name}'")

    def stop_container(self, container_id: str) -> CommandResult:
        return self._run_management_command(
            lambda sdk: sdk.containers.get(container_id).stop(), "OK"
        )

    def start_container(self, container_id: str) -> CommandResult:
        return self._run_management_command(
            lambda sdk: sdk.containers.get(container_id).start(), "OK"
        )

    def restart_container(self, container_id: str) -> CommandResult:
        return self._run_management_command(
            lambda sdk: sdk.containers.get(container_id).restart(), "OK"
        )

    def remove_container(self, container_id: str, force: bool = False) -> CommandResult:
        return self._run_management_command(
            lambda sdk: sdk.containers.get(container_id).remove(force=force), "OK"
        )

    def remove_image(self, image_id: str, force: bool = False) -> CommandResult:
        return self._run_management_command(
            lambda sdk: sdk.images.remove(image=image_id, force=force), "OK"
        )

    def remove_volume(self, volume_name: str) -> CommandResult:
        return self._run_management_command(
            lambda sdk: sdk.volumes.get(volume_name).remove(), "OK"
        )

    def remove_network(self, network_name: str) -> CommandResult:
        return self._run_management_command(
            lambda sdk: sdk.networks.get(network_name).remove(), "OK"
        )

    def tag_image(
        self, image_id: str, repository: str, tag: str = "latest"
    ) -> CommandResult:
        def op(sdk: "docker.DockerClient") -> CommandResult:
            ok = sdk.images.get(image_id).tag(repository, tag=tag)
            if not ok:
                return CommandResult.failure(
                    f"Docker rejected tag {repository}:{tag}",
                    kind=CommandErrorKind.UNKNOWN,
                )
            return CommandResult.success(f"Tagged {repository}:{tag}")

        return self._run_management_op(op)

    def create_volume(
        self,
        name: str,
        driver: str = "local",
        labels: dict[str, str] | None = None,
    ) -> CommandResult:
        def op(sdk: "docker.DockerClient") -> CommandResult:
            vol = sdk.volumes.create(
                name=name or None, driver=driver or "local", labels=labels or {}
            )
            return CommandResult.success(f"Created volume {vol.name}")

        return self._run_management_op(op)

    def create_network(
        self, name: str, driver: str = "bridge", subnet: str = ""
    ) -> CommandResult:
        def op(sdk: "docker.DockerClient") -> CommandResult:
            ipam = None
            if subnet:
                pool = IPAMPool(subnet=subnet)
                ipam = IPAMConfig(pool_configs=[pool])
            net = sdk.networks.create(name=name, driver=driver or "bridge", ipam=ipam)
            return CommandResult.success(f"Created network {net.name}")

        return self._run_management_op(op)

    def connect_container(self, network_name: str, container_id: str) -> CommandResult:
        return self._run_management_command(
            lambda sdk: sdk.networks.get(network_name).connect(container_id),
            "Connected",
        )

    def disconnect_container(
        self, network_name: str, container_id: str, force: bool = True
    ) -> CommandResult:
        return self._run_management_command(
            lambda sdk: sdk.networks.get(network_name).disconnect(
                container_id, force=force
            ),
            "Disconnected",
        )

    def pause_container(self, container_id: str) -> CommandResult:
        return self._run_management_command(
            lambda sdk: sdk.containers.get(container_id).pause(), "OK"
        )

    def unpause_container(self, container_id: str) -> CommandResult:
        return self._run_management_command(
            lambda sdk: sdk.containers.get(container_id).unpause(), "OK"
        )

    def kill_container(self, container_id: str) -> CommandResult:
        return self._run_management_command(
            lambda sdk: sdk.containers.get(container_id).kill(), "OK"
        )

    def prune_containers(self) -> CommandResult:
        def op(sdk: "docker.DockerClient") -> CommandResult:
            report = sdk.containers.prune()
            deleted = report.get("ContainersDeleted") or []
            reclaimed = report.get("SpaceReclaimed", 0) or 0
            return CommandResult.success(
                f"Pruned {len(deleted)} container(s) — "
                f"reclaimed {format_size(reclaimed)}"
            )

        return self._run_management_op(op)

    def prune_images(self) -> CommandResult:
        def op(sdk: "docker.DockerClient") -> CommandResult:
            report = sdk.images.prune(filters={"dangling": True})
            deleted = report.get("ImagesDeleted") or []
            reclaimed = report.get("SpaceReclaimed", 0) or 0
            return CommandResult.success(
                f"Pruned {len(deleted)} image(s) — reclaimed {format_size(reclaimed)}"
            )

        return self._run_management_op(op)

    def prune_volumes(self) -> CommandResult:
        def op(sdk: "docker.DockerClient") -> CommandResult:
            report = sdk.volumes.prune()
            deleted = report.get("VolumesDeleted") or []
            reclaimed = report.get("SpaceReclaimed", 0) or 0
            return CommandResult.success(
                f"Pruned {len(deleted)} volume(s) — reclaimed {format_size(reclaimed)}"
            )

        return self._run_management_op(op)

    def prune_networks(self) -> CommandResult:
        def op(sdk: "docker.DockerClient") -> CommandResult:
            report = sdk.networks.prune()
            deleted = report.get("NetworksDeleted") or []
            return CommandResult.success(f"Pruned {len(deleted)} network(s)")

        return self._run_management_op(op)

    def prune_system(self) -> CommandResult:
        """Sequential system-wide prune: containers, networks, dangling images,
        then build cache (best-effort — older docker-py/daemons may lack the
        build-cache prune endpoint).

        Matches `docker system prune` without `--volumes` (the CLI's default):
        volumes are left untouched since they can hold data the user wants to
        keep.
        """

        def op(sdk: "docker.DockerClient") -> CommandResult:
            total_deleted = 0
            total_reclaimed = 0

            containers_report = sdk.containers.prune()
            total_deleted += len(containers_report.get("ContainersDeleted") or [])
            total_reclaimed += containers_report.get("SpaceReclaimed", 0) or 0

            networks_report = sdk.networks.prune()
            total_deleted += len(networks_report.get("NetworksDeleted") or [])

            images_report = sdk.images.prune(filters={"dangling": True})
            total_deleted += len(images_report.get("ImagesDeleted") or [])
            total_reclaimed += images_report.get("SpaceReclaimed", 0) or 0

            try:
                build_report = sdk.api.prune_builds()
                total_reclaimed += build_report.get("SpaceReclaimed", 0) or 0
            except Exception:
                logger.debug("Build cache prune unavailable", exc_info=True)

            return CommandResult.success(
                f"System prune: {total_deleted} item(s) removed — "
                f"reclaimed {format_size(total_reclaimed)}"
            )

        return self._run_management_op(op)

    def inspect_resource(self, kind: str, ref: str) -> dict | None:
        """Full raw attrs for one resource — the `docker inspect` escape hatch.

        `kind` matches `_row_key()`'s first element (container/image/volume/
        network) so callers can pass a row key straight through.
        """
        sdk = self._sdk
        if not sdk:
            return None
        dispatch: dict[str, Callable[[], dict]] = {
            "container": lambda: sdk.containers.get(ref).attrs,
            "image": lambda: sdk.images.get(ref).attrs,
            "volume": lambda: sdk.volumes.get(ref).attrs,
            "network": lambda: sdk.networks.get(ref).attrs,
        }
        getter = dispatch.get(kind)
        if getter is None:
            return None
        try:
            return getter()
        except NotFound:
            logger.warning("Inspect: %s %s not found", kind, ref)
            return None
        except (DockerException, requests.exceptions.RequestException) as e:
            logger.warning("Inspect failed for %s %s: %s", kind, ref, e)
            return None

    def container_top(self, container_id: str) -> ContainerTop | None:
        """Running processes for one container (`docker top`) — `None` if the
        container isn't running or the daemon rejects the request."""
        if not self._sdk:
            return None
        try:
            raw = self._sdk.containers.get(container_id).top()
            return ContainerTop(
                titles=raw.get("Titles", []),
                processes=raw.get("Processes", []),
            )
        except NotFound:
            logger.warning("Top: container %s not found", container_id)
            return None
        except (APIError, DockerException, requests.exceptions.RequestException) as e:
            logger.warning("Top failed for %s: %s", container_id, e)
            return None

    def container_cp(self, src: str, dst: str) -> CommandResult:
        """Copy files in/out of a container (`docker cp <src> <dst>`).

        Third sanctioned subprocess exception (see CLAUDE.md): the SDK only
        exposes raw tar archives (`get_archive`/`put_archive`) — reproducing
        `docker cp`'s directory/trailing-slash semantics and safe tar
        extraction by hand is a large correctness/security surface for a
        convenience feature.
        """
        if shutil.which("docker") is None:
            logger.error("docker cp aborted — docker CLI not found on PATH")
            return CommandResult.failure(
                "docker CLI not found on PATH — cannot copy files",
                kind=CommandErrorKind.DAEMON_UNREACHABLE,
            )

        cmd = ["docker", "cp", src, dst]
        logger.info("Running: %s", " ".join(cmd))
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
        except Exception as e:
            logger.exception("docker cp failed (%s -> %s)", src, dst)
            return CommandResult.failure(
                str(e), kind=CommandErrorKind.DAEMON_UNREACHABLE
            )

        if result.returncode != 0:
            msg = (result.stderr or result.stdout or "").strip()
            msg = msg or f"docker cp exited {result.returncode}"
            logger.warning("docker cp failed (%s -> %s): %s", src, dst, msg)
            return CommandResult.failure(msg, kind=CommandErrorKind.UNKNOWN)

        return CommandResult.success(f"Copied {src} → {dst}")

    def compose_action(
        self,
        project: str,
        verb: str,
        config_files: str = "",
        working_dir: str = "",
    ) -> CommandResult:
        """Run `docker compose <verb>` for a whole project.

        Sanctioned subprocess exception (like exec-shell): docker-py has no
        Compose support, and `up` in particular is impossible via the SDK since
        it must recreate containers from the compose file. `up` therefore passes
        the project's `-f` config files (from labels) and runs from its
        working dir; `down`/`stop`/`start`/`restart` operate on the live project
        by `-p` name alone.
        """
        if shutil.which("docker") is None:
            logger.error("Compose %s aborted — docker CLI not found on PATH", verb)
            return CommandResult.failure(
                "docker CLI not found on PATH — cannot manage Compose projects",
                kind=CommandErrorKind.DAEMON_UNREACHABLE,
            )

        cwd: str | None = None
        if verb == "up":
            cmd = ["docker", "compose"]
            for config_file in filter(None, config_files.split(",")):
                cmd += ["-f", config_file.strip()]
            cmd += ["-p", project, "up", "-d"]
            cwd = working_dir or None
        else:
            cmd = ["docker", "compose", "-p", project, verb]

        logger.info("Running: %s (cwd=%s)", " ".join(cmd), cwd)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
        except Exception as e:
            logger.exception("Compose %s failed for %s", verb, project)
            return CommandResult.failure(
                str(e), kind=CommandErrorKind.DAEMON_UNREACHABLE
            )

        if result.returncode != 0:
            msg = (result.stderr or result.stdout or "").strip()
            msg = msg or f"docker compose {verb} exited {result.returncode}"
            logger.warning("Compose %s failed for %s: %s", verb, project, msg)
            return CommandResult.failure(msg, kind=CommandErrorKind.UNKNOWN)

        return CommandResult.success(f"Compose {verb} succeeded for {project}")

    # --- Internal ---

    def _run_management_op(
        self, op: Callable[["docker.DockerClient"], CommandResult]
    ) -> CommandResult:
        """Run `op` against the SDK client, classifying any raised exception
        into a `CommandResult.failure`. `op` builds its own success result —
        this is the primitive `_run_management_command` and the prune methods
        (which need to report counts/space reclaimed) both sit on top of.
        """
        sdk = self._sdk
        if not sdk:
            logger.error("Management command skipped — Docker client not initialized")
            return CommandResult.failure(
                "Docker client not initialized",
                kind=CommandErrorKind.DAEMON_UNREACHABLE,
            )
        try:
            return op(sdk)
        except NotFound as e:
            logger.warning("Docker resource not found: %s", e)
            return CommandResult.failure(str(e), kind=CommandErrorKind.NOT_FOUND)
        except APIError as e:
            kind = (
                CommandErrorKind.IN_USE
                if e.status_code == 409
                else CommandErrorKind.UNKNOWN
            )
            logger.warning("Docker API error: %s", e)
            return CommandResult.failure(str(e), kind=kind)
        except (DockerException, requests.exceptions.RequestException) as e:
            logger.warning("Docker daemon unreachable: %s", e)
            self.mark_disconnected(e)
            return CommandResult.failure(
                str(e), kind=CommandErrorKind.DAEMON_UNREACHABLE
            )

    def _run_management_command(
        self,
        action_callable: Callable[["docker.DockerClient"], None],
        success_msg: str,
    ) -> CommandResult:
        def op(sdk: "docker.DockerClient") -> CommandResult:
            action_callable(sdk)
            logger.debug("Management command succeeded")
            return CommandResult.success(success_msg)

        return self._run_management_op(op)
