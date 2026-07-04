import unittest

from docksurf_py.actions import (
    EXEC_SHELL_CANDIDATES,
    build_cp_paths,
    build_exec_argv,
    select_exec_shell,
)
from tests.test_compose import make_container


class SelectExecShellTests(unittest.TestCase):
    def test_picks_first_available_candidate(self) -> None:
        available = {"bash"}
        shell = select_exec_shell(EXEC_SHELL_CANDIDATES, available.__contains__)
        self.assertEqual(shell, "bash")

    def test_falls_back_to_later_candidate(self) -> None:
        available = {"sh"}
        shell = select_exec_shell(EXEC_SHELL_CANDIDATES, available.__contains__)
        self.assertEqual(shell, "sh")

    def test_returns_none_when_nothing_available(self) -> None:
        shell = select_exec_shell(EXEC_SHELL_CANDIDATES, lambda _: False)
        self.assertIsNone(shell)

    def test_prefers_earlier_candidate_when_both_available(self) -> None:
        shell = select_exec_shell(EXEC_SHELL_CANDIDATES, lambda _: True)
        self.assertEqual(shell, "bash")


class BuildExecArgvTests(unittest.TestCase):
    def test_default_shell_no_user(self) -> None:
        argv = build_exec_argv("abc123", "bash")
        self.assertEqual(argv, ["docker", "exec", "-it", "abc123", "bash"])

    def test_inserts_user_flag(self) -> None:
        argv = build_exec_argv("abc123", "bash", user="1000")
        self.assertEqual(
            argv, ["docker", "exec", "-it", "-u", "1000", "abc123", "bash"]
        )

    def test_splits_multi_token_command(self) -> None:
        argv = build_exec_argv("abc123", "python -c 'print(1)'")
        self.assertEqual(
            argv, ["docker", "exec", "-it", "abc123", "python", "-c", "print(1)"]
        )

    def test_strips_whitespace_from_command_and_user(self) -> None:
        argv = build_exec_argv("abc123", "  bash  ", user="  root  ")
        self.assertEqual(
            argv, ["docker", "exec", "-it", "-u", "root", "abc123", "bash"]
        )

    def test_blank_user_omits_flag(self) -> None:
        argv = build_exec_argv("abc123", "bash", user="   ")
        self.assertEqual(argv, ["docker", "exec", "-it", "abc123", "bash"])

    def test_empty_command_returns_none(self) -> None:
        self.assertIsNone(build_exec_argv("abc123", ""))
        self.assertIsNone(build_exec_argv("abc123", "   "))

    def test_unbalanced_quotes_returns_none(self) -> None:
        self.assertIsNone(build_exec_argv("abc123", "echo 'unterminated"))


def _make_container_with_id(name: str, container_id: str):
    c = make_container(name)
    c.id = container_id
    return c


class BuildCpPathsTests(unittest.TestCase):
    def test_container_side_is_source(self) -> None:
        c = _make_container_with_id("web", "abc123")
        result = build_cp_paths(c, "web:/etc/hosts", "./hosts")
        self.assertEqual(result, ("abc123:/etc/hosts", "./hosts"))

    def test_container_side_is_destination(self) -> None:
        c = _make_container_with_id("web", "abc123")
        result = build_cp_paths(c, "./hosts", "web:/etc/hosts")
        self.assertEqual(result, ("./hosts", "abc123:/etc/hosts"))

    def test_neither_side_prefixed_returns_none(self) -> None:
        c = _make_container_with_id("web", "abc123")
        self.assertIsNone(build_cp_paths(c, "./hosts", "./other"))

    def test_both_sides_prefixed_returns_none(self) -> None:
        c = _make_container_with_id("web", "abc123")
        self.assertIsNone(build_cp_paths(c, "web:/a", "web:/b"))

    def test_blank_source_returns_none(self) -> None:
        c = _make_container_with_id("web", "abc123")
        self.assertIsNone(build_cp_paths(c, "  ", "web:/etc/hosts"))

    def test_blank_destination_returns_none(self) -> None:
        c = _make_container_with_id("web", "abc123")
        self.assertIsNone(build_cp_paths(c, "web:/etc/hosts", "   "))

    def test_strips_whitespace_around_paths(self) -> None:
        c = _make_container_with_id("web", "abc123")
        result = build_cp_paths(c, "  web:/etc/hosts  ", "  ./hosts  ")
        self.assertEqual(result, ("abc123:/etc/hosts", "./hosts"))

    def test_another_containers_name_prefix_does_not_match(self) -> None:
        c = _make_container_with_id("web", "abc123")
        # "webworker:" isn't "web:" -- must not be mistaken for the
        # container prefix just because it starts with the same letters.
        self.assertIsNone(build_cp_paths(c, "webworker:/etc/hosts", "./hosts"))


if __name__ == "__main__":
    unittest.main()
