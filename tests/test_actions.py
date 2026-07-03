import unittest

from docksurf_py.actions import EXEC_SHELL_CANDIDATES, select_exec_shell


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


if __name__ == "__main__":
    unittest.main()
