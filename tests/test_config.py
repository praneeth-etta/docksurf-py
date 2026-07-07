import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docksurf_py.config import Config, load_config


class LoadConfigTests(unittest.TestCase):
    def test_missing_file_at_default_path_scaffolds_and_returns_defaults(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            default_path = Path(tmp) / "docksurf" / "config.toml"
            import docksurf_py.config as config_mod

            with patch.object(config_mod, "DEFAULT_CONFIG_PATH", default_path):
                config = load_config()

            self.assertEqual(config, Config())
            self.assertTrue(default_path.exists())
            self.assertIn("[logs]", default_path.read_text())

    def test_missing_file_at_explicit_path_returns_defaults_without_scaffold(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            explicit_path = Path(tmp) / "custom" / "config.toml"
            config = load_config(explicit_path)
            self.assertEqual(config, Config())
            self.assertFalse(explicit_path.exists())

    def test_valid_toml_overrides_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                "[logs]\n"
                'default_tail = "all"\n'
                "default_since_seconds = 300\n"
                "[confirm]\n"
                "delete = false\n"
                "compose_down = false\n"
                "prune = false\n"
            )
            config = load_config(path)
            self.assertEqual(
                config,
                Config(
                    default_log_tail=None,
                    default_log_since_seconds=300,
                    confirm_delete=False,
                    confirm_compose_down=False,
                    confirm_prune=False,
                ),
            )

    def test_bad_field_values_fall_back_to_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                "[logs]\n"
                'default_tail = "bogus"\n'
                "default_since_seconds = -5\n"
                "[confirm]\n"
                'delete = "yes"\n'
            )
            config = load_config(path)
            self.assertEqual(config, Config())

    def test_malformed_toml_returns_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("this is not [ valid toml")
            config = load_config(path)
            self.assertEqual(config, Config())

    def test_partial_config_only_overrides_given_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("[confirm]\ndelete = false\n")
            config = load_config(path)
            self.assertFalse(config.confirm_delete)
            self.assertTrue(config.confirm_compose_down)
            self.assertTrue(config.confirm_prune)
            self.assertEqual(config.default_log_tail, 500)


if __name__ == "__main__":
    unittest.main()
