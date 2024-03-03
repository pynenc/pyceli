from piceli.k8s.k8s_objects import compare


desired_spec = {
    "apiVersion": "v1",
    "kind": "PersistentVolumeClaim",
    "metadata": {
        "name": "example-persistentvolumeclaim",
        "namespace": "example-namespace",
    },
    "spec": {
        "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": "0.1Gi"}},
    },
}
existing_spec = {
    "apiVersion": "v1",
    "kind": "PersistentVolumeClaim",
    "metadata": {
        "creationTimestamp": "2024-03-01T10:19:10+00:00",
        "finalizers": ["kubernetes.io/pvc-protection"],
        "managedFields": [
            {
                "apiVersion": "v1",
                "fieldsType": "FieldsV1",
                "fieldsV1": "...",
                "manager": "OpenAPI-Generator",
                "operation": "Update",
                "time": "2024-03-01T10:19:10+00:00",
            }
        ],
        "name": "example-persistentvolumeclaim",
        "namespace": "example-namespace",
        "resourceVersion": "255202869",
        "uid": "d8eba4cf-c0b5-476f-beef-c691dd87c80a",
    },
    "spec": {
        "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": "107374182400m"}},
        "storageClassName": "standard-rwo",
        "volumeMode": "Filesystem",
    },
    "status": {"phase": "Pending"},
}


def test_find_differences() -> None:
    differences = compare.find_differences(desired_spec, existing_spec)
    assert differences.considered == []
    assert set(differences.defaults) == {
        compare.PathComparison(
            path=("spec", "volumeMode"), existing="Filesystem", desired=None
        ),
        compare.PathComparison(
            path=("spec", "storageClassName"), existing="standard-rwo", desired=None
        ),
    }
    assert set(differences.ignored) == {
        compare.PathComparison(
            path=("metadata", "finalizers"),
            existing=["kubernetes.io/pvc-protection"],
            desired=None,
        ),
        compare.PathComparison(
            path=("metadata", "managedFields"),
            existing=[
                {
                    "apiVersion": "v1",
                    "fieldsType": "FieldsV1",
                    "fieldsV1": "...",
                    "manager": "OpenAPI-Generator",
                    "operation": "Update",
                    "time": "2024-03-01T10:19:10+00:00",
                }
            ],
            desired=None,
        ),
        compare.PathComparison(
            path=("metadata", "uid"),
            existing="d8eba4cf-c0b5-476f-beef-c691dd87c80a",
            desired=None,
        ),
        compare.PathComparison(
            path=("metadata", "resourceVersion"), existing="255202869", desired=None
        ),
        compare.PathComparison(
            path=("metadata", "creationTimestamp"),
            existing="2024-03-01T10:19:10+00:00",
            desired=None,
        ),
        compare.PathComparison(
            path=("status",), existing={"phase": "Pending"}, desired=None
        ),
    }
