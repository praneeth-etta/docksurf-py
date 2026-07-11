"""Network-topology helpers: member join, boxed diagram, clipboard summary.

Sits beside `renderer/` and `actions/` rather than inside either, because both
consume it: `renderer/detail_pane_renderer.py` draws the diagrams into the
detail pane, and `actions/clipboard.py` yanks the plaintext summary — and all
of it derives from the same endpoint↔container join (`_network_members`).
Imports stay at the `docker/`-and-below layer (`models`, `constants`,
`docker.format_ports`), so either package can import this one freely.

The diagram is attachment topology only, by design — Docker records which
containers sit on a network, not who talks to whom, so the ports shown are
what each container *offers* to its peers, not observed traffic.
"""

from dataclasses import dataclass, field

from rich.console import Console, ConsoleOptions, RenderResult
from rich.text import Text

from docksurf_py.constants import STATUS_GREEN, STATUS_RED
from docksurf_py.docker import format_ports
from docksurf_py.models import Container, Network

_BORDER = "dim"
# Cap per-box container lists so one busy network can't flood the side pane.
_MAX_PEERS = 8


@dataclass(frozen=True, slots=True)
class _Member:
    """One container's view of its attachment to a specific network.

    The join of a `NetworkEndpoint` (IP, only present for running containers —
    Docker's network inspect omits stopped ones) with the snapshot `Container`
    of the same name (run state, ports, Compose service, other networks).
    Either side may be missing: a stopped container has no endpoint, and an
    endpoint can name a container the snapshot doesn't know.
    """

    name: str
    ipv4: str = ""
    running: bool = True  # an endpoint with no snapshot container is running
    ports: str = ""
    service: str = ""
    other_networks: list[str] = field(default_factory=list)


def _network_members(network: Network, containers: list[Container]) -> list[_Member]:
    """Every container attached to `network`, running or not, sorted by name."""
    by_name = {c.name: c for c in containers}
    ips = {ep.container_name: ep.ipv4 for ep in network.endpoints}
    names = set(ips) | {c.name for c in containers if network.name in c.networks}
    members = []
    for name in sorted(names):
        c = by_name.get(name)
        members.append(
            _Member(
                name=name,
                ipv4=ips.get(name, ""),
                running=c.running if c else True,
                ports=format_ports(c.ports) if c else "",
                service=c.compose_service if c else "",
                other_networks=(
                    [n for n in c.networks if n != network.name] if c else []
                ),
            )
        )
    return members


def _network_summary(network: Network, containers: list[Container]) -> str:
    """Plaintext one-network digest for the clipboard (`Y` on a network row)."""
    header = "  ".join(
        p
        for p in (
            network.name,
            network.driver,
            network.subnet,
            f"gateway {network.gateway}" if network.gateway else "",
        )
        if p
    )
    members = _network_members(network, containers)
    if not members:
        return f"{header}\n  (no containers attached)"
    name_w = max(len(m.name) for m in members)
    ip_w = max(len(m.ipv4) for m in members)
    lines = [header]
    for m in members:
        parts = [
            m.name.ljust(name_w),
            m.ipv4.ljust(ip_w) if ip_w else "",
            m.ports,
            "running" if m.running else "stopped",
        ]
        if m.service and m.service != m.name:
            parts.append(f"(svc: {m.service})")
        if m.other_networks:
            parts.append(f"also on: {', '.join(m.other_networks)}")
        lines.append("  " + "  ".join(p for p in parts if p))
    return "\n".join(lines)


# --- Boxed hub-and-spoke diagram -------------------------------------------

# Rail-row characters keyed by which directions a cell connects to:
# (up to the hub, down to a child, left, right).
_RAIL_CHARS = {
    (True, True, True, True): "┼",
    (True, False, True, True): "┴",
    (False, True, True, True): "┬",
    (True, True, False, True): "├",
    (True, True, True, False): "┤",
    (False, True, False, True): "╭",
    (False, True, True, False): "╮",
    (True, False, False, True): "╰",
    (True, False, True, False): "╯",
    (False, False, True, True): "─",
    (True, True, False, False): "│",
}


def _box_lines(
    title: Text, body: list[Text], budget: int, *, heavy: bool = False
) -> list[Text]:
    """One bordered box as equal-width `Text` rows; content wider than
    `budget` is ellipsis-truncated rather than wrapped into soup."""
    h, v = ("═", "║") if heavy else ("─", "│")
    tl, tr, bl, br = ("╔", "╗", "╚", "╝") if heavy else ("╭", "╮", "╰", "╯")
    avail = max(budget - 4, 8)
    title = title.copy()
    title.truncate(avail - 2, overflow="ellipsis")
    trimmed = []
    for line in body:
        line = line.copy()
        line.truncate(avail, overflow="ellipsis")
        trimmed.append(line)
    inner = max([title.cell_len + 2] + [ln.cell_len for ln in trimmed])
    top = Text(f"{tl}{h} ", style=_BORDER)
    top.append_text(title)
    top.append(f" {h * (inner - title.cell_len - 1)}{tr}", style=_BORDER)
    rows = [top]
    for line in trimmed:
        row = Text(f"{v} ", style=_BORDER)
        row.append_text(line)
        row.append(" " * (inner - line.cell_len))
        row.append(f" {v}", style=_BORDER)
        rows.append(row)
    rows.append(Text(f"{bl}{h * (inner + 2)}{br}", style=_BORDER))
    return rows


@dataclass(slots=True)
class _HubDiagram:
    """Adaptive hub-and-spoke renderable: a hub box with child boxes below.

    Children sit side by side under a box-drawing rail when they fit the
    render width, and stack vertically along a spine when they don't — so the
    diagram survives a narrow detail pane (or an open log pane) intact.
    """

    hub_title: Text
    hub_body: list[Text]
    children: list[tuple[Text, list[Text]]]

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        width = max(options.max_width, 20)
        hub = _box_lines(self.hub_title, self.hub_body, width, heavy=True)
        if not self.children:
            yield from hub
            return
        boxes = [_box_lines(t, b, width - 5) for t, b in self.children]
        gap = 2
        total = sum(b[0].cell_len for b in boxes) + gap * (len(boxes) - 1)
        if total <= width:
            yield from self._horizontal(hub, boxes, gap)
        else:
            yield from self._vertical(hub, boxes)

    @staticmethod
    def _horizontal(hub: list[Text], boxes: list[list[Text]], gap: int) -> RenderResult:
        centers, offset = [], 0
        for b in boxes:
            w = b[0].cell_len
            centers.append(offset + w // 2)
            offset += w + gap
        total = offset - gap
        hub_w = hub[0].cell_len
        hub_offset = max((total - hub_w) // 2, 0)
        for row in hub:
            padded = Text(" " * hub_offset)
            padded.append_text(row)
            yield padded
        hub_center = hub_offset + hub_w // 2
        yield Text(" " * hub_center + "│", style=_BORDER)
        yield _HubDiagram._rail(hub_center, centers)
        height = max(len(b) for b in boxes)
        for i in range(height):
            row = Text()
            for j, b in enumerate(boxes):
                if j:
                    row.append(" " * gap)
                if i < len(b):
                    row.append_text(b[i])
                else:
                    row.append(" " * b[0].cell_len)
            yield row

    @staticmethod
    def _rail(hub_center: int, centers: list[int]) -> Text:
        """The `╭──┴──┬──╮` row joining the hub's drop line to each child."""
        lo = min(centers[0], hub_center)
        hi = max(centers[-1], hub_center)
        down = set(centers)
        cells = []
        for i in range(hi + 1):
            if i < lo:
                cells.append(" ")
                continue
            key = (i == hub_center, i in down, i > lo, i < hi)
            cells.append(_RAIL_CHARS.get(key, "─"))
        return Text("".join(cells), style=_BORDER)

    @staticmethod
    def _vertical(hub: list[Text], boxes: list[list[Text]]) -> RenderResult:
        yield from hub
        last = len(boxes) - 1
        for j, b in enumerate(boxes):
            yield Text("  │", style=_BORDER)
            for i, row in enumerate(b):
                if i == 0:
                    prefix = "  ╰─ " if j == last else "  ├─ "
                else:
                    prefix = "     " if j == last else "  │  "
                out = Text(prefix, style=_BORDER)
                out.append_text(row)
                yield out


def _dot(running: bool) -> tuple[str, str]:
    return ("● ", STATUS_GREEN) if running else ("○ ", STATUS_RED)


def _member_box(m: _Member) -> tuple[Text, list[Text]]:
    """Full member box for the stacked fallback layout."""
    title = Text.assemble(_dot(m.running), (m.name, "bold"))
    lines = []
    if m.ipv4:
        lines.append(Text(m.ipv4, style="cyan"))
    elif not m.running:
        lines.append(Text("stopped", style="dim"))
    if m.ports:
        lines.append(Text(m.ports, style="magenta"))
    if m.service and m.service != m.name:
        lines.append(Text(f"svc: {m.service}", style="dim"))
    if m.other_networks:
        lines.append(Text(f"⇄ also on: {', '.join(m.other_networks)}", style="yellow"))
    return title, lines


def _member_box_compact(m: _Member) -> tuple[Text, list[Text]]:
    """Terser member box for the radial layout (side-by-side columns are the
    scarce resource): `also on` moves into the title, `svc:` is dropped."""
    title = Text.assemble(_dot(m.running), (m.name, "bold"))
    if m.other_networks:
        title.append(f" ⇄ also on: {', '.join(m.other_networks)}", style="yellow")
    body = []
    if m.ipv4:
        body.append(Text(m.ipv4, style="cyan"))
    elif not m.running:
        body.append(Text("stopped", style="dim"))
    if m.ports:
        body.append(Text(m.ports, style="magenta"))
    return title, body


# --- Radial layout (the Networks-tab default) --------------------------------

_GAP = 2  # minimum columns between side-by-side boxes
_MAX_RADIAL = 6  # more members than this falls back to stacked
_MIN_RADIAL_WIDTH = 32  # roughly two minimal node boxes + gap
_MIN_NODE_WIDTH = 14


def _puncture(row: Text, x: int, ch: str) -> Text:
    """Replace the border char at column `x` with a T-junction (`┬`/`┴`/`╤`/
    `╧`) so a connector visually attaches to the box. No-op when `x` lands on
    non-border content (a long title can reach the attachment column) — the
    line then simply abuts the border, which still reads fine."""
    plain = row.plain
    if not (0 < x < len(plain)) or plain[x] not in "─═":
        return row
    parts = row.divide([x, x + 1])
    out = Text()
    out.append_text(parts[0])
    out.append(ch, style=_BORDER)
    out.append_text(parts[2])
    return out


def _composite(stamps: list[tuple[int, Text]]) -> Text:
    """One output row from `(x, text)` stamps sorted by increasing `x`."""
    row, pos = Text(), 0
    for x, t in stamps:
        row.append(" " * (x - pos))
        row.append_text(t)
        pos = x + t.cell_len
    return row


def _box_band(
    boxes: list[list[Text]], xs: list[int], *, bottom_align: bool
) -> list[Text]:
    """Composite a row of boxes at their x offsets. The row above the hub is
    bottom-aligned (boxes hang down toward it) so connectors stay short."""
    height = max(len(b) for b in boxes)
    rows = []
    for i in range(height):
        stamps = []
        for b, x in zip(boxes, xs):
            idx = i - (height - len(b)) if bottom_align else i
            if 0 <= idx < len(b):
                stamps.append((x, b[idx]))
        rows.append(_composite(stamps))
    return rows


def _char_row(marks: dict[int, str]) -> Text:
    """A sparse connector row: `marks` maps column → box-drawing char."""
    cells = [" "] * (max(marks) + 1)
    for x, ch in marks.items():
        cells[x] = ch
    return Text("".join(cells), style=_BORDER)


def _run_row(ups: list[int], downs: list[int]) -> Text:
    """Elbow runs, one per connector: each joins an `up` column (line enters
    from the row above) to a `down` column (line exits below). Callers
    guarantee the runs' column spans are disjoint."""
    cells = [" "] * (max(ups + downs) + 1)
    for u, d in zip(ups, downs):
        if u == d:
            cells[u] = "│"
            continue
        lo, hi = sorted((u, d))
        for x in range(lo + 1, hi):
            cells[x] = "─"
        if d > u:
            cells[u], cells[d] = "╰", "╮"
        else:
            cells[d], cells[u] = "╭", "╯"
    return Text("".join(cells), style=_BORDER)


def _spread(widths: list[int], width: int) -> list[int] | None:
    """Left-edge x offsets spreading a row of boxes across `width`: outer
    boxes touch the edges, a middle box sits centered. `None` on overlap."""
    k = len(widths)
    if k == 1:
        return [(width - widths[0]) // 2]
    if k == 2:
        xs = [0, width - widths[1]]
    else:
        xs = [0, (width - widths[1]) // 2, width - widths[2]]
    for i in range(1, k):
        if xs[i] < xs[i - 1] + widths[i - 1] + _GAP:
            return None
    return xs


@dataclass(slots=True)
class _NetworkDiagram:
    """Render-time chooser for the Networks-tab diagram.

    Radial — the hub centered with member boxes above and below, joined by
    routed elbow connectors — whenever member count, width, and routing
    allow; otherwise the stacked `_HubDiagram`. The decision needs
    `options.max_width`, which only exists at render time, and every
    fallback rule errs toward "never emit a broken picture".
    """

    hub_title: Text
    hub_body: list[Text]
    members: list[_Member]

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        width = max(options.max_width, 20)
        rows = self._radial_rows(width)
        if rows is None:
            yield self._stacked()
        else:
            yield from rows

    def _stacked(self) -> _HubDiagram:
        if not self.members:
            body = [*self.hub_body, Text("(no containers attached)", style="dim")]
            return _HubDiagram(self.hub_title, body, [])
        children = [_member_box(m) for m in self.members[:_MAX_PEERS]]
        if len(self.members) > _MAX_PEERS:
            children.append(
                (Text(f"+{len(self.members) - _MAX_PEERS} more", style="dim"), [])
            )
        return _HubDiagram(self.hub_title, self.hub_body, children)

    def _radial_rows(self, width: int) -> list[Text] | None:
        """The radial picture as finished rows, or `None` when anything —
        member count, width, box packing, connector routing — doesn't fit."""
        if not 0 < len(self.members) <= _MAX_RADIAL or width < _MIN_RADIAL_WIDTH:
            return None
        # Alternate members above/below the hub so the picture stays balanced.
        rows_members = [self.members[0::2], self.members[1::2]]
        hub = _box_lines(self.hub_title, self.hub_body, width, heavy=True)
        hub_w = hub[0].cell_len
        hub_x = (width - hub_w) // 2

        placed = []  # (boxes, xs, node_centers, hub_attach_cols) per used row
        for row_members in rows_members:
            if not row_members:
                continue
            k = len(row_members)
            budget = (width - _GAP * (k - 1)) // k
            if budget < _MIN_NODE_WIDTH:
                return None
            boxes = [_box_lines(*_member_box_compact(m), budget) for m in row_members]
            xs = _spread([b[0].cell_len for b in boxes], width)
            if xs is None:
                return None
            centers = [x + b[0].cell_len // 2 for b, x in zip(boxes, xs)]
            # Attachment points spread along the hub border, one per node,
            # ordered left→right like the nodes so runs can't cross. A node
            # whose center falls within the hub's border gets a straight
            # drop (attach at its own center) instead of a one-column jog.
            defaults = [hub_x + 1 + (i + 1) * (hub_w - 2) // (k + 1) for i in range(k)]
            attach = [
                c if hub_x + 1 <= c <= hub_x + hub_w - 2 else d
                for c, d in zip(centers, defaults)
            ]
            if any(a2 <= a1 for a1, a2 in zip(attach, attach[1:])):
                attach = defaults  # clamping broke the ordering — revert
            spans = sorted((min(c, a), max(c, a)) for c, a in zip(centers, attach))
            if any(s <= e + 1 for (_, e), (s, _) in zip(spans, spans[1:])):
                return None  # runs would touch/overlap — no crossings, ever
            placed.append((boxes, xs, centers, attach))

        out: list[Text] = []
        top, bottom = placed[0], placed[1] if len(placed) > 1 else None

        boxes, xs, centers, attach = top
        for b, c, x in zip(boxes, centers, xs):
            b[-1] = _puncture(b[-1], c - x, "┬")
        for a in attach:
            hub[0] = _puncture(hub[0], a - hub_x, "╧")
        out.extend(_box_band(boxes, xs, bottom_align=True))
        out.append(_char_row({c: "│" for c in centers}))
        out.append(_run_row(ups=centers, downs=attach))

        if bottom is not None:
            for a in bottom[3]:
                hub[-1] = _puncture(hub[-1], a - hub_x, "╤")
        for row in hub:
            out.append(_composite([(hub_x, row)]))

        if bottom is not None:
            boxes, xs, centers, attach = bottom
            for b, c, x in zip(boxes, centers, xs):
                b[0] = _puncture(b[0], c - x, "┴")
            out.append(_run_row(ups=attach, downs=centers))
            out.append(_char_row({c: "│" for c in centers}))
            out.extend(_box_band(boxes, xs, bottom_align=False))
        return out


def network_topology(network: Network, containers: list[Container]) -> _NetworkDiagram:
    """Networks-tab diagram: the network as hub, attached containers around it
    (radial when it fits, stacked otherwise — see `_NetworkDiagram`)."""
    hub_title = Text(network.name, style="bold")
    hub_body = []
    meta = "  ".join(p for p in (network.driver, network.subnet) if p and p != "N/A")
    if meta:
        hub_body.append(Text(meta, style="dim"))
    return _NetworkDiagram(hub_title, hub_body, _network_members(network, containers))
