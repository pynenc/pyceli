# Tooling implemented based on the kubernetes library:
# https://github.com/kubernetes-client/python/blob/master/kubernetes/utils/create_from_yaml.py
import re

UPPER_FOLLOWED_BY_LOWER_RE = re.compile("(.)([A-Z][a-z]+)")
LOWER_OR_NUM_FOLLOWED_BY_UPPER_RE = re.compile("([a-z0-9])([A-Z])")


def get_api_func_ending(kind: str) -> str:
    """Returns the end of an api call based into the kind"""
    call_suffix = UPPER_FOLLOWED_BY_LOWER_RE.sub(r"\1_\2", kind)
    call_suffix = LOWER_OR_NUM_FOLLOWED_BY_UPPER_RE.sub(r"\1_\2", call_suffix).lower()
    return call_suffix


def get_available_api_methods(api: object, kind: str) -> list[str]:
    """Returns the available api methods for the object kind"""
    suffix = get_api_func_ending(kind)
    return [func for func in dir(api) if func.endswith(suffix)]


def is_namespaced(methods: list[str]) -> bool:
    """Returns True if the methods list contains a namespaced method"""
    return any("namespaced" in method for method in methods)


def build_api_method_name(method: str, namespaced: bool, kind: str) -> str:
    suffix = get_api_func_ending(kind)
    if namespaced:
        return f"{method}_namespaced_{suffix}"
    else:
        return f"{method}_{suffix}"
