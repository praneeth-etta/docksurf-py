"""Test the data layer independently: `uv run python -m docksurf_py.docker`."""

from docksurf_py.docker.client import DockerClient

if __name__ == "__main__":
    dc = DockerClient()
    snapshot = dc.fetch_snapshot()  # triggers the lazy connect
    if dc.is_connected:
        print(snapshot)
    else:
        print("Could not connect to Docker daemon")
