from dataclasses import dataclass, field
from enum import Enum, auto
from typing import NamedTuple, Any

from kubernetes.utils.quantity import parse_quantity

from piceli.k8s.k8s_objects.base import K8sObject


class UpdateAction(Enum):
    EQUALS = auto()
    NEEDS_PATCH = auto()
    NEEDS_REPLACEMENT = auto()


class PathComparison(NamedTuple):
    path: tuple[str, ...]
    existing: Any
    desired: Any

    def __hash__(self) -> int:
        return hash(self.path)


@dataclass
class Differences:
    considered: list[PathComparison] = field(default_factory=list)
    ignored: list[PathComparison] = field(default_factory=list)
    defaults: list[PathComparison] = field(default_factory=list)

    def extend(self, other: "Differences") -> None:
        self.considered.extend(other.considered)
        self.ignored.extend(other.ignored)
        self.defaults.extend(other.defaults)


@dataclass
class CompareResult:
    update_action: UpdateAction
    differences: Differences

    def patch_document(self) -> dict:
        """Build the patch document from the considered differences."""
        patch: dict = {}
        for diff in self.differences.considered:
            current = patch
            for key in diff.path[:-1]:
                current = current.setdefault(key, {})
            current[diff.path[-1]] = diff.desired
        return patch

    @property
    def no_action_needed(self) -> bool:
        return self.update_action == UpdateAction.EQUALS

    @property
    def needs_patch(self) -> bool:
        return self.update_action == UpdateAction.NEEDS_PATCH

    @property
    def needs_replacement(self) -> bool:
        return self.update_action == UpdateAction.NEEDS_REPLACEMENT


# paths to ignore in exising and desired specs
# would always be overwritten by k8s
IGNORED_PATHS = {
    ("metadata", "creationTimestamp"),
    ("metadata", "finalizers"),
    ("metadata", "labels", "kubernetes.io/metadata.name"),
    ("metadata", "managedFields"),
    ("metadata", "resourceVersion"),
    ("metadata", "uid"),
    ("spec", "finalizers"),
    ("status",),
}

# paths to ignore if only exsits in existing_spec
# because they are default values and desired do not explicitly set them
# k8s will define them if not set in desired spec
DEFAULTED_PATHS = {
    ("spec", "storageClassName"),
    ("spec", "volumeMode"),
}


def is_path_ignored(path_comparison: PathComparison) -> bool:
    """Check if the path should be completely ignored."""
    return any(
        path_comparison.path[: len(ignored_path)] == ignored_path
        for ignored_path in IGNORED_PATHS
    )


def is_path_defaulted(path_comparison: PathComparison) -> bool:
    """Check if the path should be considered a default, ignored only if missing in desired."""
    return (path_comparison.path in DEFAULTED_PATHS) and (
        path_comparison.desired is None and path_comparison.existing is not None
    )


RESOURCE_KEYS = {"memory", "cpu", "ephemeral-storage", "storage"}


def are_values_equal(path_comparison: PathComparison) -> bool:
    """Determine if two values are different, considering special cases."""
    if path_comparison.existing == path_comparison.desired:
        return True
    if path_comparison.path[-1] in RESOURCE_KEYS:
        if path_comparison.existing and path_comparison.desired:
            return parse_quantity(path_comparison.existing) == parse_quantity(
                path_comparison.desired
            )
    return False


def compare_values(path_comparison: PathComparison) -> Differences:
    """Compare two values and create a Difference based on their comparison."""
    if are_values_equal(path_comparison):
        return Differences()
    if (
        path_comparison.desired is None or isinstance(path_comparison.desired, dict)
    ) and (
        path_comparison.existing is None or isinstance(path_comparison.existing, dict)
    ):
        return find_differences(
            desired_spec=path_comparison.desired or {},
            existing_spec=path_comparison.existing or {},
            prefix=path_comparison.path,
        )
    else:
        return Differences(considered=[path_comparison])


def find_differences(
    desired_spec: dict | None, existing_spec: dict | None, prefix: tuple[str, ...] = ()
) -> Differences:
    differences = Differences()
    _desired_spec, _existing_spec = desired_spec or {}, existing_spec or {}

    for key in set(_desired_spec).union(_existing_spec):
        path_comparison = PathComparison(
            path=prefix + (key,),
            existing=_existing_spec.get(key),
            desired=_desired_spec.get(key),
        )
        if is_path_ignored(path_comparison):
            differences.ignored.append(path_comparison)
        elif is_path_defaulted(path_comparison):
            differences.defaults.append(path_comparison)
        else:
            differences.extend(compare_values(path_comparison))
    return differences


def determine_update_action(desired: K8sObject, existing: K8sObject) -> CompareResult:
    kind = desired.kind
    return _determine_update_action(kind, desired.spec, existing.spec)


def filter_spec(spec: dict) -> dict:
    return {
        key: value for key, value in spec.items() if key not in ["status", "events"]
    }


def _determine_update_action(kind: str, desired: dict, existing: dict) -> CompareResult:
    filtered_desired_spec = filter_spec(desired)
    filtered_existing_spec = filter_spec(existing)
    differences = find_differences(filtered_desired_spec, filtered_existing_spec)
    considered = differences.considered
    if any(diff for diff in considered if requires_replacement(kind, diff.path)):
        return CompareResult(UpdateAction.NEEDS_REPLACEMENT, differences)
    elif considered:
        return CompareResult(UpdateAction.NEEDS_PATCH, differences)
    else:
        return CompareResult(UpdateAction.EQUALS, differences)


IMMUTABLE_FIELDS = {("spec", "selector"), ("spec", "template"), ("spec", "completions")}


def requires_replacement(kind: str, path: tuple[str, ...]) -> bool:
    # Check if any of the immutable field paths is a prefix of the current path
    if any(path[: len(field)] == field for field in IMMUTABLE_FIELDS):
        return True
    # "PersistentVolumeClaim"
    # "spec is immutable after creation except resources.requests for bound claims"
    if kind == "PersistentVolumeClaim" and path[0] == "spec":
        if path[1] != "resources":
            return True
    return False
