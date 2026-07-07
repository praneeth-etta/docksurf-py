"""Small helpers shared across more than one actions submodule."""

from typing import TYPE_CHECKING, Any

from docksurf_py.models import Image

_PROJECT_HINT = "Select a Compose project (or one of its containers) first"


def _display_name(item: Any) -> str:
    """Human-readable name for an inspect-modal title/notification."""
    if isinstance(item, Image):
        return f"{item.repository}:{item.tag}"
    return getattr(item, "name", str(item))


if TYPE_CHECKING:
    from docksurf_py.app import AppContext

    _Base = AppContext
else:
    # Real runtime base is `object` — `AppContext` only exists for mypy to
    # check every mixin's body against; see app.py's `AppContext` docstring.
    _Base = object
