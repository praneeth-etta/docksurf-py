"""themes.py — curated `Theme` palettes."""

from textual.theme import Theme

DOCKSURF_NIGHTCITY = Theme(
    name="docksurf-nightcity",
    primary="#2B2D2DFF",
    secondary="#4A4159",
    accent="#3D5B5E",
    foreground="#E4DCF5",
    background="#0D0C0D",
    surface="#3F2C3E",
    panel="#141317",
    success="#639B65",
    warning="#B0916F",
    error="#B35959",
    dark=True,
    variables={
        "scrollbar-background": "#221E24",
        "scrollbar": "#6E6178",
        "scrollbar-hover": "#8A7B94",
        "scrollbar-active": "#B3A3C2",
    },
)

DOCKSURF_LINEN = Theme(
    name="docksurf-linen",
    primary="#A6875D",
    secondary="#6E6A63",
    accent="#8C6239",
    foreground="#2A241C",
    background="#F6F3EC",
    surface="#EDE6D8",
    panel="#E2D9C6",
    success="#3F6B3F",
    warning="#8A5A2E",
    error="#8B3A3A",
    dark=False,
)

DOCKSURF_FOREST = Theme(
    name="docksurf-forest",
    primary="#4CAF7D",
    secondary="#C9A66B",
    accent="#5EC8E0",
    foreground="#E6F1E9",
    background="#0A130E",
    surface="#111F17",
    panel="#182B1F",
    success="#7FD858",
    warning="#F2A65A",
    error="#E85D5D",
    dark=True,
)

DOCKSURF_SAKURA = Theme(
    name="docksurf-sakura",
    primary="#E7B6C3",
    secondary="#5B4B8A",
    accent="#A0A7BA",
    foreground="#2B1B22",
    background="#FBCAD7",
    surface="#F3E4E9",
    panel="#ECD8DF",
    success="#1B7A43",
    warning="#C9A27F",
    error="#A31D1D",
    dark=False,
)

DOCKSURF_ABYSS = Theme(
    name="docksurf-abyss",
    primary="#5A8EC4",
    secondary="#8172B8",
    accent="#F03DAE",
    foreground="#C9D9E8",
    background="#03050A",
    surface="#080D16",
    panel="#0D1420",
    success="#3FC7D9",
    warning="#B5C23D",
    error="#C6634A",
    dark=True,
)

CUSTOM_THEMES = [
    DOCKSURF_NIGHTCITY,
    DOCKSURF_LINEN,
    DOCKSURF_FOREST,
    DOCKSURF_SAKURA,
    DOCKSURF_ABYSS,
]

DEFAULT_THEME_NAME = DOCKSURF_ABYSS.name

TERMINAL_NATIVE_THEME_NAMES = ["ansi-dark", "ansi-light"]

THEME_CYCLE_NAMES = [
    theme.name for theme in CUSTOM_THEMES
] + TERMINAL_NATIVE_THEME_NAMES
