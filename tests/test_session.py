import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docksurf_py.session import SessionState, load_session, save_session


class SessionPersistenceTests(unittest.TestCase):
    def test_missing_file_returns_empty_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session_file = Path(tmp) / "nested" / "session.json"
            with patch("docksurf_py.session._SESSION_FILE", session_file):
                self.assertEqual(load_session(), SessionState())

    def test_corrupt_file_returns_empty_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session_file = Path(tmp) / "session.json"
            session_file.write_text("not json")
            with patch("docksurf_py.session._SESSION_FILE", session_file):
                self.assertEqual(load_session(), SessionState())

    def test_round_trips_active_tab_and_sort_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session_file = Path(tmp) / "session.json"
            with patch("docksurf_py.session._SESSION_FILE", session_file):
                state = SessionState(
                    active_tab="tab-images",
                    sort_state={"tab-containers": ("Name", False)},
                )
                save_session(state)

                loaded = load_session()
                self.assertEqual(loaded.active_tab, "tab-images")
                self.assertEqual(loaded.sort_state, {"tab-containers": ("Name", False)})
                self.assertEqual(
                    json.loads(session_file.read_text()),
                    {
                        "active_tab": "tab-images",
                        "sort_state": {"tab-containers": ["Name", False]},
                        "theme": None,
                    },
                )

    def test_invalid_active_tab_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session_file = Path(tmp) / "session.json"
            session_file.write_text(json.dumps({"active_tab": "not-a-real-tab"}))
            with patch("docksurf_py.session._SESSION_FILE", session_file):
                self.assertIsNone(load_session().active_tab)

    def test_malformed_sort_entry_is_skipped_others_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session_file = Path(tmp) / "session.json"
            session_file.write_text(
                json.dumps(
                    {
                        "sort_state": {
                            "tab-containers": ["Name", False],
                            "tab-images": "not-a-tuple",
                            "not-a-real-tab": ["Name", True],
                        }
                    }
                )
            )
            with patch("docksurf_py.session._SESSION_FILE", session_file):
                loaded = load_session()
                self.assertEqual(loaded.sort_state, {"tab-containers": ("Name", False)})

    def test_round_trips_theme(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session_file = Path(tmp) / "session.json"
            with patch("docksurf_py.session._SESSION_FILE", session_file):
                save_session(SessionState(theme="docksurf-nightcity"))

                loaded = load_session()
                self.assertEqual(loaded.theme, "docksurf-nightcity")
                self.assertEqual(
                    json.loads(session_file.read_text())["theme"], "docksurf-nightcity"
                )

    def test_missing_theme_key_defaults_to_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session_file = Path(tmp) / "session.json"
            session_file.write_text(json.dumps({"active_tab": "tab-images"}))
            with patch("docksurf_py.session._SESSION_FILE", session_file):
                self.assertIsNone(load_session().theme)

    def test_non_string_theme_is_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session_file = Path(tmp) / "session.json"
            session_file.write_text(json.dumps({"theme": 42}))
            with patch("docksurf_py.session._SESSION_FILE", session_file):
                self.assertIsNone(load_session().theme)

    def test_save_tolerates_unwritable_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # A file where a directory is expected — mkdir(parents=True) fails.
            blocker = Path(tmp) / "blocker"
            blocker.write_text("")
            session_file = blocker / "session.json"
            with patch("docksurf_py.session._SESSION_FILE", session_file):
                save_session(SessionState(active_tab="tab-images"))  # no raise


if __name__ == "__main__":
    unittest.main()
