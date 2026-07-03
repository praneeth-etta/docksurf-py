import unittest
from unittest.mock import MagicMock, patch

from docksurf_py.docker import DockerClient, _assign_service_colors
from docksurf_py.models import (
    COMPOSE_CONFIG_FILES_LABEL,
    COMPOSE_PROJECT_LABEL,
    COMPOSE_SERVICE_LABEL,
    COMPOSE_WORKING_DIR_LABEL,
    CommandErrorKind,
    ComposeProject,
    Container,
)
from docksurf_py.renderer import _group_by_project


def make_container(
    name: str,
    *,
    running: bool = True,
    project: str = "",
    service: str = "",
    config_files: str = "",
    working_dir: str = "",
) -> Container:
    labels: dict[str, str] = {}
    if project:
        labels[COMPOSE_PROJECT_LABEL] = project
        labels[COMPOSE_SERVICE_LABEL] = service or name
        labels[COMPOSE_CONFIG_FILES_LABEL] = config_files
        labels[COMPOSE_WORKING_DIR_LABEL] = working_dir
    return Container(
        id=name,
        name=name,
        image_id="img",
        image_name="img:latest",
        status="running" if running else "exited",
        state="running" if running else "exited",
        running=running,
        exit_code=0,
        health="",
        ports=[],
        mounts=[],
        networks=[],
        created="",
        env=[],
        labels=labels,
    )


class ContainerComposePropertyTests(unittest.TestCase):
    def test_compose_labels_expose_project_and_service(self) -> None:
        c = make_container(
            "web",
            project="myapp",
            service="web",
            config_files="/srv/myapp/docker-compose.yml",
            working_dir="/srv/myapp",
        )
        self.assertTrue(c.is_compose)
        self.assertEqual(c.compose_project, "myapp")
        self.assertEqual(c.compose_service, "web")
        self.assertEqual(c.compose_config_files, "/srv/myapp/docker-compose.yml")
        self.assertEqual(c.compose_working_dir, "/srv/myapp")

    def test_non_compose_container_has_empty_compose_fields(self) -> None:
        c = make_container("solo")
        self.assertFalse(c.is_compose)
        self.assertEqual(c.compose_project, "")
        self.assertEqual(c.compose_service, "")


class ComposeProjectTests(unittest.TestCase):
    def test_counts_and_all_running(self) -> None:
        project = ComposeProject(
            name="myapp",
            containers=[
                make_container("web", running=True, project="myapp", service="web"),
                make_container("db", running=False, project="myapp", service="db"),
            ],
            config_files="",
            working_dir="",
        )
        self.assertEqual(project.total_count, 2)
        self.assertEqual(project.running_count, 1)
        self.assertFalse(project.all_running)

    def test_all_running_true_when_every_container_up(self) -> None:
        project = ComposeProject(
            name="myapp",
            containers=[make_container("web", running=True, project="myapp")],
            config_files="",
            working_dir="",
        )
        self.assertTrue(project.all_running)


class GroupByProjectTests(unittest.TestCase):
    def test_groups_projects_and_separates_standalone(self) -> None:
        containers = [
            make_container("solo1"),
            make_container("b-web", project="beta", service="web"),
            make_container("a-api", project="alpha", service="api"),
            make_container("a-db", project="alpha", service="db"),
        ]
        projects, standalone = _group_by_project(containers)

        # Projects sorted alphabetically by name.
        self.assertEqual([p.name for p in projects], ["alpha", "beta"])
        # Services sorted by service name within a project.
        self.assertEqual(
            [c.compose_service for c in projects[0].containers], ["api", "db"]
        )
        self.assertEqual([c.name for c in standalone], ["solo1"])

    def test_project_metadata_taken_from_first_service(self) -> None:
        containers = [
            make_container(
                "web",
                project="myapp",
                service="web",
                config_files="/srv/myapp/dc.yml",
                working_dir="/srv/myapp",
            )
        ]
        projects, _ = _group_by_project(containers)
        self.assertEqual(projects[0].config_files, "/srv/myapp/dc.yml")
        self.assertEqual(projects[0].working_dir, "/srv/myapp")

    def test_no_compose_containers_yields_no_projects(self) -> None:
        projects, standalone = _group_by_project(
            [make_container("a"), make_container("b")]
        )
        self.assertEqual(projects, [])
        self.assertEqual(len(standalone), 2)


class AssignServiceColorsTests(unittest.TestCase):
    def test_distinct_services_get_distinct_colors_and_repeat_is_stable(self) -> None:
        colors = _assign_service_colors(["web", "db", "web"])
        self.assertEqual(colors["web"], colors["web"])
        self.assertNotEqual(colors["web"], colors["db"])


class ComposeActionCommandTests(unittest.TestCase):
    def _client(self) -> DockerClient:
        client = DockerClient()
        client._sdk = MagicMock()
        return client

    def test_up_uses_config_files_and_working_dir(self) -> None:
        client = self._client()
        with (
            patch("docksurf_py.docker.shutil.which", return_value="/usr/bin/docker"),
            patch("docksurf_py.docker.subprocess.run") as run,
        ):
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = client.compose_action(
                "myapp",
                "up",
                config_files="/srv/myapp/a.yml,/srv/myapp/b.yml",
                working_dir="/srv/myapp",
            )
        cmd = run.call_args.args[0]
        self.assertEqual(
            cmd,
            [
                "docker",
                "compose",
                "-f",
                "/srv/myapp/a.yml",
                "-f",
                "/srv/myapp/b.yml",
                "-p",
                "myapp",
                "up",
                "-d",
            ],
        )
        self.assertEqual(run.call_args.kwargs["cwd"], "/srv/myapp")
        self.assertTrue(result.ok)

    def test_stop_uses_project_name_only(self) -> None:
        client = self._client()
        with (
            patch("docksurf_py.docker.shutil.which", return_value="/usr/bin/docker"),
            patch("docksurf_py.docker.subprocess.run") as run,
        ):
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            client.compose_action("myapp", "stop")
        cmd = run.call_args.args[0]
        self.assertEqual(cmd, ["docker", "compose", "-p", "myapp", "stop"])
        self.assertIsNone(run.call_args.kwargs["cwd"])

    def test_missing_docker_cli_is_daemon_unreachable(self) -> None:
        client = self._client()
        with patch("docksurf_py.docker.shutil.which", return_value=None):
            result = client.compose_action("myapp", "down")
        self.assertFalse(result.ok)
        self.assertEqual(result.kind, CommandErrorKind.DAEMON_UNREACHABLE)

    def test_nonzero_exit_reports_failure_with_stderr(self) -> None:
        client = self._client()
        with (
            patch("docksurf_py.docker.shutil.which", return_value="/usr/bin/docker"),
            patch("docksurf_py.docker.subprocess.run") as run,
        ):
            run.return_value = MagicMock(
                returncode=1, stdout="", stderr="no such project"
            )
            result = client.compose_action("myapp", "down")
        self.assertFalse(result.ok)
        self.assertEqual(result.message, "no such project")
        self.assertEqual(result.kind, CommandErrorKind.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
