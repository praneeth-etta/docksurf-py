import os
import unittest
from unittest.mock import MagicMock, patch

from docksurf_py.app import DockSurfApp
from docksurf_py.docker import (
    DockerResourceFetcher,
    _parse_stats,
    _parse_system_df,
    format_uptime,
)
from docksurf_py.models import (
    ContainerStats,
    ContainerTop,
    DiskUsageEntry,
    DockerSnapshot,
    SystemDf,
)
from docksurf_py.observability import _render_df, _render_stats, _render_top
from docksurf_py.widgets import SystemDfScreen
from tests.test_app import EMPTY_SNAPSHOT, MockDockerService, wait_until
from tests.test_compose import make_container


class ParseStatsTests(unittest.TestCase):
    def test_computes_cpu_mem_net_block(self) -> None:
        sample = {
            "cpu_stats": {
                "cpu_usage": {"total_usage": 2000, "percpu_usage": [1, 1]},
                "system_cpu_usage": 10000,
                "online_cpus": 2,
            },
            "precpu_stats": {
                "cpu_usage": {"total_usage": 1000},
                "system_cpu_usage": 5000,
            },
            "memory_stats": {
                "usage": 200,
                "limit": 1000,
                "stats": {"inactive_file": 50},
            },
            "networks": {"eth0": {"rx_bytes": 100, "tx_bytes": 40}},
            "blkio_stats": {
                "io_service_bytes_recursive": [
                    {"op": "Read", "value": 10},
                    {"op": "Write", "value": 5},
                ]
            },
        }
        stats = _parse_stats(sample)
        # cpu_delta=1000, system_delta=5000, online=2 -> 1000/5000 * 2 * 100 = 40%
        self.assertAlmostEqual(stats.cpu_percent, 40.0)
        self.assertEqual(stats.mem_used, 150)  # 200 - 50 inactive_file
        self.assertAlmostEqual(stats.mem_percent, 15.0)
        self.assertEqual((stats.net_rx, stats.net_tx), (100, 40))
        self.assertEqual((stats.blk_read, stats.blk_write), (10, 5))

    def test_zero_system_delta_is_zero_cpu(self) -> None:
        stats = _parse_stats({"cpu_stats": {}, "precpu_stats": {}, "memory_stats": {}})
        self.assertEqual(stats.cpu_percent, 0.0)
        self.assertEqual(stats.mem_percent, 0.0)


class FormatUptimeTests(unittest.TestCase):
    def test_empty_and_zero_time_are_dash(self) -> None:
        self.assertEqual(format_uptime(""), "—")
        self.assertEqual(format_uptime("0001-01-01T00:00:00Z"), "—")

    def test_past_timestamp_has_no_ago_suffix(self) -> None:
        out = format_uptime("2000-01-01T00:00:00Z")
        self.assertNotEqual(out, "—")
        self.assertNotIn("ago", out)


class ParseSystemDfTests(unittest.TestCase):
    def test_totals_and_reclaimable(self) -> None:
        raw = {
            "Images": [
                {"Size": 100, "Containers": 1},
                {"Size": 200, "Containers": 0},
            ],
            "Containers": [
                {"SizeRw": 10, "State": "running"},
                {"SizeRw": 20, "State": "exited"},
            ],
            "Volumes": [
                {"UsageData": {"Size": 50, "RefCount": 1}},
                {"UsageData": {"Size": 70, "RefCount": 0}},
            ],
            "BuildCache": [
                {"Size": 30, "InUse": False},
                {"Size": 40, "InUse": True},
            ],
        }
        df = _parse_system_df(raw)
        by_kind = {e.kind: e for e in df.entries}
        self.assertEqual(by_kind["Images"].size_bytes, 300)
        self.assertEqual(by_kind["Images"].reclaimable_bytes, 200)
        self.assertEqual(by_kind["Images"].active_count, 1)
        self.assertEqual(by_kind["Containers"].reclaimable_bytes, 20)
        self.assertEqual(by_kind["Local Volumes"].reclaimable_bytes, 70)
        self.assertEqual(by_kind["Build Cache"].reclaimable_bytes, 30)
        self.assertEqual(df.total_size, 520)
        self.assertEqual(df.total_reclaimable, 320)


class GetContainersFieldsTests(unittest.TestCase):
    def test_maps_started_at_restart_count_and_health_log(self) -> None:
        attrs = {
            "Id": "abc123" + "0" * 58,
            "Name": "/web",
            "Image": "sha256:deadbeef",
            "Created": "2026-07-01T00:00:00Z",
            "RestartCount": 3,
            "State": {
                "Status": "running",
                "Running": True,
                "ExitCode": 0,
                "StartedAt": "2026-07-02T09:00:00Z",
                "Health": {
                    "Status": "unhealthy",
                    "Log": [
                        {
                            "Start": "2026-07-02T09:05:00Z",
                            "ExitCode": 1,
                            "Output": "boom\n",
                        },
                    ],
                },
            },
            "Config": {"Env": [], "Labels": {}},
            "NetworkSettings": {"Ports": {}, "Networks": {}},
            "Mounts": [],
        }
        sdk = MagicMock()
        sdk.api.containers.return_value = [{"Id": attrs["Id"]}]
        sdk.api.inspect_container.return_value = attrs
        sdk.api.images.return_value = [
            {"Id": "sha256:deadbeef", "RepoTags": ["nginx:latest"]}
        ]
        containers = DockerResourceFetcher(sdk).get_containers()
        self.assertEqual(len(containers), 1)
        got = containers[0]
        self.assertEqual(got.name, "web")
        self.assertEqual(got.image_name, "nginx:latest")
        self.assertEqual(got.started_at, "2026-07-02T09:00:00Z")
        self.assertEqual(got.restart_count, 3)
        self.assertEqual(got.health, "unhealthy")
        self.assertEqual(len(got.health_log), 1)
        self.assertEqual(got.health_log[0].exit_code, 1)
        self.assertEqual(got.health_log[0].output, "boom")


class EventNoiseFilterTests(unittest.TestCase):
    """The noise filter must match the action *prefix* (Docker appends the
    healthcheck command, e.g. 'exec_create: /bin/sh -c echo ok')."""

    def setUp(self) -> None:
        from docksurf_py.renderer import SnapshotManager

        self.mgr = SnapshotManager()

    def test_exec_events_with_command_suffix_are_noise(self) -> None:
        self.assertTrue(self.mgr._is_noise_event("exec_create: /bin/sh -c echo ok"))
        self.assertTrue(self.mgr._is_noise_event("exec_start: /bin/sh -c echo ok"))
        self.assertTrue(self.mgr._is_noise_event("exec_die"))

    def test_periodic_health_status_is_noise(self) -> None:
        self.assertTrue(self.mgr._is_noise_event("health_status: healthy"))
        self.assertTrue(self.mgr._is_noise_event("health_status: unhealthy"))

    def test_real_state_changes_are_not_noise(self) -> None:
        for action in ("start", "die", "stop", "create", "destroy", "pull"):
            self.assertFalse(self.mgr._is_noise_event(action), action)


class CreateSdkClientContextTests(unittest.TestCase):
    """docker.from_env() ignores `docker context`; _create_sdk_client fixes that."""

    def test_docker_host_env_takes_precedence(self) -> None:
        from docksurf_py.docker import context as dockmod

        with (
            patch.dict(os.environ, {"DOCKER_HOST": "tcp://1.2.3.4:2375"}),
            patch.object(dockmod.docker, "from_env") as from_env,
        ):
            dockmod._create_sdk_client()
        from_env.assert_called_once()

    def test_non_default_context_uses_its_endpoint(self) -> None:
        from docksurf_py.docker import context as dockmod

        fake_ctx = MagicMock(Name="colima", Host="unix:///home/u/.colima/docker.sock")
        fake_ctx.TLSConfig = None
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "docker.context.ContextAPI.get_current_context", return_value=fake_ctx
            ),
            patch.object(dockmod.docker, "DockerClient") as dc,
            patch.object(dockmod.docker, "from_env") as from_env,
        ):
            dockmod._create_sdk_client()
        dc.assert_called_once()
        self.assertEqual(
            dc.call_args.kwargs["base_url"], "unix:///home/u/.colima/docker.sock"
        )
        from_env.assert_not_called()

    def test_default_context_falls_back_to_from_env(self) -> None:
        from docksurf_py.docker import context as dockmod

        fake_ctx = MagicMock(Name="default", Host="unix:///var/run/docker.sock")
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "docker.context.ContextAPI.get_current_context", return_value=fake_ctx
            ),
            patch.object(dockmod.docker, "from_env") as from_env,
        ):
            dockmod._create_sdk_client()
        from_env.assert_called_once()


class RenderSmokeTests(unittest.TestCase):
    def test_render_stats_and_df_do_not_raise(self) -> None:
        stats = ContainerStats(42.0, 100, 200, 50.0, 1, 2, 3, 4)
        self.assertIsNotNone(_render_stats(stats, "web"))
        df = SystemDf(
            entries=[DiskUsageEntry("Images", 1, 0, 100, 100)],
            total_size=100,
            total_reclaimable=100,
        )
        self.assertIsNotNone(_render_df(df))

    def test_render_top_does_not_raise_and_uses_all_titles(self) -> None:
        top = ContainerTop(
            titles=["PID", "USER", "CMD"],
            processes=[["1", "root", "python app.py"], ["7", "root", "sh"]],
        )
        panel = _render_top(top, "web")
        self.assertIsNotNone(panel)
        table = panel.renderable
        self.assertEqual([c.header for c in table.columns], ["PID", "USER", "CMD"])
        self.assertEqual(table.row_count, 2)

    def test_render_top_handles_no_processes(self) -> None:
        top = ContainerTop(titles=["PID", "CMD"], processes=[])
        self.assertIsNotNone(_render_top(top, "web"))


class _OneEventStream:
    """Yields a single container event, then ends (mirrors EventStream shape)."""

    error = None

    def __iter__(self):
        yield {"Type": "container", "Action": "start"}

    def stop(self) -> None:
        pass


class _OneStatsStream:
    def __iter__(self):
        yield ContainerStats(42.0, 100, 200, 50.0, 1, 2, 3, 4)

    def stop(self) -> None:
        pass


class LiveObservabilityAppTests(unittest.IsolatedAsyncioTestCase):
    async def test_docker_event_triggers_refresh_without_manual_r(self) -> None:
        count = {"n": 0}

        def fetch() -> DockerSnapshot:
            count["n"] += 1
            return EMPTY_SNAPSHOT

        svc = MockDockerService(fetch)
        svc.stream_events = lambda: _OneEventStream()  # type: ignore[method-assign]
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            initial = count["n"]
            await wait_until(lambda: count["n"] > initial, timeout=3.0)
            self.assertGreater(count["n"], initial)

    async def test_selecting_running_container_starts_stats_stream(self) -> None:
        snap = DockerSnapshot([make_container("web", running=True)], [], [], [])
        svc = MockDockerService(lambda: snap)
        svc.stream_stats = lambda cid: _OneStatsStream()  # type: ignore[method-assign]
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            await wait_until(lambda: app._stats_target == "web", timeout=2.0)
            self.assertEqual(app._stats_target, "web")

    async def test_stopped_container_has_no_stats_target(self) -> None:
        snap = DockerSnapshot([make_container("web", running=False)], [], [], [])
        svc = MockDockerService(lambda: snap)
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.pause()
            self.assertIsNone(app._stats_target)

    async def test_refresh_preserves_selection(self) -> None:
        from textual.widgets import DataTable

        from docksurf_py.constants import TabID, TableID

        snap = DockerSnapshot(
            [
                make_container("a", running=False),
                make_container("b", running=False),
                make_container("c", running=False),
            ],
            [],
            [],
            [],
        )
        count = {"n": 0}

        def fetch() -> DockerSnapshot:
            count["n"] += 1
            return snap

        app = DockSurfApp(docker=MockDockerService(fetch))
        async with app.run_test() as pilot:
            await pilot.pause()
            await wait_until(
                lambda: len(app._current.get(TabID.CONTAINERS, [])) == 3, timeout=2.0
            )
            table = app.query_one(f"#{TableID.CONTAINERS}", DataTable)
            table.move_cursor(row=1)
            await pilot.pause()
            self.assertEqual(app._get_focused_container().id, "b")

            before = count["n"]
            app.action_refresh()
            await wait_until(lambda: count["n"] > before, timeout=2.0)
            await pilot.pause()
            # A refresh must NOT reset the cursor back to row 0 ("a").
            self.assertEqual(table.cursor_row, 1)
            self.assertEqual(app._get_focused_container().id, "b")

    async def test_system_df_screen_opens(self) -> None:
        svc = MockDockerService(lambda: EMPTY_SNAPSHOT)
        svc.system_df = lambda: SystemDf(  # type: ignore[method-assign]
            entries=[DiskUsageEntry("Images", 1, 0, 100, 80)],
            total_size=100,
            total_reclaimable=80,
        )
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.action_system_df()
            await wait_until(
                lambda: any(isinstance(s, SystemDfScreen) for s in app.screen_stack),
                timeout=3.0,
            )
            self.assertTrue(
                any(isinstance(s, SystemDfScreen) for s in app.screen_stack)
            )

    async def test_container_top_fetches_then_toggles_off(self) -> None:
        snap = DockerSnapshot([make_container("web", running=True)], [], [], [])
        svc = MockDockerService(lambda: snap)

        def fake_container_top(cid: str) -> ContainerTop:
            svc.calls.append(("container_top", cid))
            return ContainerTop(titles=["PID", "CMD"], processes=[["1", "python"]])

        svc.container_top = fake_container_top  # type: ignore[method-assign]
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            await wait_until(lambda: app._get_focused_container() is not None)

            app.action_container_top()
            # `_top_target` is set synchronously (before the threaded fetch
            # even starts) — wait on the actual fetch call, not the target.
            await wait_until(lambda: ("container_top", "web") in svc.calls, timeout=2.0)
            self.assertEqual(app._top_target, "web")

            # Pressing `t` again on the same container toggles it off.
            app.action_container_top()
            await pilot.pause()
            self.assertIsNone(app._top_target)

    async def test_container_top_guarded_when_not_running(self) -> None:
        snap = DockerSnapshot([make_container("web", running=False)], [], [], [])
        app = DockSurfApp(docker=MockDockerService(lambda: snap))
        async with app.run_test() as pilot:
            await pilot.pause()
            await wait_until(lambda: app._get_focused_container() is not None)

            app.action_container_top()
            await pilot.pause()
            self.assertIsNone(app._top_target)

    async def test_selecting_different_container_clears_top(self) -> None:
        from textual.widgets import DataTable

        from docksurf_py.constants import TabID, TableID

        snap = DockerSnapshot(
            [
                make_container("web", running=True),
                make_container("worker", running=True),
            ],
            [],
            [],
            [],
        )
        svc = MockDockerService(lambda: snap)
        svc.container_top = lambda cid: ContainerTop(  # type: ignore[method-assign]
            titles=["PID"], processes=[["1"]]
        )
        app = DockSurfApp(docker=svc)
        async with app.run_test() as pilot:
            await pilot.pause()
            await wait_until(
                lambda: len(app._current.get(TabID.CONTAINERS, [])) == 2, timeout=2.0
            )
            table = app.query_one(f"#{TableID.CONTAINERS}", DataTable)
            table.move_cursor(row=0)
            await pilot.pause()

            app.action_container_top()
            await wait_until(lambda: app._top_target == "web", timeout=2.0)

            table.move_cursor(row=1)
            await pilot.pause()
            self.assertIsNone(app._top_target)


if __name__ == "__main__":
    unittest.main()
