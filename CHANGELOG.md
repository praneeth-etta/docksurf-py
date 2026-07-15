# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/) — with
the caveat that pre-1.0 (`0.x`) minor bumps may still include breaking changes.

**Versioning convention:** bump `version` in `pyproject.toml`, move the
`[Unreleased]` entries below into a new `## [x.y.z] - YYYY-MM-DD` section,
commit, then tag the commit `vx.y.z` and push the tag — that triggers the
PyPI publish workflow.

## [Unreleased]

## [0.1.0] - 2026-07-15

### Added
- Compose-aware container grouping, with project-wide up/down/stop/start/restart
  and interleaved, colour-coded per-service logs.
- Live resource stats (CPU %, memory, network, block I/O) streamed into the
  detail pane for the selected running container.
- Full-control log viewer: live follow, in-log search, timestamps toggle,
  configurable tail depth / `--since` window, line wrap, and buffer export.
- Multi-select and bulk actions across containers, images, volumes, and networks.
- Inspect (raw `docker inspect` JSON, searchable) and prune (containers,
  dangling images, unused volumes/networks, or everything at once).
- Image, volume, and network management: pull with live progress, layer
  history, tagging, dangling-image bulk cleanup, per-volume disk size,
  network create/connect/disconnect.
- Power-user exec (custom command/user) and `docker cp` in/out of a container.
- `docker system df` disk usage breakdown on demand.
- In-app Docker context switching and auto-reconnect on daemon restart.
- Curated themes with cycling (`M`), and a network topology visualization.
