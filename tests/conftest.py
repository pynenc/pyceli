import difflib
import json
from typing import Any, Optional

import pytest


def pytest_assertrepr_compare(op: str, left: Any, right: Any) -> Optional[list[str]]:
    if isinstance(left, dict) and isinstance(right, dict) and op == "==":
        # Serialize the dictionaries to JSON formatted strings, sorted to ensure consistency
        left_str = json.dumps(left, sort_keys=True, indent=2).splitlines(keepends=True)
        right_str = json.dumps(right, sort_keys=True, indent=2).splitlines(
            keepends=True
        )
        # Generate the unified diff
        diff = list(
            difflib.unified_diff(left_str, right_str, fromfile="left", tofile="right")
        )
        if diff:
            return [
                "Dictionaries do not match:",
                *diff,
                # "left:",
                # *left_str,
                # "right:",
                # *right_str,
            ]
    return None


RESOURCE_JSON = '{"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "tasker-scheduler"}, "spec": {"template": {"metadata": {"name": "tasker-scheduler"}, "spec": {"containers": [{"command": ["sh", "-c", "echo \'scheduler\' && sleep 30"], "image": "busybox", "name": "tasker-scheduler"}], "restartPolicy": "Never"}}}}'


@pytest.fixture
def resource_dict() -> dict:
    """Parse JSON string to a Python dictionary."""
    return json.loads(RESOURCE_JSON)
