"""themes.py — curated `Theme` palettes."""

from textual.theme import Theme

DOCKSURF_OCEAN = Theme(
    name="docksurf-ocean",
    primary="#22D3C0",
    secondary="#2E7DB2",
    accent="#FF8A5B",
    foreground="#E4F3F1",
    background="#061620",
    surface="#0E2430",
    panel="#123044",
    success="#4ADE9B",
    warning="#F2B84B",
    error="#E9505A",
    dark=True,
)

DOCKSURF_NIGHTCITY = Theme(
    name="docksurf-nightcity",
    primary="#FF2E92",
    secondary="#7B2FF7",
    accent="#00E5FF",
    foreground="#E4DCF5",
    background="#0B0714",
    surface="#150F24",
    panel="#1D1530",
    success="#05FFA1",
    warning="#F9F002",
    error="#FF3864",
    dark=True,
)

DOCKSURF_AUGGOLD = Theme(
    name="docksurf-auggold",
    primary="#D4A24C",
    secondary="#5C6773",
    accent="#F5C158",
    foreground="#EDE3D0",
    background="#0C0B09",
    surface="#1A1611",
    panel="#241E16",
    success="#7FA65C",
    warning="#C97A34",
    error="#B23A3A",
    dark=True,
)

CUSTOM_THEMES = [DOCKSURF_OCEAN, DOCKSURF_NIGHTCITY, DOCKSURF_AUGGOLD]

DEFAULT_THEME_NAME = DOCKSURF_OCEAN.name
