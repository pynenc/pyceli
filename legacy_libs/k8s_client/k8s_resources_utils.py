from typing import Optional

from k8s_client import k8s_resources, constants as const
from k8s_client.k8s_model_lib import Pod, Container
import logger

log = logger.get_logger(__name__)


def get_container_in_cluster(
    pod: Pod,
    container_name: str,
    cluster_resources: k8s_resources.ClusterResources,
) -> Optional[k8s_resources.ContainerResourcesData]:
    """get the container in the cluster resources"""
    if pod_in_cluster := cluster_resources.get_pod(pod.name):
        if container_in_cluster := pod_in_cluster.containers.get(container_name):
            return container_in_cluster
    return None


def increase_container_resources_if_necessary(
    container: k8s_resources.ContainerResourcesData,
    usage_threshold: float,
    increase_factor: float,
) -> dict[str, float]:
    """increase the container resources if necessary"""
    requested_quantities = container.requested_resources.to_quantity_dict()
    usage_quantities = container.used_resources.to_quantity_dict()
    increased_quantities: dict[str, float] = {}
    for quantity, requested_quantity in list(requested_quantities.items()):
        used_quantity = usage_quantities[quantity]
        if not requested_quantity or not used_quantity:
            log.warning(
                f"Not enough information to calculate usage for {quantity=} {container.container_name=}"
            )
            continue
        usage = used_quantity / requested_quantity
        if usage > usage_threshold:
            increased_quantities[quantity] = (
                max(used_quantity, requested_quantity) * increase_factor
            )
            log.info(
                f"Increasing {container.container_name=} resource {quantity} ({used_quantity=} {requested_quantity=}) "
                f"by factor {increase_factor} to {increased_quantities[quantity]}"
            )
    return increased_quantities


def calculate_container_average_resources(
    container: Container,
    cluster_resources: k8s_resources.ClusterResources,
) -> dict[str, Optional[float]]:
    """will calculate the average used resources of existing pods in the cluster wtih same name"""
    log.info(f"Calculating average resources for {container.name=}")
    sum_quantities: dict[str, float] = {}
    count_quantities: dict[str, int] = {}
    for pod_in_cluster in cluster_resources.pods_map.values():
        if container.name not in pod_in_cluster.containers:
            continue
        log.info(
            f"considering existing pod {pod_in_cluster.pod_name} resources on average calculation"
        )
        requested_quantities = pod_in_cluster.containers[
            container.name
        ].requested_resources.to_quantity_dict()
        for quantity, requested_quantity in list(requested_quantities.items()):
            if not requested_quantity:
                continue
            sum_quantities[quantity] = (
                sum_quantities.get(quantity, 0) + requested_quantity
            )
            count_quantities[quantity] = count_quantities.get(quantity, 0) + 1
    return {
        quantity: total / count_quantities[quantity]
        for quantity, total in sum_quantities.items()
    }


def increase_pod_resources_if_necessary(
    pod: Pod,
    cluster_resources: k8s_resources.ClusterResources,
    increase_vertically: bool,
    usage_threshold: float,
    increase_factor: float,
    max_cluster_allocatable_threshold: float,
    compute_class: const.ComputeClassLimits,
) -> bool:
    """
    If the database already exists, copy existing resources to avoid decreasing them
    Increase cluster resources by a factor of increase_factor if the usage threshold is exceeded
    In case of a new data node, increase the resources by the average of the other data nodes
    return True if the pod resources were increased, False otherwise
    """
    any_change = False
    for container in pod.containers:
        quantities_to_apply = {}
        if container_in_cluster := get_container_in_cluster(
            pod, container.name, cluster_resources
        ):
            log.info(
                f"Existing Pod {pod.name} checking if resources need to be increased"
            )
            # update pod resources with current requested resources
            pod.container_map[
                container.name
            ].resources = container_in_cluster.requested_resources
            if not increase_vertically:
                continue
            # check if the container resources need to be increased
            increased_quantities = increase_container_resources_if_necessary(
                container_in_cluster, usage_threshold, increase_factor
            )
            if not increased_quantities:
                log.info(
                    f"Pod {pod.name} container {container_in_cluster.container_name} resources are within limits"
                )
                continue
            requested_quantities = (
                container_in_cluster.requested_resources.to_quantity_dict()
            )
            requested_quantities.update(increased_quantities)
            quantities_to_apply = requested_quantities
        elif cluster_resources.pods_map:
            log.info(
                f"New {pod.name=} {container.name} will be initialized with the average of the other data nodes"
            )
            if quantities_to_apply := calculate_container_average_resources(
                container, cluster_resources
            ):
                log.info(
                    f"Initializing pod {pod.name} with resource request {quantities_to_apply=}"
                )
        if quantities_to_apply:
            # An increase it's necessary, update the pod resources within limits (cluster resources and compute class)
            request_resource = apply_all_filters(
                k8s_resources.PodResources.from_quantity_dict(quantities_to_apply),
                compute_class=compute_class,
                cluster_resources=cluster_resources,
                max_cluster_allocatable_threshold=max_cluster_allocatable_threshold,
            )
            pod.container_map[container.name].resources = request_resource
            any_change = True
    return any_change


def apply_all_filters(
    request_resource: k8s_resources.PodResources,
    compute_class: const.ComputeClassLimits,
    cluster_resources: k8s_resources.ClusterResources,
    max_cluster_allocatable_threshold: float,
) -> k8s_resources.PodResources:
    """modify the required resources to comply with the compute class cpu/mem ratios and cluster max allocatable limit"""
    request_resource = apply_cpu_mem_ratios(
        request_resource, compute_class=compute_class
    )
    request_resource = apply_cluster_max_allocatable_limit(
        request_resource,
        cluster_resources,
        max_allocatable_threshold=max_cluster_allocatable_threshold,
    )
    request_resource = apply_pod_limits(request_resource, compute_class=compute_class)
    return request_resource


def apply_cpu_mem_ratios(
    required_resources: k8s_resources.PodResources,
    compute_class: const.ComputeClassLimits,
) -> k8s_resources.PodResources:
    """modify the required resources to comply with the compute class cpu/mem ratios"""
    if required_resources == k8s_resources.PodResources():
        log.debug(
            f"requesting minimum resources, no necessary to apply {compute_class=} cpu/mem ratios"
        )
        return required_resources
    # apply compute class minimuls to null values
    if not required_resources.cpu or not required_resources.memory:
        log.warning(
            f"cpu or mem missing on  {required_resources=} applying {compute_class=} min values"
        )
        required_resources = k8s_resources.PodResources(
            cpu=required_resources.cpu or compute_class.min_cpu,
            memory=required_resources.memory or compute_class.min_memory,
            ephemeral_storage=required_resources.ephemeral_storage,
        )
    required_ratio = required_resources.get_cpu_memory_ratio()
    resource_dict = required_resources.to_quantity_dict()
    if required_ratio > compute_class.max_cpu_memory_ratio:
        msg = f"{required_resources=} cpu/mem ratio {required_ratio} excess {compute_class=}."
        required_increase = required_ratio / compute_class.max_cpu_memory_ratio
        resource = "cpu"
    elif required_ratio < compute_class.min_cpu_memory_ratio:
        msg = f"{required_resources=} cpu/mem ratio {required_ratio} below {compute_class=}."
        required_increase = compute_class.min_cpu_memory_ratio / required_ratio
        resource = "memory"
        log.warning(msg)
    else:
        log.info(
            f"{required_resources=} cpu/mem ratio {required_ratio} within {compute_class=}."
        )
        return required_resources
    resource_dict[resource] *= required_increase  # type: ignore
    return k8s_resources.PodResources.from_quantity_dict(resource_dict)


def apply_cluster_max_allocatable_limit(
    required_resources: k8s_resources.PodResources,
    cluster_resources: k8s_resources.ClusterResources,
    max_allocatable_threshold: float,
) -> k8s_resources.PodResources:
    """modify the required resources to comply with the cluster max allocatable limit"""
    required_dict = required_resources.to_quantity_dict()
    max_allocatable = cluster_resources.total_allocatable * max_allocatable_threshold
    max_allocatable_dict = max_allocatable.to_quantity_dict()
    msg: list[str] = []
    for resource, quantity in required_dict.items():
        if quantity and quantity > (max_allocatable_dict.get(resource) or quantity):
            msg.append(
                f"{resource=} {quantity=} exceed {max_allocatable_dict[resource]=}"
            )
            required_dict[resource] = max_allocatable_dict[resource]
    if msg:
        log.warning(", ".join(msg) + f" for {required_resources=}.")
        return k8s_resources.PodResources.from_quantity_dict(required_dict)
    return required_resources


def apply_pod_limits(
    required_resources: k8s_resources.PodResources,
    compute_class: const.ComputeClassLimits,
) -> k8s_resources.PodResources:
    """modify the required resources to comply with the pod limits"""
    max_pod_resources = {
        "cpu": float(k8s_resources.parse_quantity(compute_class.max_cpu)),
        "memory": float(k8s_resources.parse_quantity(compute_class.max_memory)),
        "ephemeral-storage": float(
            k8s_resources.parse_quantity(const.MAX_EPHEMERAL_STORAGE)
        ),
    }
    trimmed_quantities = {}
    for resource, quantity in required_resources.to_quantity_dict().items():
        max_quantity = max_pod_resources[resource]
        if quantity and quantity > max_quantity:
            trimmed_quantities[resource] = max_quantity
    if trimmed_quantities:
        requested_quantities = required_resources.to_quantity_dict()
        requested_quantities.update(trimmed_quantities)
        log.warning(
            f"{required_resources=} exceed {max_pod_resources=} trimmed to {requested_quantities=}"
        )
        return k8s_resources.PodResources.from_quantity_dict(requested_quantities)
    return required_resources
