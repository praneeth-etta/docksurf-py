"""OS-specific directories for DockSurf's persistent data.

This module is the single source of truth for application data and config
paths. Using `platformdirs` ensures the correct location is used on Linux,
macOS, and Windows instead of hardcoding platform-specific paths.
"""

from pathlib import Path

import platformdirs

CONFIG_DIR: Path = platformdirs.user_config_path("docksurf", appauthor=False)
DATA_DIR: Path = platformdirs.user_data_path("docksurf", appauthor=False)
