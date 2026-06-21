import json
import logging
import subprocess
from collections import defaultdict
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Container:
    id: str
    name: str
    image_id: str
    image_name: str
    status: str
    ports: list[str]
    mounts: list[str]
    networks: list[str]
    created: str


@dataclass(slots=True)
class Image:
    id: str
    repository: str
    tag: str
    size: str
    is_dangling: bool
    used_by: list[str]
    created: str
    architecture: str


@dataclass(slots=True)
class Volume:
    name: str
    driver: str
    mountpoint: str
    used_by: list[str]
    labels: str


@dataclass(slots=True)
class Network:
    id: str
    name: str
    driver: str
    subnet: str
    gateway: str
    scope: str
    used_by: list[str]


@dataclass(slots=True)
class DockerSnapshot:
    containers: list[Container]
    images: list[Image]
    volumes: list[Volume]
    networks: list[Network]


def parse_json_lines(raw: str):
    for line in raw.splitlines():
        if line.strip():
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                logger.warning(
                    "Failed to parse Docker JSON line: %s",
                    exc,
                )
                continue


def run_docker_command(*args: str) -> str:
    try:
        result = subprocess.run(
            ["docker", *args], capture_output=True, text=True, check=True
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as exc:
        logger.warning(
            "Docker command failed: docker %s",
            " ".join(args),
        )
        logger.debug(
            "stderr: %s",
            exc.stderr,
        )
        return ""


def inspect_containers(container_ids: list[str]) -> dict:
    if not container_ids:
        return {}

    result = subprocess.run(
        ["docker", "inspect", *container_ids],
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        logger.warning(
            "docker inspect failed for %d containers",
            len(container_ids),
        )
        return {}

    if not result.stdout:
        return {}

    inspect_data = json.loads(result.stdout)

    return {item["Id"][:12]: item for item in inspect_data}


def inspect_networks(network_ids: list[str]) -> dict:
    if not network_ids:
        return {}

    result = subprocess.run(
        ["docker", "inspect", *network_ids],
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        logger.warning("docker inspect failed for %d networks", len(network_ids))
        return {}

    if not result.stdout:
        return {}

    inspect_data = json.loads(result.stdout)
    lookup = {}

    for item in inspect_data:
        net_id = item.get("Id", "")[:12]
        subnet = "N/A"
        gateway = "N/A"

        ipam_config = item.get("IPAM", {}).get("Config", {})
        if ipam_config and isinstance(ipam_config, list) and len(ipam_config) > 0:
            subnet = ipam_config[0].get("Subnet", "N/A")
            gateway = ipam_config[0].get("Gateway", "N/A")

        lookup[net_id] = {"subnet": subnet, "gateway": gateway}

    return lookup


def fetch_raw_containers() -> str:
    return run_docker_command("ps", "-a", "--format", "{{json .}}")


def fetch_raw_images() -> str:
    return run_docker_command("images", "-a", "--no-trunc", "--format", "{{json .}}")


def fetch_raw_volumes() -> str:

    return run_docker_command("volume", "ls", "--format", "{{json .}}")


def fetch_raw_networks() -> str:

    return run_docker_command("network", "ls", "--format", "{{json .}}")


def get_containers() -> list[Container]:
    raw_data = fetch_raw_containers()
    if not raw_data:
        return []

    container_rows = list(parse_json_lines(raw_data))

    container_ids = [row["ID"] for row in container_rows]

    inspect_lookup = inspect_containers(container_ids)

    containers = []

    for row in container_rows:
        cid = row["ID"]

        inspect = inspect_lookup.get(cid, {})

        mounts = [
            m.get("Name") or m.get("Source", "")
            for m in inspect.get("Mounts", [])
            if m.get("Name") or m.get("Source")
        ]

        networks = list(inspect.get("NetworkSettings", {}).get("Networks", {}).keys())

        c = Container(
            id=cid,
            name=row.get("Names", "").lstrip("/"),
            image_id=inspect.get("Image", ""),
            image_name=row.get("Image", ""),
            status=row.get("Status", ""),
            ports=row.get("Ports", ""),
            mounts=mounts,
            networks=networks,
            created=row.get("CreatedAt", ""),
        )
        containers.append(c)
    return containers


def get_images() -> list[Image]:
    raw_data = fetch_raw_images()
    if not raw_data:
        return []

    images = []

    for data in parse_json_lines(raw_data):
        all_images = Image(
            id=data.get("ID"),
            repository=data.get("Repository"),
            tag=data.get("Tag"),
            size=data.get("Size"),
            is_dangling=(
                data.get("Repository") == "<none>" and data.get("Tag") == "<none>"
            ),
            used_by=[],
            created=data.get("CreatedAt", ""),
            architecture=data.get("Architecture"),
        )
        images.append(all_images)

    return images


def get_volumes() -> list[Volume]:
    raw_data = fetch_raw_volumes()
    if not raw_data:
        return []

    volumes = []

    for data in parse_json_lines(raw_data):
        all_volumes = Volume(
            name=data.get("Name"),
            driver=data.get("Driver"),
            mountpoint=data.get("Mountpoint"),
            used_by=[],
            labels=data.get("Labels"),
        )
        volumes.append(all_volumes)
    return volumes


def get_networks() -> list[Network]:
    raw_data = fetch_raw_networks()
    if not raw_data:
        return []

    network_rows = list(parse_json_lines(raw_data))
    network_ids = [row.get("ID") for row in network_rows if row.get("ID")]
    inspect_lookup = inspect_networks(network_ids)

    networks = []

    for data in network_rows:
        nid = data.get("ID", "")
        net_info = inspect_lookup.get(nid, {})

        all_networks = Network(
            id=nid,
            name=data.get("Name", ""),
            driver=data.get("Driver", ""),
            subnet=net_info.get("subnet", "N/A"),
            gateway=net_info.get("gateway", "N/A"),
            scope=data.get("Scope", ""),
            used_by=[],
        )
        networks.append(all_networks)
    return networks


def fetch_snapshot() -> DockerSnapshot:
    containers, images, volumes, networks = (
        get_containers(),
        get_images(),
        get_volumes(),
        get_networks(),
    )

    image_usage = defaultdict(list)
    volume_usage = defaultdict(list)
    network_usage = defaultdict(list)

    for c in containers:
        image_usage[c.image_id].append(c.name)

        for mount in c.mounts:
            volume_usage[mount].append(c.name)

        for network in c.networks:
            network_usage[network].append(c.name)

    for image in images:
        image.used_by.extend(image_usage.get(image.id, []))

    for volume in volumes:
        volume.used_by.extend(volume_usage.get(volume.name, []))

    for network in networks:
        network.used_by.extend(network_usage.get(network.name, []))

    return DockerSnapshot(containers, images, volumes, networks)


if __name__ == "__main__":
    print(fetch_snapshot())
