# Keybindings

Full keybinding reference for DockSurf. For the handful you'll use constantly, see the [main README](README.md#keybindings).

## Global

| Key     | Action                                                    |
|---------|------------------------------------------------------------|
| `r`     | Refresh all Docker data                                    |
| `/`     | Open search / filter for active tab                        |
| `i`     | Inspect focused resource — full `docker inspect` JSON, searchable (works on any tab) |
| `P`     | Prune menu — stopped containers / dangling images / unused volumes / unused networks / everything |
| `w`     | Disk-usage screen (`docker system df`)                     |
| `D`     | Switch Docker context — in-app only, never touches `docker context use` (see [Docker contexts](README.md#docker-contexts--local-and-remote)) |
| `space` | Mark the focused row for a bulk action (or collapse/expand a Compose project header — see below) |
| `escape`| Clear all marks on the active tab (no-op if nothing's marked) |
| `?`     | Help screen                                                 |
| `q`     | Quit                                                        |
| `d`     | Delete the selected resource — or every marked resource — with confirmation |
| `Tab`   | Switch between tabs                                         |
| `↑/↓`   | Navigate rows                                               |
| `1`-`4` | Jump directly to the Containers / Images / Volumes / Networks tab |
| `[` / `]` | Previous / next tab                                       |

**Multi-select + bulk actions**: mark any number of rows with `space` (on any tab), then `s`/`S`/`d` act on the whole marked set instead of just the focused row — e.g. mark five stopped test containers and `d` once to remove them all behind a single confirmation. Bulk stop/start silently skips marked containers that don't qualify (already stopped/running); bulk delete reuses each resource's normal guards (in-use volumes, built-in networks aren't touched).

## Containers tab

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
| `B` | Rebuild image from source + recreate this container (Compose services only; skipped for image-only services, e.g. `postgres`), streamed live | — |
| `ctrl+u` | Compose **up** — brings the whole project up (`docker compose up -d`) | Compose **up** — brings the whole project up |
| `ctrl+k` | Compose **down** — tears the whole project down (confirmed) | Compose **down** — tears the whole project down |
| `space` | Mark for bulk action                          | Collapse / expand project group  |

## Images / Volumes / Networks tabs

Like the container keys, these are tab-scoped — each acts on the focused row of its tab (and no-ops with a hint elsewhere). `+` is shared: it creates/pulls whatever the active tab holds.

| Key | Tab       | Action                                                             |
|-----|-----------|--------------------------------------------------------------------|
| `+` | Images    | **Pull** an image (`name:tag`) with a live progress view           |
| `+` | Volumes   | **Create** a volume (name / driver / labels prompt)                |
| `+` | Networks  | **Create** a network (name / driver / subnet prompt)               |
| `h` | Images    | **Layer history** (`docker history`) — per-layer command + size    |
| `y` | Images    | **Tag** the selected image (repository / tag prompt)               |
| `a` | Images    | **Mark all dangling** images — then `d` removes them as a batch    |
| `b` | Volumes   | **Size on disk** for the selected volume (on-demand; it's slow)    |
| `v` | Networks  | **Connect** a container to the network (pick from a list)          |
| `m` | Networks  | **Disconnect** a container from the network (pick from a list)     |

## Log pane (when open)

| Key       | Action                                                           |
|-----------|--------------------------------------------------------------------|
| `f`       | Toggle live log follow (pause / resume)                          |
| `/`       | Filter to matching lines; matches are highlighted (Esc to clear) |
| `n` / `N` | Jump to next / previous match (with a `k/N` counter in the header) |
| `T`       | Show / hide timestamps                                           |
| `o`       | Log options — tail depth (100 / 500 / 5000 / all) and a `--since` window |
| `W`       | Toggle line wrap (for long JSON lines)                           |
| `g` / `G` | Jump to top / bottom of the buffer                               |
| `X`       | Export the buffer to a file (`~/.local/share/docksurf/exports/`) |
| `z`       | Expand log pane to full width / collapse                         |
| `l`       | Close log viewer, return to detail pane                          |

The `⛶ Expand` / `⊡ Collapse` button in the log toolbar is also clickable.

**Mouse drag-to-select**: drag over log text to highlight it, then `Ctrl+C` to copy — Textual's log widget has no built-in selection support, so DockSurf implements it directly.
