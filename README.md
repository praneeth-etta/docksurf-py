# docksurf-py

A keyboard-driven terminal UI for visualising and managing Docker resources — containers, images, volumes, and networks. No GUI, no browser tab.

```
┌──────────────────────────────────────────────────────────────────────┐
│  DockSurf                                                  12:34:56  │
├───────────────────────────┬──────────────────────────────────────────┤
│ Containers  Images        │  Container: api-server                   │
│ Volumes     Networks      │  ────────────────────────────────────    │
│                           │  ID         a1b2c3d4                     │
│  ● api-server             │  Image      myapp-api:2.1                │
│  ● nginx-proxy            │  Status     Up 3 hours                   │
│  ● worker                 │  Created    3 hours ago                  │
│  ○ deploy-build           │  Ports      0.0.0.0:8080->8080/tcp       │
│                           │  Networks   myapp_net                    │
│                           │  Mounts     /uploads, /app/pgdata        │
│                           │  ▶ Environment Variables                 │
└───────────────────────────┴──────────────────────────────────────────┘
  Containers: 3 running / 1 stopped  |  Images: 12 total  |  Volumes: 2 orphaned
  q Quit  r Refresh  / Search  ? Help  l Logs  e Exec  s Stop  S Start  d Delete
```

## Requirements

- Python 3.11+
- Docker daemon running locally
- [`uv`](https://github.com/astral-sh/uv) (package manager)

## Install

```bash
git clone <repo>
cd docksurf-py

uv venv && source .venv/bin/activate
uv pip install -e .

docksurf-py
```

Or without installing:

```bash
uv run python -m docksurf_py.app
```

## Keybindings

### Global

| Key   | Action                              |
|-------|-------------------------------------|
| `r`   | Refresh all Docker data             |
| `/`   | Open search / filter for active tab |
| `?`   | Help screen                         |
| `q`   | Quit                                |
| `Tab` | Switch between tabs                 |
| `↑/↓` | Navigate rows                       |

### Containers tab

| Key | Action                             |
|-----|------------------------------------|
| `s` | Stop container                     |
| `S` | Start container                    |
| `x` | Restart container                  |
| `e` | Exec shell into container (`sh`)   |
| `l` | Toggle log viewer                  |
| `d` | Delete (with confirmation dialog)  |

### All tabs

| Key | Action                             |
|-----|------------------------------------|
| `d` | Delete selected resource           |

### Log pane (when open)

| Key | Action                                       |
|-----|----------------------------------------------|
| `f` | Toggle live log follow (`docker logs -f`)    |
| `z` | Expand log pane to full width / collapse     |
| `l` | Close log viewer, return to detail pane      |

The `⛶ Expand` / `⊡ Collapse` button in the log toolbar is also clickable.

## Tabs

**Containers** — all containers (running and stopped). Detail pane shows image, ports, networks, mounts, and a collapsible environment variable section.

**Images** — all images, tagged as *In Use*, *Unused*, or *Dangling*. Detail pane shows size, created date, architecture, and which containers reference the image.

**Volumes** — all volumes, tagged as *In Use* or *Orphaned*. Detail pane shows mountpoint, driver, labels, and attached containers.

**Networks** — all networks with driver and scope. Detail pane shows subnet, gateway, and attached containers.

## Architecture

Four modules with strict layering:

- **`constants.py`** — all widget/tab/table IDs and Rich markup helpers. Nothing imports into this module.
- **`docker.py`** — all Docker I/O via the [Docker SDK for Python](https://docker-py.readthedocs.io/). `DockerClient` owns the SDK connection; `DockerResourceFetcher` handles parallel reads; `LogStream` handles live log iteration. Returns typed dataclasses (`Container`, `Image`, `Volume`, `Network`, `DockerSnapshot`).
- **`widgets.py`** — Textual UI components with no Docker knowledge: `DetailPane`, `LogPane`, `SearchBar`, `ConfirmDialog`, `HelpScreen`, `StatusBar`.
- **`app.py`** — assembles layout and event wiring through seven focused mixin classes (`TableRenderer`, `SnapshotManager`, `ResourceFocusResolver`, `DetailPaneRenderer`, `ContainerActionHandler`, `ResourceDeletionHandler`, `ResourceSearchController`) composed into `DockSurfApp`.

## How data is fetched

DockSurf uses the Docker SDK (`docker-py`) — no subprocess calls, no raw CLI parsing. All four resource types are fetched in parallel via `ThreadPoolExecutor` on each refresh, so the UI never blocks. Press `r` to refresh on demand.

Logs are streamed live from the SDK's generator and fed into the `LogPane` via a background thread.

## Docker contexts — local and remote

DockSurf connects to whatever daemon your active Docker context points at. That doesn't have to be your local machine.

```bash
# Create a context pointing at a remote server over SSH
docker context create prod --docker "host=ssh://user@prod.example.com"

# Switch to it
docker context use prod

# DockSurf now shows that server's containers, images, volumes, and networks
docksurf-py
```

The active context name is shown in the status bar at the bottom of the TUI. Switch contexts between runs to manage different environments from the same tool.

**Common use cases:**
- Managing a VPS or cloud instance without keeping an SSH session open
- Inspecting a production server's containers without full shell access
- Managing a Raspberry Pi or edge device from your laptop

Any runtime that registers a Docker context works — Docker Desktop, Colima, Rancher Desktop, or a plain daemon on a remote host.

## Logging

App logs are written to `~/.local/share/docksurf-py/docksurf.log` — never to stdout (which belongs to the TUI). Useful for debugging refresh errors, failed Docker API calls, and container action results.
