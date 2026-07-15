# Quickstart

Get DockSurf running in a couple of minutes.

## 1. Install

From [PyPI](https://pypi.org/project/docksurf/):

```bash
pip install docksurf
```

Or without installing, via [`uvx`](https://docs.astral.sh/uv/guides/tools/):

```bash
uvx docksurf
```

From source (development, or an unreleased change):

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

## 2. Launch

```bash
docksurf
```

By default DockSurf connects to whatever your active `docker context` points at — same precedence the `docker` CLI itself uses: `DOCKER_HOST` → active context → default socket.

To connect elsewhere for a single run, without touching your global `docker context`:

```bash
docksurf --host tcp://remote-host:2375   # a specific daemon endpoint
docksurf --context my-context            # a named context, this run only
docksurf --config ~/alt-config.toml      # a non-default config.toml
```

## 3. First things to try

- `↑` / `↓` and `Tab` — move around the table and switch between Containers / Images / Volumes / Networks tabs (or jump straight there with `1`-`4`)
- Select a running container and glance at the detail pane — image, ports, uptime, live CPU/mem/net stats
- `l` — open the log viewer for the selected container
- `/` — search / filter the current tab
- `s` / `S` / `x` — stop / start / restart the focused container
- `space` then `d` — mark a few rows and delete them as a batch
- `?` — the full help screen; your fallback whenever you forget a key

## 4. Go deeper

- [KEYBINDINGS.md](KEYBINDINGS.md) — every key, every tab, the log pane
- [README.md](README.md) — highlights, architecture, Docker contexts (including remote hosts), logging
