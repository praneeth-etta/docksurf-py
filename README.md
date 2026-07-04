# docksurf-py

A keyboard-driven terminal UI for visualising and managing Docker resources like containers, images, volumes, and networks. Compose-aware and live: it reacts to Docker events on its own and streams real time resource usage, so you're observing, not polling. No GUI, no browser tab.

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  DockSurf                                                          12:34:56  │
├──────────────────────────────────┬───────────────────────────────────────────┤
│ Containers Images Volumes Nets   │  Container: myapp-api-1                   │
│                                  │  ─────────────────────────────────────    │
│ Name         Status   Health Up  │  Project    myapp                         │
│ ▾ myapp      3/4 run             │  Service    api                           │
│   ├ api      Up ✓     healthy 2h │  Status     Up 2 hours ✓                  │
│   ├ web      Up       —       2h │  Health     healthy                       │
│   └ worker   Exited(1)—       —  │  Uptime     2h     Restarts  0            │
│ ▸ infra      2/2 run             │ ┌ Live stats: myapp-api-1 ──────────────┐ │
│                                  │ │ CPU  ▐███▌     42.1%                  │ │
│ redis        Up       —       5h │ │ MEM  ▐█▌  180 MiB / 512 MiB (35%)     │ │
│                                  │ │ NET  ↓ 1.2 MB   ↑ 340 KB              │ │
│                                  │ └───────────────────────────────────────┘ │
└──────────────────────────────────┴───────────────────────────────────────────┘
  Containers: 4 running / 1 stopped │ Images: 12 │ Volumes: 2 orphaned │ Context: default
  q Quit  r Refresh  / Search  ? Help  l Logs  s Stop  u Up  k Down  w Disk
```

## Highlights

- **Live by default** — the tables auto-refresh on `docker events` (container start/stop/die, image pull/delete, …); `r` is a manual reload.
- **Docker Compose–aware** — containers are grouped by project into a collapsible tree, with project-wide up / down / stop / start / restart and interleaved, colour-coded-per-service logs.
- **Live resource stats** — CPU %, memory, network and block I/O streamed into the detail pane for the selected running container.
- **Full-control log viewer** — live follow, in-log search with `n`/`N` match jumping, a timestamps toggle, configurable tail depth and `--since` window, line wrap for long JSON, jump-to-top/bottom, and one-key export of the buffer to a file.
- **Operational signals at a glance** — colour-coded health column, uptime and restart count, plus recent health-check probe output in the detail pane.
- **Full lifecycle control** — pause/unpause and kill sit alongside stop/start/restart, so a `stop` that hangs on its 10s timeout never needs the CLI.
- **Multi-select + bulk actions** — mark rows on any tab and stop/start/remove them as a batch; ideal for cleaning up after a test run.
- **Inspect & prune** — the full raw `docker inspect` JSON for any resource in a searchable modal, plus a one-key menu to prune stopped containers, dangling images, unused volumes/networks, or everything at once.
- **Power-user exec & copy** — a custom exec command with a chosen user, `docker cp` in/out of a container, and an on-demand `docker top` process snapshot, all via quick prompts.
- **Disk usage** — a `docker system df` breakdown (per-type size + reclaimable) on demand.
- **Local or remote** — honours your active `docker context`, so it manages the same daemon your CLI does like the local, Docker Desktop, Colima, or a remote host over SSH.

## Requirements

- Python 3.11+
- A reachable Docker daemon (local, or a remote one via `docker context`)
- [`uv`](https://github.com/astral-sh/uv) (package manager)
- The `docker` CLI on `PATH` — only needed for exec-shell (`e`/`E`), Compose project actions (`u`/`k`), and file copy (`C`); everything else uses the SDK. DockSurf degrades gracefully if it's absent.

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

| Key     | Action                                                    |
|---------|------------------------------------------------------------|
| `r`     | Refresh all Docker data                                    |
| `/`     | Open search / filter for active tab                        |
| `i`     | Inspect focused resource — full `docker inspect` JSON, searchable (works on any tab) |
| `P`     | Prune menu — stopped containers / dangling images / unused volumes / unused networks / everything |
| `w`     | Disk-usage screen (`docker system df`)                     |
| `space` | Mark the focused row for a bulk action (or collapse/expand a Compose project header — see below) |
| `escape`| Clear all marks on the active tab (no-op if nothing's marked) |
| `?`     | Help screen                                                 |
| `q`     | Quit                                                        |
| `d`     | Delete the selected resource — or every marked resource — with confirmation |
| `Tab`   | Switch between tabs                                         |
| `↑/↓`   | Navigate rows                                               |

**Multi-select + bulk actions**: mark any number of rows with `space` (on any tab), then `s`/`S`/`d` act on the whole marked set instead of just the focused row — e.g. mark five stopped test containers and `d` once to remove them all behind a single confirmation. Bulk stop/start silently skips marked containers that don't qualify (already stopped/running); bulk delete reuses each resource's normal guards (in-use volumes, built-in networks aren't touched).

### Containers tab

The container/project keys are context-sensitive: on a Compose **project header** (compose stack) row they act on the whole project; on a single container row they act on that container. When rows are marked, `s`/`S` act on the marked set instead (see above).

| Key | On a container row                              | On a project header row          |
|-----|--------------------------------------------------|-----------------------------------|
| `s` | Stop container                                    | Stop whole project               |
| `S` | Start container                                   | Start whole project              |
| `x` | Restart container                                 | Restart whole project            |
| `p` | Pause / unpause container                         | —                                 |
| `K` | Kill container (`SIGKILL`) — no confirmation, the escape hatch when `stop` hangs | — |
| `l` | Toggle log viewer                                 | Aggregated project logs          |
| `e` | Exec shell (`bash`→`sh`)                          | —                                 |
| `E` | Exec with a custom command and/or user (`-u`), pre-filled with the detected shell | — |
| `t` | Toggle a `docker top` running-process snapshot in the detail pane | — |
| `C` | Copy files in/out of the container (`docker cp`), via a source/destination prompt | — |
| `u` | Compose **up** (`docker compose up -d`) — brings the focused project up |
| `k` | Compose **down** (`docker compose down`, confirmed) — tears the project down |
| `space` | Mark for bulk action                          | Collapse / expand project group  |

### Log pane (when open)

| Key       | Action                                                           |
|-----------|------------------------------------------------------------------|
| `f`       | Toggle live log follow (pause / resume)                          |
| `/`       | Filter to matching lines; matches are highlighted (Esc to clear) |
| `n` / `N` | Jump to next / previous match (with a `k/N` counter in the header) |
| `T`       | Show / hide timestamps                                           |
| `o`       | Log options — tail depth (100 / 500 / 5000 / all) and a `--since` window |
| `W`       | Toggle line wrap (for long JSON lines)                           |
| `g` / `G` | Jump to top / bottom of the buffer                               |
| `X`       | Export the buffer to a file (`~/.local/share/docksurf-py/exports/`) |
| `z`       | Expand log pane to full width / collapse                         |
| `l`       | Close log viewer, return to detail pane                          |

The `⛶ Expand` / `⊡ Collapse` button in the log toolbar is also clickable.

## Tabs

Every tab has a leading mark column (`space` to toggle) for multi-select + bulk actions — see [Keybindings](#keybindings).

**Containers** — all containers (running and stopped), **grouped by Compose project** into a collapsible tree (project header → service rows), with standalone containers listed below. Columns: name, status, colour-coded health, and uptime. The detail pane shows image, ports, networks, project/service, uptime, restart count, a collapsible environment-variable section, a collapsible recent-health-probe log, a live CPU/mem/net/block-IO panel for a running container, and — on demand (`t`) — a `docker top` running-process snapshot.

**Images** — all images, tagged as *In Use*, *Unused*, or *Dangling*. Detail pane shows size, created date, architecture, and which containers reference the image.

**Volumes** — all volumes, tagged as *In Use* or *Orphaned*. Detail pane shows mountpoint, driver, labels, and attached containers.

**Networks** — all networks with driver and scope. Detail pane shows subnet, gateway, and attached containers.

## Architecture

Eleven modules with strict layering (_models.py_ and _constants.py_ are leaf nodes and nothing imports into them):

- **`constants.py`** — widget/tab/table IDs, the _SafeMarkup_ render-boundary marker, and Rich markup helpers.
- **`models.py`** — typed dataclasses for every resource (_Container_, _Image_, _Volume_, _Network_, _ComposeProject_, _ContainerStats_, _ContainerTop_, _SystemDf_, …). No presentation logic.
- **`connection.py`** — Docker connection detection/classification, context and host resolution.
- **`docker.py`** — all Docker I/O via the [Docker SDK for Python](https://docker-py.readthedocs.io/). _DockerClient_ owns the SDK connection (honouring _docker context_) and every management call — full container lifecycle (stop/start/restart/pause/kill/remove), prune, inspect, `top`, and file copy; _DockerResourceFetcher_ does the parallel reads; _LogStream_/_MergedLogStream_/_StatsStream_/_EventStream_ are the live iterators.
- **`service.py`** — the _DockerService_ protocol _DockerClient_ implements (swappable in tests).
- **`widgets.py`** — Textual UI components with no Docker knowledge: _ContainerTable_, _DetailPane_, _LogPane_, _SearchBar_, _ConfirmDialog_, _HelpScreen_, _SystemDfScreen_, _InspectScreen_, _PruneScreen_, _PromptScreen_, _StatusBar_.
- **`renderer.py`**, **`actions.py`**, **`search.py`**, **`observability.py`** — focused mixin classes composed into _DockSurfApp_ in **_app.py_**, driven by a single per-tab ResourceEntry registry: table rendering, snapshot/event lifecycle, and multi-select marking (`renderer.py`); container lifecycle, Compose, delete, bulk actions, inspect, prune, exec, and file-copy actions (`actions.py`); search (`search.py`); live stats, disk usage, and `docker top` (`observability.py`).

## How data is fetched

DockSurf talks to Docker through the SDK (`docker-py`), not the CLI but with three sanctioned exceptions, all guarded on the `docker` CLI being present: the interactive **exec-shell** (needs a real TTY the SDK can't hand back), **Compose project actions** (docker-py has no Compose support, and `up` must recreate containers from the compose file), and **file copy** (`docker cp`) — the SDK only exposes raw tar archives, and safely reproducing `docker cp`'s copy semantics isn't worth it for a convenience feature.

- **Snapshots** — all four resource types are fetched in parallel via `ThreadPoolExecutor` on each refresh, so the UI never blocks.
- **Event-driven refresh** — a background worker subscribes to `docker events` and debounces bursts into a refresh, ignoring high-frequency noise (exec probes, per-interval health-status pings). A refresh preserves your current selection rather than resetting the cursor.
- **Live stats** — `container.stats(stream=True)` is streamed for the selected running container on a background thread and rendered into the detail pane (one stream at a time, to stay cheap).
- **Logs** — streamed live from the SDK's generator; timestamps are always fetched (shown on demand), and tail depth / `--since` window are configurable per view. A Compose project's logs are merged across its containers with a colour-coded per-service prefix.

## Docker contexts — local and remote

DockSurf connects to whatever daemon your active Docker context points at (matching the `docker` CLI's precedence: `DOCKER_HOST` → active context → default socket). That doesn't have to be your local machine.

```bash
# Create a context pointing at a remote server over SSH
docker context create prod --docker "host=ssh://user@prod.example.com"

# Switch to it
docker context use prod

# DockSurf now shows that server's containers, images, volumes, and networks
docksurf-py
```

The active context name and endpoint are shown in the status bar at the bottom of the TUI. Switch contexts between runs to manage different environments from the same tool.

**Common use cases:**
- Managing a VPS or cloud instance without keeping an SSH session open
- Inspecting a production server's containers without full shell access
- Managing a Raspberry Pi or edge device from your laptop

Any runtime that registers a Docker context works — Docker Desktop, Colima, Rancher Desktop, or a plain daemon on a remote host.

## Logging

App logs are written to `~/.local/share/docksurf-py/docksurf.log` — never to stdout (which belongs to the TUI). Useful for debugging refresh errors, failed Docker API calls, container/Compose action results, and stream lifecycle events.
