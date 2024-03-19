import pytest

from piceli.k8s.object_manager.base import ObjectManager
from piceli.k8s.ops import loader


@pytest.fixture
def object_manager() -> ObjectManager:
    """Create a k8s object manager."""
    filepath = ["tests/unit/object_manager/resources/cronjob.yml"]
    k8s_objects = loader.load_resources_from_files(filepath)
    return ObjectManager(next(k8s_objects))
