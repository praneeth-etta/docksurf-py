import subprocess
import json
from dataclasses import dataclass


@dataclass
class Container:
    id: str
    name: str
    image: str
    status: str


@dataclass
class Image:
    id: str
    repository: str
    tag: str
    size: str
    is_dangling: bool


@dataclass
class Volume:
    name: str
    driver: str
    mountpoint: str


@dataclass
class Network:
    id: str
    name: str
    driver: str
    scope: str


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
            ["docker", "volume", "ls", "--format", "{{json .}}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return ""


def get_container() -> list[Container] | None:
    raw_data = fetch_raw_containers()
    if not raw_data:
        return []

    containers = []

    for line in raw_data.splitlines():
        if not line:
            continue
        try:
            jsondata = json.loads(line)
            all_containers = Container(
                id=jsondata.get("ID"),
                name=jsondata.get("Names"),
                image=jsondata.get("Image"),
                status=jsondata.get("Status"),
            )
            containers.append(all_containers)
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
            )
            networks.append(all_volumes)
        except json.JSONDecodeError:
            continue
    return networks


if __name__ == "__main__":
    container_data, image_data, volume_data, network_data = (
        get_container(),
        get_image(),
        get_volume(),
        get_network(),
    )
    print(container_data)
    print(image_data)
    print(volume_data)
    print(network_data)
