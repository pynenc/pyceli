import base64
import json
import os
from typing import Any, Dict, List, Optional

from k8s_client import k8s_client
from settings.settings_model import SETTINGS
import logger

log = logger.get_logger(__name__)


def get_docker_registry_secret_data() -> Dict[str, str]:
    """gets the data field for the docker secret from docker env vars"""
    if not SETTINGS.docker_auth:
        raise ValueError("SETTINGS.docker_auth needs to be specified in order to deploy")
    key_data_str = "_json_key:" + base64.b64decode(SETTINGS.docker_auth).decode()
    data = {
        "auths": {
            "gcr.io": {
                "auth": base64.b64encode(key_data_str.encode("utf8")).decode(),
            }
        }
    }
    return {".dockerconfigjson": base64.b64encode(json.dumps(data, separators=(",", ":")).encode()).decode()}


def get_configmap_data_from_files(filepaths: list[str], mappings: Optional[dict[str, str]] = None) -> dict[str, str]:
    """loads the filepath into a string and returns a dict [filename:filecontents]"""
    # this could easily support an array of filepath
    data = {}
    for filepath in filepaths:
        with open(filepath, "r", encoding="utf8") as _file:
            content = _file.read()
            if mappings:
                content = content.format(**mappings)
            data[os.path.basename(filepath)] = content
    return data


def wait_for_items(k8s: k8s_client.Kubernetes, dry_run: k8s_client.DryRun, items: List[Any]) -> None:
    """wait for each of the items in the list"""
    for item in items:
        if dry_run == k8s_client.DryRun.ON:
            log.warning("Running on dry_run: Not waiting for %s %s", type(item).__name__, item.name)
            continue
        item.wait(k8s)
