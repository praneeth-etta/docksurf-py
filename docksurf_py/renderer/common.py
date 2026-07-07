"""Shared typing shim for the renderer mixins."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from docksurf_py.app import AppContext

    _Base = AppContext
else:
    # Real runtime base is `object` — `AppContext` only exists for mypy to
    # check every mixin's body against; see app.py's `AppContext` docstring.
    _Base = object
