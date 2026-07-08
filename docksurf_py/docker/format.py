"""Display-string formatting for Docker resource fields."""

import re
from datetime import datetime, timezone

from docksurf_py.models import DiskUsageEntry, PortBinding, SystemDf

# Matches an env var key that looks like it holds a secret. Deliberately a
# broad substring match. A false positive (e.g. KEYVAULT ,KEYBOARD_LAYOUT)
# just masks an harmless value, while a false negative would
# leak a real secret onto a screen that gets shared.
_SECRET_KEY_RE = re.compile(
    r"(PASSWORD|SECRET|TOKEN|KEY|PASS|CREDENTIAL)", re.IGNORECASE
)

_AGE_UNITS = (
    (60, 1, "s"),
    (3600, 60, "m"),
    (86400, 3600, "h"),
    (86400 * 30, 86400, "d"),
    (86400 * 365, 86400 * 30, "mo"),
)


def _format_age(diff: int) -> str:
    """Format a second delta as a short relative-age string."""
    if diff < 0:
        return "just now"
    for threshold, unit, suffix in _AGE_UNITS:
        if diff < threshold:
            return f"{diff // unit}{suffix} ago"
    return f"{diff // (86400 * 365)}y ago"


def _parse_docker_ts(ts: str) -> datetime | None:
    """Parse a Docker RFC3339 timestamp to an aware datetime, or None."""
    ts_clean = ts
    dot = ts_clean.find(".")
    if dot != -1:
        end = dot + 1
        while end < len(ts_clean) and ts_clean[end].isdigit():
            end += 1
        fraction = ts_clean[dot + 1 : end]
        if len(fraction) > 6:
            ts_clean = ts_clean[: dot + 1] + fraction[:6] + ts_clean[end:]

    if ts_clean.endswith("Z"):
        ts_clean = ts_clean[:-1] + "+00:00"

    try:
        dt = datetime.fromisoformat(ts_clean)
    except ValueError:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def format_relative_time(ts: str) -> str:
    """Convert a Docker timestamp string to a human-readable relative age."""
    if not ts:
        return "Unknown"
    dt = _parse_docker_ts(ts)
    if dt is None:
        return ts
    diff = int((datetime.now(timezone.utc) - dt).total_seconds())
    return _format_age(diff)


def format_uptime(started_at: str) -> str:
    """How long a container has been running, e.g. "3h" — "—" if not started.

    Docker reports `StartedAt` as the zero time ("0001-01-01T…") for containers
    that have never run.
    """
    if not started_at or started_at.startswith("0001"):
        return "—"
    dt = _parse_docker_ts(started_at)
    if dt is None:
        return "—"
    diff = int((datetime.now(timezone.utc) - dt).total_seconds())
    return _format_age(diff).removesuffix(" ago")


def format_size(size_in_bytes: int | None) -> str:
    if not size_in_bytes:
        return "0B"
    size: float = size_in_bytes
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024.0:
            return f"{size:.2f}{unit}"
        size /= 1024.0
    return f"{size:.2f}PB"


def format_ports(ports: list[PortBinding]) -> str:
    parts = []
    for p in ports:
        if p.host_port:
            prefix = f"{p.host_ip}:" if p.host_ip else ""
            parts.append(f"{prefix}{p.host_port}->{p.container_port}")
        else:
            parts.append(p.container_port)
    return ", ".join(parts)


def format_labels(labels: dict[str, str]) -> str:
    return ", ".join(f"{k}={v}" for k, v in labels.items())


def format_env(env: list[str], reveal: bool = False) -> str:
    """Render `KEY=VALUE` env entries, one per line.

    Masks the value of any entry whose key matches `_SECRET_KEY_RE` unless
    `reveal` is set, env vars are shown in plaintext in the detail pane
    """
    if reveal:
        return "\n".join(env)
    lines = []
    for entry in env:
        key, sep, _value = entry.partition("=")
        if sep and _SECRET_KEY_RE.search(key):
            lines.append(f"{key}=••••••••")
        else:
            lines.append(entry)
    return "\n".join(lines)


def _parse_system_df(raw: dict) -> SystemDf:
    """Parse the raw `/system/df` payload into typed per-category entries.

    Reclaimable figures are approximate (image sizes include shared layers, so
    unused-image reclaimable can be slightly overstated) — enough to answer
    "what can I prune?" at a glance, matching `docker system df` closely.
    """
    entries: list[DiskUsageEntry] = []

    images = raw.get("Images") or []
    entries.append(
        DiskUsageEntry(
            kind="Images",
            total_count=len(images),
            active_count=sum(1 for i in images if i.get("Containers", 0)),
            size_bytes=sum(i.get("Size", 0) or 0 for i in images),
            reclaimable_bytes=sum(
                (i.get("Size", 0) or 0) for i in images if not i.get("Containers", 0)
            ),
        )
    )

    containers = raw.get("Containers") or []

    def _running(c: dict) -> bool:
        return (c.get("State") or "").lower() == "running"

    entries.append(
        DiskUsageEntry(
            kind="Containers",
            total_count=len(containers),
            active_count=sum(1 for c in containers if _running(c)),
            size_bytes=sum(c.get("SizeRw", 0) or 0 for c in containers),
            reclaimable_bytes=sum(
                (c.get("SizeRw", 0) or 0) for c in containers if not _running(c)
            ),
        )
    )

    volumes = raw.get("Volumes") or []

    def _vsize(v: dict) -> int:
        return (v.get("UsageData") or {}).get("Size", 0) or 0

    def _vactive(v: dict) -> bool:
        return ((v.get("UsageData") or {}).get("RefCount", 0) or 0) > 0

    entries.append(
        DiskUsageEntry(
            kind="Local Volumes",
            total_count=len(volumes),
            active_count=sum(1 for v in volumes if _vactive(v)),
            size_bytes=sum(_vsize(v) for v in volumes),
            reclaimable_bytes=sum(_vsize(v) for v in volumes if not _vactive(v)),
        )
    )

    cache = raw.get("BuildCache") or []
    entries.append(
        DiskUsageEntry(
            kind="Build Cache",
            total_count=len(cache),
            active_count=sum(1 for b in cache if b.get("InUse")),
            size_bytes=sum(b.get("Size", 0) or 0 for b in cache),
            reclaimable_bytes=sum(
                (b.get("Size", 0) or 0) for b in cache if not b.get("InUse")
            ),
        )
    )

    return SystemDf(
        entries=entries,
        total_size=sum(e.size_bytes for e in entries),
        total_reclaimable=sum(e.reclaimable_bytes for e in entries),
    )
