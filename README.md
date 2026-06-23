# docksurf-py

A keyboard-driven terminal UI for visualising and managing Docker resources — containers, images, volumes, and networks. No GUI, no browser, no daemon socket needed.

```
┌──────────────────────────────────────────────────────────────────┐
│  DockSurf                                                12:34:56 │
├──────────────────────────┬───────────────────────────────────────┤
│ Containers Images        │  Container: api-server                │
│ Volumes    Networks      │  ───────────────────────────────────  │
│                          │  ID         a1b2c3d4e5f6...           │
│  ● api-server            │  Image      myapp-api:2.1             │
│  ● nginx-proxy           │  Status     Up 3 hours                │
│  ● worker                │  Created    3 hours ago               │
│  ○ deploy-build          │  Ports      0.0.0.0:8080->8080/tcp    │
│                          │  Networks   myapp_net                  │
│                          │  Mounts     /uploads, /app/pgdata      │
│                          │                                        │
└──────────────────────────┴───────────────────────────────────────┘
  q Quit  r Refresh  l Logs  e Exec  s Stop  S Start  x Restart  d Delete
```

## Install (Python 3.11+, uv)

```bash
git clone <repo>
cd docksurf-py

uv venv && source .venv/bin/activate
uv pip install -e .

docksurf-py
```

Or run directly without installing:

```bash
uv run python -m docksurf_py.app
```

## Keybindings

### Global

| Key        | Action                          |
|------------|---------------------------------|
| `r`        | Refresh all Docker data         |
| `q`        | Quit                            |
| `/`        | Filter current tab              |

### Containers tab

| Key        | Action                          |
|------------|---------------------------------|
| `l`        | Open log viewer (right pane)    |
| `e`        | Exec into container (`sh`)      |
| `s`        | Stop container                  |
| `S`        | Start container                 |
| `x`        | Restart container               |
| `d`        | Delete resource (with confirm)  |

### Log viewer

| Key        | Action                                      |
|------------|---------------------------------------------|
| `f`        | Toggle live log streaming (`docker logs -f`)|
| `z`        | Expand log pane to full width / collapse    |
| `L`        | Close log viewer, return to detail pane     |

The expand button (`⛶ Expand` / `⊡ Collapse`) in the log pane toolbar is also clickable.

## Tabs

**Containers** — all containers with status. Select one to see image, ports, networks, and mounts in the detail pane. Press `l` to stream logs inline.

**Images** — all images tagged as In Use, Unused, or Dangling. Detail pane shows size, created date, architecture, and which containers use it.

**Volumes** — all volumes tagged as In Use or Orphaned. Detail pane shows mountpoint, labels, and which containers mount it.

**Networks** — all networks with driver and scope. Detail pane shows subnet, gateway, and attached containers.

## Architecture

Three modules with strict layering:

- **`docker.py`** — data layer. `fetch_snapshot()` shells out to the Docker CLI and returns a `DockerSnapshot` dataclass. No daemon socket needed.
- **`widgets.py`** — UI components: `DetailPane`, `LogPane`, `ConfirmDialog`, `SearchBar`.
- **`app.py`** — wires layout and events. 40/60 horizontal split: `TabbedContent` on the left, `DetailPane` / `LogPane` on the right.

## How data is fetched

DockSurf talks to Docker exclusively through the CLI using `docker <cmd> --format '{{json .}}'` for structured output. Commands used:

- `docker ps -a` + `docker inspect`
- `docker images -a`
- `docker volume ls` + `docker volume inspect`
- `docker network ls` + `docker network inspect`
- `docker logs` (static snapshot + live streaming via `docker logs -f`)

Snapshots are fetched in a background thread — the UI never freezes. Press `r` to refresh on demand.
