# docksurf

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

**Docs:** [Quickstart](QUICKSTART.md) · [Full keybindings reference](KEYBINDINGS.md) · [Changelog](CHANGELOG.md)

## Highlights

- **Live by default** — the tables auto-refresh on `docker events` (container start/stop/die, image pull/delete, …); `r` is a manual reload.
- **Docker Compose–aware** — containers are grouped by project into a collapsible tree, with project-wide up / down / stop / start / restart and interleaved, colour-coded-per-service logs.
- **Live resource stats** — CPU %, memory, network and block I/O streamed into the detail pane for the selected running container.
- **Full-control log viewer** — live follow, in-log search with `n`/`N` match jumping, a timestamps toggle, configurable tail depth and `--since` window, line wrap for long JSON, jump-to-top/bottom, and one-key export of the buffer to a file.
- **Operational signals at a glance** — colour-coded health column, uptime and restart count, plus recent health-check probe output in the detail pane.
- **Full lifecycle control** — pause/unpause and kill sit alongside stop/start/restart, so a `stop` that hangs on its 10s timeout never needs the CLI.
- **Multi-select + bulk actions** — mark rows on any tab and stop/start/remove them as a batch; ideal for cleaning up after a test run.
- **Inspect & prune** — the full raw `docker inspect` JSON for any resource in a searchable modal, plus a one-key menu to prune stopped containers, dangling images, unused volumes/networks, or everything at once.
- **Image / volume / network operations** — pull images with live progress, view layer history, tag, and bulk-clean dangling images; create volumes and check per-volume disk size; create networks and connect/disconnect containers, with per-container IP/MAC in the detail pane.
- **Power-user exec & copy** — a custom exec command with a chosen user, `docker cp` in/out of a container, and an on-demand `docker top` process snapshot, all via quick prompts.
- **Disk usage** — a `docker system df` breakdown (per-type size + reclaimable) on demand.
- **Local or remote** — honours your active `docker context` on startup, so it manages the same daemon your CLI does: local, Docker Desktop, Colima, or a remote host over SSH/TCP.
- **In-app context switching** — list and switch Docker contexts from inside the TUI (`D`) without ever running `docker context use`, so other terminals keep whatever context they already had. The choice is remembered across restarts.
- **Auto-reconnect** — if the daemon goes down mid-session, the status bar flags it immediately and DockSurf reconnects on its own the moment it's back, with a full refresh — no restart required.

## Requirements

- Python 3.11+
- A reachable Docker daemon (local, or a remote one via `docker context`)
- [`uv`](https://github.com/astral-sh/uv) (package manager)
- The `docker` CLI on `PATH` — only needed for exec-shell (`e`/`E`), Compose project actions (`u`/`k`), and file copy (`C`); everything else uses the SDK. DockSurf degrades gracefully if it's absent.

## Install

See [QUICKSTART.md](QUICKSTART.md) for install + first steps in under 2 minutes.

```bash
git clone <repo>
cd docksurf

uv venv && source .venv/bin/activate
uv pip install -e .

docksurf
```

Or without installing:

```bash
uv run python -m docksurf_py.app
```

## Keybindings

The essentials — full reference (per-tab keys, log pane, Compose header behaviour) lives in [KEYBINDINGS.md](KEYBINDINGS.md).

| Key          | Action                                                 |
|--------------|----------------------------------------------------------|
| `?`          | Help screen — every keybinding, in-app                   |
| `r`          | Refresh all Docker data                                  |
| `/`          | Search / filter the active tab                           |
| `↑/↓`, `Tab` | Navigate rows / switch tabs                              |
| `s`/`S`/`x`  | Stop / start / restart (Containers tab)                  |
| `e`          | Exec shell into the focused container                    |
| `l`          | Toggle log viewer                                        |
| `space`      | Mark for a bulk action                                   |
| `d`          | Delete the selected — or marked — resource(s)            |
| `i`          | Inspect (raw `docker inspect` JSON)                      |
| `P`          | Prune menu                                               |
| `D`          | Switch Docker context                                    |
| `q`          | Quit                                                     |

## Tabs

Every tab has a leading mark column (`space` to toggle) for multi-select + bulk actions — see [Keybindings](#keybindings).

**Containers** — all containers (running and stopped), **grouped by Compose project** into a collapsible tree (project header → service rows), with standalone containers listed below. Columns: name, status, colour-coded health, and uptime. The detail pane shows image, ports, networks, project/service, uptime, restart count, a collapsible environment-variable section, a collapsible recent-health-probe log, a live CPU/mem/net/block-IO panel for a running container, and — on demand (`t`) — a `docker top` running-process snapshot.

**Images** — all images, tagged as *In Use*, *Unused*, or *Dangling*. Detail pane shows size, created date, architecture, and which containers reference the image. Pull new images with live progress (`+`), view per-layer history (`h`), retag (`y`), and one-key mark-all-dangling for bulk cleanup (`a`).

**Volumes** — all volumes, tagged as *In Use* or *Orphaned*. Detail pane shows mountpoint, driver, labels, and attached containers. Create volumes (`+`) and pull on-demand per-volume size on disk (`b`).

**Networks** — all networks with driver and scope. Detail pane shows driver, scope, subnet, gateway, and each attached container's IP/MAC within the network. Create networks (`+`) and connect/disconnect containers (`v`/`m`).

## Architecture

Eleven top-level modules/packages with strict layering (_models.py_ and _constants.py_ are leaf nodes and nothing imports into them). Four of the eleven — `docker`, `renderer`, `actions`, `widgets` — are packages split into one file per class/mixin, each re-exporting exactly what the equivalent single file used to expose, so nothing outside the package needed to change:

- **`constants.py`** — widget/tab/table IDs, the _SafeMarkup_ render-boundary marker, and Rich markup helpers.
- **`models.py`** — typed dataclasses for every resource (_Container_, _Image_, _Volume_, _Network_, _ComposeProject_, _ContainerStats_, _ContainerTop_, _SystemDf_, _ContextInfo_, …). No presentation logic.
- **`connection.py`** — Docker connection detection/classification, context and host resolution.
- **`docker/`** — all Docker I/O via the [Docker SDK for Python](https://docker-py.readthedocs.io/). _DockerClient_ (`client.py`) owns the SDK connection (honouring _docker context_, with an in-app override for `switch_context`) and every management call — full container lifecycle (stop/start/restart/pause/kill/remove), prune, inspect, `top`, file copy, and context listing/switching; it also detects a dropped daemon (`mark_disconnected`) so reconnecting doesn't need a restart. _DockerResourceFetcher_ (`fetcher.py`) does the parallel reads; _LogStream_/_MergedLogStream_/_StatsStream_/_EventStream_ (`streams.py`) are the live iterators; context resolution (`context.py`) and display formatting (`format.py`) round out the package.
- **`service.py`** — the _DockerService_ protocol _DockerClient_ implements (swappable in tests).
- **`widgets/`** — Textual UI components with no Docker knowledge, one file per widget/screen: _ContainerTable_, _DetailPane_, _LogPane_, _SearchBar_, _ConfirmDialog_, _HelpScreen_, _SystemDfScreen_, _InspectScreen_, _PruneScreen_, _PromptScreen_, _StatusBar_ (renders a connection-lost indicator).
- **`renderer/`**, **`actions/`**, **`search.py`**, **`observability.py`** — focused mixin classes composed into _DockSurfApp_ in **_app.py_**, driven by a single per-tab ResourceEntry registry: table rendering, snapshot/event lifecycle, connection-state tracking, and multi-select marking (`renderer/`); container lifecycle, Compose, delete, bulk actions, inspect, prune, exec, file-copy, and context-switching actions (`actions/`); search (`search.py`); live stats, disk usage, and `docker top` (`observability.py`).

## How data is fetched

DockSurf talks to Docker through the SDK (`docker-py`), not the CLI but with three sanctioned exceptions, all guarded on the `docker` CLI being present: the interactive **exec-shell** (needs a real TTY the SDK can't hand back), **Compose project actions** (docker-py has no Compose support, and `up` must recreate containers from the compose file), and **file copy** (`docker cp`) — the SDK only exposes raw tar archives, and safely reproducing `docker cp`'s copy semantics isn't worth it for a convenience feature.

- **Snapshots** — all four resource types are fetched in parallel via `ThreadPoolExecutor` on each refresh, so the UI never blocks.
- **Event-driven refresh** — a background worker subscribes to `docker events` and debounces bursts into a refresh, ignoring high-frequency noise (exec probes, per-interval health-status pings). A refresh preserves your current selection rather than resetting the cursor.
- **Live stats** — `container.stats(stream=True)` is streamed for the selected running container on a background thread and rendered into the detail pane (one stream at a time, to stay cheap).
- **Logs** — streamed live from the SDK's generator; timestamps are always fetched (shown on demand), and tail depth / `--since` window are configurable per view. A Compose project's logs are merged across its containers with a colour-coded per-service prefix.

## Docker contexts — local and remote

DockSurf connects to whatever daemon your active Docker context points at (matching the `docker` CLI's precedence: `DOCKER_HOST` → active context → default socket). That doesn't have to be your local machine, and — since context switching now happens in-app — it doesn't require restarting DockSurf either.

**Creating a context** is still a `docker` CLI step (DockSurf lists and switches contexts, it doesn't create them):

```bash
# A context pointing at a remote Linux host over SSH — any host with a
# reachable Docker daemon and SSH access works: a cloud VM, a bare-metal
# box, a Raspberry Pi, a home server.
docker context create prod --docker "host=ssh://user@prod.example.com"

# Or over plain TCP, if the daemon's API is exposed that way
docker context create staging --docker "host=tcp://staging.example.com:2375"
```

**Switching contexts from inside DockSurf** — press `D` to list every context `docker context ls` knows about and pick one. This is in-app only: it never runs `docker context use`, so it doesn't touch `~/.docker/config.json` or repoint any other terminal's `docker`/`docker compose` — DockSurf just opens its own connection to the chosen context's daemon. The choice is remembered across restarts (`~/.local/share/docksurf/state.json`).

**Auto-reconnect** — if the daemon your active context points at goes down mid-session (VM reboot, daemon restart, network blip), the status bar flags it immediately (`● <reason>`) and DockSurf retries on its own every couple of seconds, reconnecting and refreshing the moment it's back — no restart, no manual `r`.

**Common use cases:**
- Managing a Linux VM (cloud instance, on-prem box, home server) over SSH without keeping a shell session open
- Switching between staging and production daemons from one tool, mid-session, with `D`
- Inspecting a production server's containers without full shell access
- Managing a Raspberry Pi or other edge device from your laptop

Any endpoint that speaks the **Docker Engine API** works this way — a plain Linux daemon, Docker Desktop, Colima, Rancher Desktop, or a remote host over SSH/TCP. Managed platforms that don't expose that API can't be reached this way, even with a custom context — **Azure Container Apps is the common trap**: it's Kubernetes-based under the hood with no Docker Engine endpoint at all, so `docker context`/DockSurf/`docker ps` have nothing to talk to. Azure Container Instances had a (now-deprecated) `docker context create aci` integration; Container Apps never did — manage those via `az containerapp`, the Azure Portal, or the Azure SDK instead.

## Logging

App logs are written to `~/.local/share/docksurf/docksurf.log` — never to stdout (which belongs to the TUI). Useful for debugging refresh errors, failed Docker API calls, container/Compose action results, and stream lifecycle events.

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for release history.
