# docker-vis

A terminal UI for visualising Docker containers, images, volumes and networks
in WSL — no GUI, no browser needed.

```
┌──────────────────────────────────────────────────────────────┐
│  docker-vis                                          12:34:56 │
├──────────────────┬───────────────────────────────────────────┤
│  Overview Images │  ● api-server                             │
│  Volumes Networks│  ─────────────────────────────────────── │
│                  │  Info                                     │
│  ◈ MY-APP        │  id           a1b2c3d4e5f6               │
│  ● nginx-proxy   │  image        myapp-api:2.1               │
│  ● api-server    │  status       running                     │
│  ● worker        │  compose      my-app / api                │
│  ● postgres      │                                           │
│  ● redis         │  Live Stats                               │
│                  │  cpu          ████░░░░░░  8.4%            │
│  ◈ STANDALONE    │  memory       210MB / 2GB                 │
│  ○ deploy-build  │                                           │
│                  │  Networks                                  │
│                  │  myapp_net    172.18.0.3                  │
│                  │                                           │
│                  │  Volumes                                   │
│                  │  [uploads-vol] → /uploads                 │
│                  │  [pgdata]      → /app/pgdata               │
└──────────────────┴───────────────────────────────────────────┘
  r Refresh  q Quit
```

## Install (WSL, Python 3.11+, uv)

```bash
# 1. Clone or copy this folder
cd docker-vis

# 2. Create a virtual environment and install
uv venv
source .venv/bin/activate
uv pip install -e .

# 3. Run
docker-vis
```

Or run directly without installing:

```bash
uv run python -m docker_vis.app
```

## Keybindings

| Key             | Action                     |
|-----------------|----------------------------|
| `Tab`           | Switch between sidebar tabs |
| `↑` / `↓`       | Navigate list              |
| `Enter`         | Select item / show detail  |
| `r`             | Refresh all data           |
| `q` / `Ctrl+C`  | Quit                       |

## Tabs

### Overview
All containers, grouped by `docker-compose` project. Shows running status
at a glance. Select a container to see: image, live CPU/memory stats,
ports, networks with IPs, and volume mounts.

### Images
All images grouped as **In Use**, **Unused**, and **Dangling**.
Select an image to see size, layer count, parent image, and which
containers use it. Dangling and unused images show a ready-to-copy
`docker rmi` command.

### Volumes
All volumes grouped as **Mounted** (attached to at least one container)
and **Orphaned** (safe to prune). Select a volume to see which containers
mount it and where. Orphaned volumes show a ready-to-copy `docker volume rm`
command.

### Networks
All Docker networks with driver and scope. Select a network to see
all attached containers.

## How data is fetched

docker-vis shells out to the Docker CLI — no daemon socket access needed.
Commands used:
- `docker ps -a` + `docker inspect`
- `docker stats --no-stream` (for live CPU/memory)
- `docker images -a` + `docker history`
- `docker volume ls` + `docker volume inspect`
- `docker network ls` + `docker network inspect`

All fetching happens in a background thread so the UI never freezes.
Press `r` to refresh on demand.

## Adding more features later

The project is split into two files intentionally:

- **`docker_vis/docker.py`** — pure data layer, returns dataclasses.
  Add new fetchers here (e.g. `docker system df` for cache sizes).
- **`docker_vis/widgets.py`** — pure UI layer, reads dataclasses.
  Add new list items and detail panels here.
- **`docker_vis/app.py`** — wires them together, handles layout and events.
