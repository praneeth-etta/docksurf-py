"""OS-specific directories for DockSurf's persistent data.

This module is the single source of truth for application data and config
paths. Using `platformdirs` ensures the correct location is used on Linux,
macOS, and Windows instead of hardcoding platform-specific paths.

`CONFIG_DIR` intentionally uses the existing app name ("docksurf"), while
`DATA_DIR` uses "docksurf-py". This mismatch is preserved for backward
compatibility because changing it would relocate existing users' config/data.
"""

from pathlib import Path

import platformdirs

CONFIG_DIR: Path = platformdirs.user_config_path("docksurf", appauthor=False)
DATA_DIR: Path = platformdirs.user_data_path("docksurf-py", appauthor=False)
