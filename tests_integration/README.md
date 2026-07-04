# Integration tests (live Docker daemon)

This directory is **not** part of the default test suite. `pytest tests/`
(what CI runs, and what `CLAUDE.md` documents as the standard dev command)
does not collect anything here — that's intentional, not an oversight.

## Why this is separate from `tests/`

`tests/` is entirely mock-based (`MockDockerService`, a fake SDK client via
`unittest.mock`). That's the right default: fast, deterministic, no
external dependencies, safe to run on every push. But it can't catch
everything — SDK/CLI behavior drift, real `docker cp` semantics, actual
`container.top()` output shapes, and similar are invisible to a mock by
construction.

This suite fills that gap by running `DockerClient` against a real
`dockerd`. It's opt-in because it:

- requires a running Docker daemon + the `docker` CLI on `PATH`
- is slower (spins up real containers, ~20-40s total)
- is unnecessary on every commit — the mocked suite already covers the
  logic; this covers "does it actually work against a real daemon"

## Running it

```bash
uv run python -m pytest tests_integration/ -v
```

Tests skip themselves cleanly (not error) if Docker isn't available.

## Safety

Every container/volume/network/image this suite creates is named with a
`docksurf-it-` prefix and removed in `tearDown` — including on test
failure. If a run is killed mid-way (e.g. `Ctrl-C`) and something is left
behind, clean up with:

```bash
docker ps -a --filter "name=docksurf-it-" -q | xargs -r docker rm -f
docker volume ls --filter "name=docksurf-it-" -q | xargs -r docker volume rm -f
docker network ls --filter "name=docksurf-it-" -q | xargs -r docker network rm
docker images --filter "reference=docksurf-it-*" -q | xargs -r docker rmi -f
```

**`prune_*` methods are deliberately not tested here.** Every prune method
(`prune_containers`, `prune_images`, `prune_volumes`, `prune_networks`,
`prune_system`) acts on the *entire* daemon, not just resources this suite
created — running one for real would risk deleting a developer's unrelated
stopped containers, dangling images, or networks from other work. Their
message-formatting and SDK-call-shape logic is already covered by mocked
tests in `tests/test_docker.py`; that's the appropriate layer for them.

## Adding a test

Extend `_LiveDockerTestCase` (in `test_live_docker.py`) for the
container/volume/network fixture helpers and guaranteed cleanup. Give any
new resource a name via `_unique_name(...)` (keeps the `docksurf-it-`
prefix) and register it for cleanup (`self._containers`/`self._volumes`/
`self._networks`) as soon as it's created — before any assertion that
could fail.
