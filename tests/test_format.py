"""Unit tests for docker/format.py's format_env — secret masking in the
detail pane's env var display. See ROBUSTNESS_PERF_P2_PLAN.md §4."""

import unittest

from docksurf_py.docker import format_env


class FormatEnvTests(unittest.TestCase):
    def test_masks_password_key_case_insensitively(self) -> None:
        self.assertEqual(
            format_env(["db_Password=supersecret"]), "db_Password=••••••••"
        )

    def test_masks_token_secret_key_credential_keys(self) -> None:
        env = [
            "API_TOKEN=abc123",
            "APP_SECRET=xyz",
            "SECRET_KEY=zzz",
            "AWS_CREDENTIAL_FILE=/root/.aws",
        ]
        for line in format_env(env).splitlines():
            self.assertTrue(line.endswith("=••••••••"), line)

    def test_leaves_ordinary_vars_untouched(self) -> None:
        env = ["PATH=/usr/bin:/bin", "NODE_ENV=production", "LANG=en_US.UTF-8"]
        self.assertEqual(format_env(env), "\n".join(env))

    def test_over_inclusive_false_positive_is_accepted(self) -> None:
        # KEYBOARD_LAYOUT matches "KEY" as a substring — intentional
        # over-masking (see format_env's docstring): a false positive just
        # masks a harmless value, a false negative would leak a real secret.
        self.assertEqual(format_env(["KEYBOARD_LAYOUT=us"]), "KEYBOARD_LAYOUT=••••••••")

    def test_reveal_shows_everything_unmasked(self) -> None:
        env = ["DB_PASSWORD=supersecret", "PATH=/bin"]
        self.assertEqual(format_env(env, reveal=True), "\n".join(env))

    def test_malformed_entry_without_equals_passes_through(self) -> None:
        self.assertEqual(format_env(["JUST_A_FLAG"]), "JUST_A_FLAG")

    def test_empty_list_returns_empty_string(self) -> None:
        self.assertEqual(format_env([]), "")

    def test_multiple_lines_joined_with_newline(self) -> None:
        env = ["PATH=/bin", "DB_PASSWORD=secret"]
        self.assertEqual(format_env(env), "PATH=/bin\nDB_PASSWORD=••••••••")


if __name__ == "__main__":
    unittest.main()
