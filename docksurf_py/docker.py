import json
import subprocess
from dataclasses import dataclass


@dataclass
class Container:
    id: str
    name: str
    image: str
    status: str
    mounts: list[str]
    networks: list[str]


@dataclass
class Image:
    id: str
    repository: str
    tag: str
    size: str
    is_dangling: bool
    used_by: list[str]


@dataclass
class Volume:
    name: str
    driver: str
    mountpoint: str
    used_by: list[str]


@dataclass
class Network:
    id: str
    name: str
    driver: str
    scope: str
    used_by: list[str]


@dataclass
class DockerSnapshot:
    containers: list[Container]
    images: list[Image]
    volumes: list[Volume]
    networks: list[Network]


def fetch_raw_containers() -> str:
    try:
        result = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{json .}}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return ""


def fetch_raw_images() -> str:
    try:
        result = subprocess.run(
            ["docker", "images", "-a", "--format", "{{json .}}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return ""


def fetch_raw_volumes() -> str:
    try:
        result = subprocess.run(
            ["docker", "volume", "ls", "--format", "{{json .}}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return ""


def fetch_raw_networks() -> str:
    try:
        result = subprocess.run(
            ["docker", "network", "ls", "--format", "{{json .}}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return ""


def get_container() -> list[Container]:
    raw_data = fetch_raw_containers()
    if not raw_data:
        return []

    containers = []

    for line in raw_data.splitlines():
        if not line:
            continue
        try:
            data = json.loads(line)
            cid = data.get("ID", "")

            inspect_raw = subprocess.run(
                ["docker", "inspect", "--format", "{{json .}}", cid],
                stdout=subprocess.PIPE,
                text=True,
                check=False,
            ).stdout.strip()

            mount_names = []
            network_names = []

            if inspect_raw:
                inspect_data = json.loads(inspect_raw)

                for m in inspect_data.get("Mounts", []):
                    vol_name = m.get("Name") or m.get("Source", "")
                    if vol_name:
                        mount_names.append(vol_name)

                net_dict = inspect_data.get("NetworkSettings", {}).get("Networks", {})
                network_names = list(net_dict.keys())

            c = Container(
                id=cid,
                name=data.get("Names", "").lstrip("/"),
                image=data.get("Image", ""),
                status=data.get("Status", ""),
                mounts=mount_names,
                networks=network_names,
            )
            containers.append(c)
        except json.JSONDecodeError:
            continue
    return containers


def get_image() -> list[Image]:
    raw_data = fetch_raw_images()
    if not raw_data:
        return []

    images = []

    for line in raw_data.splitlines():
        if not line:
            continue
        try:
            jsondata = json.loads(line)
            all_images = Image(
                id=jsondata.get("ID"),
                repository=jsondata.get("Repository"),
                tag=jsondata.get("Tag"),
                size=jsondata.get("Size"),
                is_dangling=(
                    jsondata.get("Repository") == "<none>"
                    and jsondata.get("Tag") == "<none>"
                ),
                used_by=[],
            )
            images.append(all_images)
        except json.JSONDecodeError:
            continue
    return images


def get_volume() -> list[Volume]:
    raw_data = fetch_raw_volumes()
    if not raw_data:
        return []

    volumes = []

    for line in raw_data.splitlines():
        if not line:
            continue
        try:
            jsondata = json.loads(line)
            all_volumes = Volume(
                name=jsondata.get("Name"),
                driver=jsondata.get("Driver"),
                mountpoint=jsondata.get("Mountpoint"),
                used_by=[],
            )
            volumes.append(all_volumes)
        except json.JSONDecodeError:
            continue
    return volumes


def get_network() -> list[Network]:
    raw_data = fetch_raw_networks()
    if not raw_data:
        return []

    networks = []

    for line in raw_data.splitlines():
        if not line:
            continue
        try:
            jsondata = json.loads(line)
            all_volumes = Network(
                id=jsondata.get("ID"),
                name=jsondata.get("Name"),
                driver=jsondata.get("Driver"),
                scope=jsondata.get("Scope"),
                used_by=[],
            )
            networks.append(all_volumes)
        except json.JSONDecodeError:
            continue
    return networks


def fetch_snapshot() -> DockerSnapshot:
    containers, images, volumes, networks = (
        get_container(),
        get_image(),
        get_volume(),
        get_network(),
    )

    for i in images:
        for c in containers:
            if c.image == i.repository or c.image == f"{i.repository}:{i.tag}":
                i.used_by.append(c.name)

    for v in volumes:
        for c in containers:
            if v.name in c.mounts:
                v.used_by.append(c.name)

    for n in networks:
        for c in containers:
            if n.name in c.networks:
                n.used_by.append(c.name)

    return DockerSnapshot(containers, images, volumes, networks)


if __name__ == "__main__":
    print(fetch_snapshot())
