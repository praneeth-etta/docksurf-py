"""ResourceDeletionHandler — confirmation dialogs and dispatched remove calls."""

import logging
from dataclasses import dataclass
from typing import Callable

from rich.markup import escape
from textual import work
from textual.widgets import TabbedContent

from docksurf_py.actions.common import _Base, _display_name
from docksurf_py.constants import TabID
from docksurf_py.models import (
    CommandErrorKind,
    CommandResult,
    Container,
    Image,
    Network,
    Volume,
)
from docksurf_py.widgets import ConfirmDialog

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeletePlan:
    """What to confirm and run to delete one focused resource.

    `force_default=None` renders a plain Confirm/Cancel dialog and `command`
    is always called with `force=False`. A `bool` shows a "Force" checkbox
    pre-checked to that default; `command` is then called with whatever the
    user leaves the checkbox at. Bulk delete (no interactive checkbox per
    row) always calls `command` with the plan's own `force_default`.
    """

    confirm_message: str
    command: Callable[[bool], CommandResult]
    success_message: str
    force_default: bool | None = None


def _bind_delete_command(plan: DeletePlan, force: bool) -> Callable[[], CommandResult]:
    """Bind a `DeletePlan.command` to a fixed `force`, for non-interactive
    (bulk) delete jobs that can't show a per-row checkbox."""
    return lambda: plan.command(force)


class ResourceDeletionHandler(_Base):
    """Confirmation dialogs and dispatched remove calls for all resource types.

    Per-resource behavior (the confirm message, force-flag logic, and any
    pre-condition guard) lives in the `_plan_*_delete` methods below, wired
    into `self._resource_registry` by `DockSurfApp` — `action_delete` itself
    no longer branches on which tab is active.
    """

    def _apply_if_confirmed(
        self,
        confirmed: bool,
        force: bool,
        command_fn: Callable[[bool], CommandResult],
        success_msg: str,
    ) -> None:
        if not confirmed:
            logger.debug("Deletion cancelled by user")
            return
        result = command_fn(force)
        if result.ok:
            logger.info("%s", success_msg)
            self.notify(success_msg)
            self.start_refresh()
        else:
            logger.warning("Delete failed: %s", result.message)
            self.notify(f"Error: {result.message}", severity="error")
            if result.kind is CommandErrorKind.NOT_FOUND:
                # Our snapshot is stale — the resource is already gone.
                self.start_refresh()

    def _plan_container_delete(self, c: Container) -> DeletePlan | None:
        is_running = c.running
        msg = (
            f"Force-remove RUNNING container '{escape(c.name)}'?"
            if is_running
            else f"Remove container '{escape(c.name)}'?"
        )
        return DeletePlan(
            confirm_message=msg,
            command=lambda force: self.docker.remove_container(c.id, force=force),
            success_message=f"Removed container: {escape(c.name)}",
            force_default=is_running,
        )

    def _plan_image_delete(self, img: Image) -> DeletePlan | None:
        in_use = bool(img.used_by)
        img_label = f"{escape(img.repository)}:{escape(img.tag)}"
        msg = (
            f"Force-remove IN-USE image '{img_label}'?"
            if in_use
            else f"Remove image '{img_label}'?"
        )
        return DeletePlan(
            confirm_message=msg,
            command=lambda force: self.docker.remove_image(img.id, force=force),
            success_message=f"Removed image {img_label}",
            force_default=in_use,
        )

    def _plan_volume_delete(self, vol: Volume) -> DeletePlan | None:
        if vol.used_by:
            # Docker's volume-remove `force` only suppresses a "not found"
            # error — it does not override an in-use guard, so there's no
            # honest "Force" checkbox to offer here. Name the blockers instead.
            blockers = ", ".join(escape(name) for name in vol.used_by)
            self.notify(
                f"Volume '{escape(vol.name)}' is in use by {blockers} — "
                "stop them first",
                severity="warning",
            )
            return None
        return DeletePlan(
            confirm_message=f"Remove volume '{escape(vol.name)}'?",
            command=lambda force: self.docker.remove_volume(vol.name),
            success_message=f"Removed volume {escape(vol.name)}",
        )

    def _plan_network_delete(self, net: Network) -> DeletePlan | None:
        if net.name in ("bridge", "host", "none"):
            self.notify(
                f"Cannot remove built-in network '{escape(net.name)}'",
                severity="warning",
            )
            return None

        def _remove(force: bool) -> CommandResult:
            # The Docker API has no real "force remove" for networks — every
            # attached endpoint must be disconnected first.
            if force:
                for endpoint in net.endpoints:
                    self.docker.disconnect_container(net.name, endpoint.container_name)
            return self.docker.remove_network(net.name)

        has_endpoints = bool(net.endpoints)
        msg = (
            f"Force-remove network '{escape(net.name)}'? This disconnects "
            f"{len(net.endpoints)} attached container(s) first."
            if has_endpoints
            else f"Remove network '{escape(net.name)}'?"
        )
        return DeletePlan(
            confirm_message=msg,
            command=_remove,
            success_message=f"Removed network {escape(net.name)}",
            force_default=True if has_endpoints else None,
        )

    @work
    async def action_delete(self) -> None:
        if not self.snapshot:
            return
        active = self.query_one(TabbedContent).active
        entry = self._resource_registry.get(active)
        if entry is None:
            return

        if self._marked.get(active):
            await self._bulk_delete(active)
            return

        item = self._get_focused_resource(active)
        if item is None:
            self.notify(f"No {entry.label} selected", severity="warning")
            return

        plan = entry.plan_delete(item)
        if plan is None:
            return

        if plan.force_default is None:
            confirmed = await self.push_screen_wait(ConfirmDialog(plan.confirm_message))
            force = False
        else:
            confirmed, force = await self.push_screen_wait(
                ConfirmDialog(plan.confirm_message, force_default=plan.force_default)
            )
        self._apply_if_confirmed(confirmed, force, plan.command, plan.success_message)

    async def _bulk_delete(self, tab_id: TabID) -> None:
        """Delete every marked resource on `tab_id` behind one confirm dialog.

        Reuses each item's existing `plan_delete` for its confirm wording/
        force-flag logic; items whose plan is `None` (in-use volume, built-in
        network) keep their guard-notify from `plan_delete` and are silently
        excluded from the batch.
        """
        entry = self._resource_registry[tab_id]
        jobs: list[tuple[tuple[str, str], str, Callable[[], CommandResult]]] = []
        names: list[str] = []
        for item in self._marked_items(tab_id):
            key = self._row_key(item)
            plan = entry.plan_delete(item)
            if key is None or plan is None:
                continue
            name = _display_name(item)
            force = plan.force_default if plan.force_default is not None else False
            jobs.append((key, name, _bind_delete_command(plan, force)))
            names.append(name)

        if not jobs:
            self.notify(
                f"No marked {entry.label}s eligible to delete", severity="warning"
            )
            self._marked[tab_id] = set()
            self._rerender_active_table()
            return

        preview = ", ".join(escape(n) for n in names[:8])
        if len(names) > 8:
            preview += f", and {len(names) - 8} more"
        confirmed = await self.push_screen_wait(
            ConfirmDialog(f"Delete {len(names)} {entry.label}(s)? {preview}")
        )
        if not confirmed:
            logger.debug("Bulk delete cancelled by user")
            return
        self._run_bulk(tab_id, "Deleted", jobs)
