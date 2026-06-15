import subprocess
import json
from dataclasses import dataclass


@dataclass
class container:
    id: str
    name: str
    image: str
    status: str


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


def get_container() -> list[container] | None:
    raw_data = fetch_raw_containers()
    if not raw_data:
        return []

    containers = []

    for line in raw_data.splitlines():
        if not line:
            continue
        try:
            jsondata = json.loads(line)
            all_containers = container(
                id=jsondata.get("ID"),
                name=jsondata.get("Names"),
                image=jsondata.get("Image"),
                status=jsondata.get("Status"),
            )
            containers.append(all_containers)
        except json.JSONDecodeError:
            continue
    return containers


if __name__ == "__main__":
    raw_data = get_container()
    print(raw_data)
