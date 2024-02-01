# pylint: disable=too-many-lines
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import json
import re
from multiprocessing.pool import ApplyResult
import time
from typing import Any, Callable, Dict, List, Optional, ClassVar, Iterator, Protocol

from kubernetes.utils.quantity import parse_quantity
from kubernetes.client.exceptions import ApiException
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_fixed,
    after_log,
)
from urllib3.exceptions import HTTPError

from k8s_client import k8s_client
from k8s_client.k8s_client import client
from k8s_client.k8s_resources import PodResources
from k8s_client.constants import (
    DEFAULT_CONFIGMAP_NAME,
    DEFAULT_SECRET_NAME,
    PULL_SECRET_NAME,
    DEFAULT_NAMESPACE,
    WAIT_TIMEOUT,
    DELETE_TIMEOUT,
    BACKOFF_LIMIT_JOB,
    ComputeClasses,
)
from src.errors import K8SPullImageError
import logger

log = logger.get_logger(__name__)

# Accepted in most names
# K8S_NAME_RE = re.compile("[a-z0-9]([-a-z0-9]*[a-z0-9])?(\\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*")
# Most restrictive names
K8S_NAME_RE = re.compile("[a-z0-9]([-a-z0-9]*[a-z0-9])?")


@dataclass  # type: ignore
class K8sModel(ABC):
    """PaaS Kubernetes Model"""

    name: str
    API: ClassVar[str]
    API_FUNC: ClassVar[str]
    NAMESPACED: ClassVar[bool] = True

    def __post_init__(self) -> None:
        if not bool(K8S_NAME_RE.fullmatch(self.name)):
            # a lowercase RFC 1123 subdomain must consist of lower case alphanumeric characters, '-' or '.',
            # and must start and end with an alphanumeric character
            # (e.g. 'example.com', regex used for validation is '[a-z0-9]([-a-z0-9]*[a-z0-9])?(\\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*')
            raise ValueError(
                f"Invalid name '{self.name}', should be lower-case a-z (any position) and '.' '-' (not first) "
            )

    def __hash__(self) -> int:
        return hash(self.name)

    @property
    def namespaced(self) -> str:
        """returns _namespaced if the object is namespaced"""
        if self.NAMESPACED:
            return "_namespaced"
        return ""

    @abstractmethod
    def get(self, k8s: k8s_client.Kubernetes) -> client.V1PersistentVolume:
        """get the k8s object to apply"""

    def check_authorization(self, k8s: k8s_client.Kubernetes) -> None:
        """check that is running in a k8s instance with enough permissions"""
        resource = f"{self.__class__.__name__.lower()}s"
        for verb in list(k8s_client.APIRequestVerb):
            if k8s.check_authorization(
                self.API, resource, verb.value, DEFAULT_NAMESPACE
            ):
                log.info(
                    "current client authorized on resource %s verb %s", resource, verb
                )
            else:
                log.warning(
                    "current client NOT authorized on resource %s verb %s",
                    resource,
                    verb,
                )

    @classmethod
    def get_auth_role(
        cls, verbs: Optional[List[k8s_client.APIRequestVerb]] = None
    ) -> "Role":
        """gets the role necessaries to authorize a service account on this the K8s object"""
        resource = cls.__name__.lower()
        if verbs:
            name = resource + "-" + "-".join(sorted(v.value for v in verbs))
            if len(name) > 30:
                name = resource + "-" + "".join(sorted(v.value[0] for v in verbs))
            return Role(name, cls.API, resource + "s", verbs)
        return Role(
            resource + "-full", cls.API, resource + "s", list(k8s_client.APIRequestVerb)
        )

    def api(
        self, k8s: k8s_client.Kubernetes
    ) -> client.CoreV1Api | client.BatchV1Api | client.AppsV1Api:
        """api required by the kubernetes object"""
        return getattr(k8s, f"{self.API}_api")

    def read(self, k8s: k8s_client.Kubernetes) -> Any:
        """reads the k8s object from the cluster, exception if not exists"""
        # return self.api(k8s).read_persistent_volume(self.name)
        if self.NAMESPACED:
            args: tuple = (self.name, DEFAULT_NAMESPACE)
        else:
            args = (self.name,)
        return getattr(self.api(k8s), f"read{self.namespaced}_{self.API_FUNC}")(*args)

    def patch(
        self,
        k8s: k8s_client.Kubernetes,
        async_req: bool = False,
        dry_run: k8s_client.DryRun = k8s_client.DryRun.OFF,
    ) -> Any | ApplyResult:
        """patch the existing k8s object"""
        if self.NAMESPACED:
            args: tuple = (self.name, DEFAULT_NAMESPACE, self.get(k8s))
        else:
            args = (self.name, self.get(k8s))
        return getattr(self.api(k8s), f"patch{self.namespaced}_{self.API_FUNC}")(
            *args, async_req=async_req, dry_run=dry_run.value
        )

    def create(
        self,
        k8s: k8s_client.Kubernetes,
        async_req: bool = False,
        dry_run: k8s_client.DryRun = k8s_client.DryRun.OFF,
    ) -> Any | ApplyResult:
        """create the k8s object"""

        class RetryException(Exception):
            """Exception to Retry creation"""

        @retry(
            retry=retry_if_exception_type(RetryException),
            stop=stop_after_attempt(10),
            wait=wait_fixed(1),
            after=after_log(log, logger.logging.INFO),
        )
        def _create() -> Any | ApplyResult:
            log.info("creating %s %s", _type := type(self).__name__, self.name)
            if self.NAMESPACED:
                args: tuple = (DEFAULT_NAMESPACE, self.get(k8s))
            else:
                args = (self.get(k8s),)
            try:
                return getattr(
                    self.api(k8s), f"create{self.namespaced}_{self.API_FUNC}"
                )(*args, async_req=async_req, dry_run=dry_run.value)
            except ApiException as ex:
                if (ex_body := json.loads(ex.body)).get("reason") != "AlreadyExists":
                    raise
                if "object is being deleted" in ex_body.get("message"):
                    raise RetryException(
                        f"{_type} {self.name} is being deleted"
                    ) from ex
                log.exception(
                    "%s %s no created, already exists: %s", _type, self.name, ex_body
                )
            return None

        return _create()

    def delete(
        self,
        k8s: k8s_client.Kubernetes,
        async_req: bool = False,
        dry_run: k8s_client.DryRun = k8s_client.DryRun.OFF,
    ) -> Any | ApplyResult:
        """delete the k8s object"""
        log.info("deleting %s %s", _type := type(self).__name__, self.name)
        if self.NAMESPACED:
            args: tuple = (self.name, DEFAULT_NAMESPACE)
        else:
            args = (self.name,)
        try:
            return getattr(self.api(k8s), f"delete{self.namespaced}_{self.API_FUNC}")(
                *args, async_req=async_req, dry_run=dry_run.value
            )
        except ApiException as ex:
            if json.loads(ex.body).get("reason") != "NotFound":
                raise
            log.info("%s %s do not exists, nothing to delete", _type, self.name)
        return None

    def apply(
        self,
        k8s: k8s_client.Kubernetes,
        async_req: bool = False,
        dry_run: k8s_client.DryRun = k8s_client.DryRun.OFF,
    ) -> Any | ApplyResult:
        """deletes the object if already exists and then creates new version"""
        log.info("applying %s %s", _type := type(self).__name__, self.name)
        try:
            self.read(k8s)
            log.info("%s %s already exists, deleting and recreating", _type, self.name)
            self.delete(k8s, async_req, dry_run)
            if dry_run == k8s_client.DryRun.ON:
                log.warning(
                    "Running apply with dry_run:ON, aborting before creating"
                    "because the previous delete on dry_run did nothing,"
                    "so the create will fail with already exists"
                )
                return self.read(k8s)
            timeout = time.time() + DELETE_TIMEOUT
            while True:
                log.debug("waiting for %s %s to be deleted", _type, self.name)
                self.read(k8s)
                if time.time() > timeout:
                    break
                time.sleep(1)
        except ApiException as ex:
            if json.loads(ex.body).get("reason") != "NotFound":
                raise
            log.info("%s %s do not exists, creating", _type, self.name)
        return self.create(k8s, async_req, dry_run)

    def wait(self, k8s: k8s_client.Kubernetes) -> None:
        """waits until the k8s object exists"""
        if self.NAMESPACED:
            args: tuple = (DEFAULT_NAMESPACE,)
        else:
            args = tuple()
        self._wait(
            k8s=k8s,
            func=getattr(self.api(k8s), f"list{self.namespaced}_{self.API_FUNC}"),
            args=args,
        )

    def _check_condition(
        self,
        current_conditions: List[str],
        condition: k8s_client.WaitCondition,
        event: dict,
    ) -> bool:
        for _condition in event["object"].status.conditions or []:
            current_conditions.append(f"{_condition.type}:{_condition.status}")
            if _condition.type == condition.value:
                return bool(_condition.status)
        return False

    def _explore_object_pods(
        self, k8s: k8s_client.Kubernetes, event: dict
    ) -> Iterator[client.V1Pod]:
        _object = event["object"]
        namespace, selector = None, None
        if hasattr(_object, "metadata"):
            if hasattr(_object.metadata, "namespace"):
                namespace = _object.metadata.namespace
            if hasattr(_object.metadata, "name"):
                name = _object.metadata.name
        if hasattr(_object, "kind"):
            selector = f"{_object.kind.lower()}-name"
        if namespace and selector:
            for pod in k8s.core_api.list_namespaced_pod(
                namespace, label_selector=f"{selector}={name}"
            ).items:
                yield pod

    @retry(
        retry=retry_if_exception_type(HTTPError),
        stop=stop_after_attempt(3),
        wait=wait_fixed(1),
        after=after_log(log, logger.logging.INFO),
    )
    def _wait(  # pylint: disable=too-many-branches # TODO refactor
        self,
        k8s: k8s_client.Kubernetes,
        func: Callable,
        args: tuple,
        condition: Optional[k8s_client.WaitCondition] = None,
        phases: Optional[List[k8s_client.Phase]] = None,
        check_readiness: Optional[bool] = False,
        label_selector: Optional[str] = None,
        check_replicas: Optional[bool] = False,
    ) -> None:
        """Wait until the object satisfies any of the phases, the condition or if None specified, that it exists in the cluster"""
        current_conditions: List[str] = []
        current_phase = None
        _phases = [p.value for p in phases] if phases else []
        field_selector = None if label_selector else f"metadata.name={self.name}"
        msg = f"{type(self).__name__} {self.name} with {condition=} {_phases=} {check_readiness=}"
        log.info("Waiting for %s", msg)
        for event in k8s.watch.stream(
            func,
            *args,
            field_selector=field_selector,
            label_selector=label_selector,
            timeout_seconds=WAIT_TIMEOUT,
            _request_timeout=80,
        ):
            current_conditions.clear()
            if condition and self._check_condition(
                current_conditions, condition, event
            ):
                k8s.watch.stop()
                log.info("Done, condition satisfied for %s ", msg)
                return
            if (
                current_phase := getattr(
                    status := event["object"].status, "phase", None
                )
            ) in _phases:  # pylint: disable=superfluous-parens
                k8s.watch.stop()
                log.info("Done, phase satisfied for %s ", msg)
                return
            if getattr(status, "failed", 0):
                k8s.watch.stop()
                raise RuntimeError(f"{msg} Failed!")
            if check_readiness and (
                container_statuses := getattr(status, "container_statuses", [])
            ):
                for container_status in container_statuses:
                    if container_status.ready:
                        k8s.watch.stop()
                        log.info("Done, %s passed its readiness probe", msg)
                        return
            if check_replicas and isinstance(
                status, (client.V1ReplicaSetStatus, client.V1StatefulSetStatus)
            ):
                if status.replicas == status.available_replicas:
                    k8s.watch.stop()
                    log.info("Done, %s has all replicas available", msg)
                    return
                if status.available_replicas > 1 and status.ready_replicas > 1:
                    k8s.watch.stop()
                    log.info("Done, %s has enough replicas available", msg)
                    return
            if not condition and not _phases and not check_replicas:
                k8s.watch.stop()
                log.info("Done, object exists for %s ", msg)
                return
            for pod in self._explore_object_pods(k8s, event):
                if not pod.status.container_statuses:
                    continue
                for container_status in pod.status.container_statuses:
                    if (
                        waiting_status := container_status.state.waiting
                    ) and waiting_status.reason in [
                        "ErrImagePull",
                        "ImagePullBackOff",
                    ]:
                        k8s.watch.stop()
                        raise K8SPullImageError(
                            f"{msg} Failed! Not possible to retrieve pod image ({waiting_status})"
                        )
            log.info(
                "Still waiting for %s, last event: conditions[%s] phase[%s]",
                msg,
                current_conditions,
                current_phase,
            )

        raise RuntimeError(
            f"{msg} is not available yet, {current_conditions=}, {current_phase=}"
        )


@dataclass  # type: ignore
class Volume(K8sModel):
    """Common methods for Persistent Volumes and PV Claims"""

    storage: str

    @abstractmethod
    def get_current_volume(self, k8s: k8s_client.Kubernetes) -> Optional[Any]:
        """gets the list of existing volumes"""

    @staticmethod
    @abstractmethod
    def get_volume_capacity(
        volume: client.V1PersistentVolume | client.V1PersistentVolumeClaim,
    ) -> str:
        """gets the capacity of the volume"""

    def apply(
        self,
        k8s: k8s_client.Kubernetes,
        async_req: bool = False,
        dry_run: k8s_client.DryRun = k8s_client.DryRun.OFF,
    ) -> Any | ApplyResult:
        """Creates the volume or applies if previous volume had less storage"""
        log.info("applying %s %s", _type := type(self).__name__, self.name)
        if volume := self.get_current_volume(k8s):
            current_storage = self.get_volume_capacity(volume)
            log.info("%s %s already exists", _type, self.name)
            if parse_quantity(current_storage) > parse_quantity(self.storage):
                log.warning(
                    "Not possible to apply: New storage:%s for %s is smaller than existing %s",
                    self.storage,
                    self.name,
                    current_storage,
                )
            else:
                log.info("patching %s %s", _type, self.name)
                return self.patch(k8s, async_req, dry_run)
            return volume
        log.info("creating %s %s", _type, self.name)
        return self.create(k8s, async_req, dry_run)


@dataclass
class PersistentVolume(Volume):
    """Persistent Volume"""

    storage: str
    disk_name: str
    API: ClassVar[str] = "core"
    API_FUNC: ClassVar[str] = "persistent_volume"

    def __post_init__(self) -> None:
        super().__post_init__()
        parse_quantity(self.storage)
        if not self.disk_name:
            raise ValueError("disk_name must be specified")

    def get(self, k8s: k8s_client.Kubernetes) -> client.V1PersistentVolume:
        del k8s
        return k8s_client.Kubernetes.get_persistent_volume(
            self.name, self.storage, self.disk_name
        )

    def get_current_volume(
        self, k8s: k8s_client.Kubernetes
    ) -> Optional[client.V1PersistentVolume]:
        """gets the volume existing in the cluster if any"""
        if items := k8s.core_api.list_persistent_volume(
            field_selector=f"metadata.name={self.name}"
        ).items:
            return items[0]
        return None

    @staticmethod
    def get_volume_capacity(volume: client.V1PersistentVolume) -> str:
        """gets the capacity of the volume"""
        return volume.spec.capacity["storage"]

    def read(self, k8s: k8s_client.Kubernetes) -> client.V1PersistentVolume:
        return k8s.core_api.read_persistent_volume(self.name)

    def patch(
        self,
        k8s: k8s_client.Kubernetes,
        async_req: bool = False,
        dry_run: k8s_client.DryRun = k8s_client.DryRun.OFF,
    ) -> client.V1PersistentVolume | ApplyResult:
        """modifies existing volume"""
        return k8s.core_api.patch_persistent_volume(
            self.name, self.get(k8s), async_req=async_req, dry_run=dry_run.value
        )

    def create(
        self,
        k8s: k8s_client.Kubernetes,
        async_req: bool = False,
        dry_run: k8s_client.DryRun = k8s_client.DryRun.OFF,
    ) -> client.V1PersistentVolume | ApplyResult:
        """modifies existing volume"""
        return k8s.core_api.create_persistent_volume(
            self.get(k8s), async_req=async_req, dry_run=dry_run.value
        )

    def delete(
        self,
        k8s: k8s_client.Kubernetes,
        async_req: bool = False,
        dry_run: k8s_client.DryRun = k8s_client.DryRun.OFF,
    ) -> Any | ApplyResult:
        del k8s, async_req, dry_run
        raise RuntimeError("Not automatized, deleting the PV will remove all the data")
        # return k8s.core_api.delete_persistent_volume(self.name, async_req=async_req)

    def wait(self, k8s: k8s_client.Kubernetes) -> None:
        self._wait(
            k8s=k8s,
            func=k8s.core_api.list_persistent_volume,
            args=tuple(),
            phases=[k8s_client.PhaseVolume.AVAILABLE, k8s_client.PhaseVolume.BOUND],
        )


@dataclass
class PersistentVolumeClaim(Volume):
    """Persistent Volume Claim"""

    storage: str
    API: ClassVar[str] = "core"
    API_FUNC: ClassVar[str] = "persistent_volume_claim"

    def __post_init__(self) -> None:
        super().__post_init__()
        # if the storage is not valid will raise an
        try:
            parse_quantity(self.storage)
        except ValueError as ex:
            raise ValueError(f"Invalid storage:'{self.storage}' {ex}") from ex

    def get(self, k8s: k8s_client.Kubernetes) -> client.V1PersistentVolumeClaim:
        del k8s
        return k8s_client.Kubernetes.get_persistent_volume_claim(
            self.name, self.storage
        )

    def get_current_volume(self, k8s: k8s_client.Kubernetes) -> Optional[Any]:
        """gets the list of existing volumes"""
        if items := k8s.core_api.list_namespaced_persistent_volume_claim(
            DEFAULT_NAMESPACE, field_selector=f"metadata.name={self.name}"
        ).items:
            return items[0]
        return None

    @staticmethod
    def get_volume_capacity(volume: client.V1PersistentVolumeClaim) -> str:
        """gets the capacity of the volume"""
        return volume.spec.resources.requests["storage"]

    def delete(
        self,
        k8s: k8s_client.Kubernetes,
        async_req: bool = False,
        dry_run: k8s_client.DryRun = k8s_client.DryRun.OFF,
    ) -> Any | ApplyResult:
        raise RuntimeError("Not automatized, deleting the PVC will remove all the data")
        # return k8s.core_api.delete_namespaced_persistent_volume_claim(self.name, DEFAULT_NAMESPACE, async_req=async_req)

    def wait(self, k8s: k8s_client.Kubernetes) -> None:
        self._wait(
            k8s=k8s,
            func=k8s.core_api.list_namespaced_persistent_volume_claim,
            args=(DEFAULT_NAMESPACE,),
            phases=[
                k8s_client.PhaseVolume.AVAILABLE,
                k8s_client.PhasePVC.PENDING,
                k8s_client.PhaseVolume.BOUND,
            ],
        )


@dataclass
class PersistentVolumeClaimTemplate:
    """Persistent Volume Claim Template"""

    name: str
    storage: str

    def get_template(
        self, k8s: k8s_client.Kubernetes
    ) -> client.V1PersistentVolumeClaim:
        """get a volume claim template for stateful sets"""
        del k8s
        return k8s_client.Kubernetes.get_persistent_volume_claim_template(
            self.name, self.storage
        )


@dataclass
class VolumeMount:
    """Mount a Volume in a pod folder and manage Persistent Volume Claim"""

    mount_path: str


@dataclass
class VolumeMountPVC(VolumeMount):
    """Mount a Volume in a pod folder and manage Persistent Volume Claim"""

    pvc: PersistentVolumeClaim
    sub_path: Optional[str] = None


@dataclass
class VolumeMountPVCTemplate(VolumeMount):
    """Mount a Volume from a PVC template in a pod folder
    This is a volume use in stateful sets to automatically manage PVC and PV for the replicas
    """

    pvc_template: PersistentVolumeClaimTemplate
    sub_path: Optional[str] = None


@dataclass
class VolumeMountConfigMap(VolumeMount):
    """Mount a Volume config map in a pod folder"""

    config_map: "ConfigMap"
    default_mode: int


@dataclass
class VolumeMountSecret(VolumeMount):
    """Mount a Secret in a pod folder"""

    secret: "Secret"
    default_mode: int = 0o600


@dataclass
class VolumeMountEmptyDir(VolumeMount):
    """Mount an EmptyDir Volume to share data between containers"""

    name: str


@dataclass
class ServicePort:
    """Service Port"""

    name: str
    port: int
    target_port: int

    def get(self, k8s: k8s_client.Kubernetes) -> client.V1ServicePort:
        """get the k8s object to apply"""
        del k8s
        return k8s_client.client.V1ServicePort(
            port=self.port, target_port=self.target_port, name=self.name
        )


@dataclass
class Port:
    """Generic port definition"""

    name: str
    port: int
    target_port: Optional[int] = None


@dataclass
class Service(K8sModel):
    """k8s_client.Kubernetes Service"""

    ports: List[ServicePort]
    selector: dict
    API: ClassVar[str] = "core"
    API_FUNC: ClassVar[str] = "service"

    def get(self, k8s: k8s_client.Kubernetes) -> client.V1Service:
        return k8s_client.Kubernetes.get_service(
            name=self.name,
            ports=[p.get(k8s) for p in self.ports],
            selector=self.selector,
        )

    def wait(self, k8s: k8s_client.Kubernetes) -> None:
        log.info("Waiting for service %s", self.name)
        for event in k8s.watch.stream(
            k8s.core_api.list_namespaced_endpoints,
            DEFAULT_NAMESPACE,
            field_selector=f"metadata.name={self.name}",
            timeout_seconds=WAIT_TIMEOUT,
        ):
            details = []
            for subset in getattr(event["object"], "subsets", []) or []:
                for address in subset.addresses or []:
                    details.append(
                        f"Endpoint({address.ip} --> {address.target_ref.kind} {address.target_ref.name})"
                    )
            if details:
                k8s.watch.stop()
                log.info(
                    "Done, found endpoints for service %s : %s", self.name, details
                )
                return
        raise RuntimeError(f"Service {self.name} is not available")


@dataclass
class HorizontalPodAutoscaler(K8sModel):
    """HorizontalPodAutoscaler"""

    target_kind: str
    target_name: str
    min_replicas: int
    max_replicas: int
    target_cpu_utilization_percentage: int

    API: ClassVar[str] = "hpa"
    API_FUNC: ClassVar[str] = "horizontal_pod_autoscaler"

    def get(self, k8s: k8s_client.Kubernetes) -> client.V1Service:
        del k8s
        return k8s_client.Kubernetes.get_hpa(
            self.name,
            self.target_kind,
            self.target_name,
            self.min_replicas,
            self.max_replicas,
            self.target_cpu_utilization_percentage,
        )


@dataclass
class ConfigMap(K8sModel):
    """Config Map"""

    data: Dict[str, str]
    API: ClassVar[str] = "core"
    API_FUNC: ClassVar[str] = "config_map"

    def get(self, k8s: k8s_client.Kubernetes) -> client.V1ConfigMap:
        """get the k8s object to apply"""
        del k8s
        return k8s_client.Kubernetes.get_configmap(self.name, self.data)


@dataclass
class Secret(K8sModel):
    """Secret"""

    secret_type: k8s_client.SecretType
    string_data: Optional[Dict[str, str]] = None
    data: Optional[Dict[str, str]] = None
    API: ClassVar[str] = "core"
    API_FUNC: ClassVar[str] = "secret"

    def get(self, k8s: k8s_client.Kubernetes) -> client.V1Secret:
        """get the k8s object to apply"""
        del k8s
        return k8s_client.Kubernetes.get_secret(
            self.name, self.secret_type, self.string_data, self.data
        )


class K8sRole(Protocol):
    """Defines the common Protocol for Role and ClusterRoles"""

    name: str
    api_group: str
    resource: str
    verbs: List[k8s_client.APIRequestVerb]

    def apply(
        self,
        k8s: k8s_client.Kubernetes,
        async_req: bool = False,
        dry_run: k8s_client.DryRun = k8s_client.DryRun.OFF,
    ) -> Any | ApplyResult:
        """Apply the Role"""


@dataclass
class Role(K8sModel, K8sRole):
    """Role"""

    api_group: str
    resource: str
    verbs: List[k8s_client.APIRequestVerb]
    resource_names: list[str] = field(default_factory=list)
    API: ClassVar[str] = "rbacauthorization"
    API_FUNC: ClassVar[str] = "role"

    def get(self, k8s: k8s_client.Kubernetes) -> client.V1Job:
        """gets the Job definition"""
        del k8s
        return k8s_client.Kubernetes.get_role(
            self.name, self.api_group, self.resource, self.resource_names, self.verbs
        )


@dataclass
class ClusterRole(K8sModel, K8sRole):
    """Cluster Role"""

    api_group: str
    resource: str
    verbs: List[k8s_client.APIRequestVerb]
    resource_names: list[str] = field(default_factory=list)
    API: ClassVar[str] = "rbacauthorization"
    API_FUNC: ClassVar[str] = "cluster_role"
    NAMESPACED: ClassVar[bool] = False

    def get(self, k8s: k8s_client.Kubernetes) -> client.V1Job:
        """gets the Job definition"""
        del k8s
        return k8s_client.Kubernetes.get_cluster_role(
            self.name, self.api_group, self.resource, self.resource_names, self.verbs
        )


@dataclass
class RoleBinding(K8sModel):
    """Role Binding"""

    role_name: str
    service_account_name: Optional[str] = None
    users: list[str] = field(default_factory=list)
    resource_names: list[str] = field(default_factory=list)
    API: ClassVar[str] = "rbacauthorization"
    API_FUNC: ClassVar[str] = "role_binding"

    def get(self, k8s: k8s_client.Kubernetes) -> client.V1Job:
        """gets the Job definition"""
        del k8s
        return k8s_client.Kubernetes.get_role_binding(
            self.name,
            DEFAULT_NAMESPACE,
            self.service_account_name,
            self.users,
            self.role_name,
        )


@dataclass
class ClusterRoleBinding(K8sModel):
    """Cluster Role Binding"""

    role_name: str
    service_account_name: Optional[str] = None
    users: list[str] = field(default_factory=list)
    API: ClassVar[str] = "rbacauthorization"
    API_FUNC: ClassVar[str] = "cluster_role_binding"
    NAMESPACED: ClassVar[bool] = False

    def get(self, k8s: k8s_client.Kubernetes) -> client.V1Job:
        """gets the Job definition"""
        del k8s
        return k8s_client.Kubernetes.get_cluster_role_binding(
            self.name,
            DEFAULT_NAMESPACE,
            self.service_account_name,
            self.users,
            self.role_name,
        )


@dataclass
class ServiceAccount(K8sModel):
    """Service Account"""

    roles: list[K8sRole]
    annotations: Optional[dict[str, str]] = None
    API: ClassVar[str] = "core"
    API_FUNC: ClassVar[str] = "service_account"

    def get(self, k8s: k8s_client.Kubernetes) -> client.V1Deployment:
        """gets the Job definition"""
        return k8s_client.Kubernetes.get_service_account(self.name, self.annotations)

    def get_role_binding(self, role: K8sRole) -> RoleBinding | ClusterRoleBinding:
        """Gets the service related to this Deployment"""
        if len(name := f"sa-{self.name}-role-{role.name}") > 63:
            if len(name := f"{self.name}-{role.name}") > 63:
                raise ValueError(f"role binding name {name} is too long")
        if isinstance(role, ClusterRole):
            return ClusterRoleBinding(
                name=name, service_account_name=self.name, role_name=role.name
            )
        return RoleBinding(
            name=name, service_account_name=self.name, role_name=role.name
        )

    def apply(
        self,
        k8s: k8s_client.Kubernetes,
        async_req: bool = False,
        dry_run: k8s_client.DryRun = k8s_client.DryRun.OFF,
    ) -> client.V1ServiceAccount | ApplyResult:
        service_account = super().apply(k8s, async_req, dry_run)
        for role in self.roles:
            role.apply(k8s, async_req, dry_run)
            self.get_role_binding(role).apply(k8s, async_req, dry_run)
        return service_account


@dataclass
class WorkloadIdentity(ServiceAccount):
    """Service Account created on terraform using Workload Identity"""

    roles: List[K8sRole] = field(default_factory=list)
    MSG: ClassVar[str] = "Workload Identity is managed by pcaas_infra"

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.roles:
            raise ValueError("WorkloadIdentity roles are specified in pcaas_infra")

    def apply(
        self,
        k8s: k8s_client.Kubernetes,
        async_req: bool = False,
        dry_run: k8s_client.DryRun = k8s_client.DryRun.OFF,
    ) -> client.V1ServiceAccount | ApplyResult:
        del async_req
        try:
            log.info(
                "%s, the service account %s MUST EXISTS already in the cluster",
                self.MSG,
                self.name,
            )
            return self.read(k8s)
        except ApiException as ex:
            if json.loads(ex.body).get("reason") == "NotFound":
                if dry_run == k8s_client.DryRun.ON:
                    log.error(
                        "Running apply with dry_run:ON, ignoring Workload Identity Not Found "
                        "because this service account is created on terraform, "
                        "so it will not exists until we deploy in the cluster"
                    )
                    return self.get(k8s)
                raise RuntimeError(
                    f"workload identity service account {self.name} does not exists, check terraform"
                ) from ex
            raise

    def patch(
        self,
        k8s: k8s_client.Kubernetes,
        async_req: bool = False,
        dry_run: k8s_client.DryRun = k8s_client.DryRun.OFF,
    ) -> Any | ApplyResult:
        raise RuntimeError(f"{self.MSG}, patch is forbidden")

    def create(
        self,
        k8s: k8s_client.Kubernetes,
        async_req: bool = False,
        dry_run: k8s_client.DryRun = k8s_client.DryRun.OFF,
    ) -> Any | ApplyResult:
        raise RuntimeError(f"{self.MSG}, create is forbidden")

    def delete(
        self,
        k8s: k8s_client.Kubernetes,
        async_req: bool = False,
        dry_run: k8s_client.DryRun = k8s_client.DryRun.OFF,
    ) -> Any | ApplyResult:
        raise RuntimeError(f"{self.MSG}, delete is forbidden")


@dataclass
class ValueFromField:
    """ValueFromField"""

    field_path: str

    def get(self) -> client.V1EnvFromSource:
        """gets the value"""
        return client.V1EnvVarSource(
            field_ref=client.V1ObjectFieldSelector(field_path=self.field_path)
        )


@dataclass
class ValueFromResourceField:
    """ValueFromResourceField"""

    container_name: str
    divisor: str
    resource: str

    def get(self) -> client.V1EnvFromSource:
        """gets the value"""
        return client.V1EnvVarSource(
            resource_field_ref=client.V1ResourceFieldSelector(
                container_name=self.container_name,
                divisor=self.divisor,
                resource=self.resource,
            )
        )


@dataclass
class Container:
    """Container definition"""

    name: str
    image: str
    command: Optional[List[str]] = None
    args: Optional[List[str]] = None
    image_pull_policy: str = "Always"
    ports: Optional[list[Port]] = None
    env: Optional[Dict[str, str | ValueFromField | ValueFromResourceField]] = None
    volumes: Optional[List[VolumeMount]] = None
    liveness_pre_stop_command: Optional[List[str]] = None
    liveness_post_start_command: Optional[List[str]] = None
    readiness_command: Optional[List[str]] = None
    liveness_command: Optional[List[str]] = None
    resources: Optional[PodResources] = None
    extra_env_sources: list[str] = field(default_factory=list)
    security_context_uid: Optional[int] = None

    @property
    def ports_dict(self) -> dict[str, int]:
        """ports to dict"""
        if not self.ports:
            return {}
        return {port.name: port.port for port in self.ports}

    def _volumes(self) -> Iterator[tuple[str, VolumeMount]]:
        """Assign an unique name to the volumes and iterates over them"""
        for i, volume in enumerate(self.volumes or []):
            name = f"{self.name}-data-{i}"
            if isinstance(volume, VolumeMountEmptyDir):
                name = volume.name
            if isinstance(volume, VolumeMountPVCTemplate):
                # for PVC Templates in statefulsets
                name = volume.pvc_template.name
                # TODO refactor so all the volume mounts can share volume claims between containers
            yield name, volume

    def get_volume_claims(self) -> Iterator[client.V1PersistentVolumeClaim]:
        """gets the volume claims"""
        for name, volume in self._volumes():
            if isinstance(volume, VolumeMountPVC):
                yield k8s_client.Kubernetes.get_pod_volume_claim(name, volume.pvc.name)
            elif isinstance(volume, VolumeMountPVCTemplate):
                yield k8s_client.Kubernetes.get_pod_volume_claim(
                    volume.pvc_template.name, volume.pvc_template.name
                )
            elif isinstance(volume, VolumeMountConfigMap):
                keys = list(volume.config_map.data.keys())
                yield k8s_client.Kubernetes.get_pod_volume_from_configmap(
                    name, volume.config_map.name, keys, volume.default_mode
                )
            elif isinstance(volume, VolumeMountSecret):
                if volume.secret.data:
                    keys = list(volume.secret.data.keys())
                elif volume.secret.string_data:
                    keys = list(volume.secret.string_data.keys())
                else:
                    raise ValueError(
                        f"Secret {volume.secret.name} has no data to mount"
                    )
                yield k8s_client.Kubernetes.get_pod_volume_from_secret(
                    name, volume.secret.name, keys, volume.default_mode
                )
            elif isinstance(volume, VolumeMountEmptyDir):
                yield k8s_client.Kubernetes.get_empty_dir_volume(name)
            else:
                raise ValueError(f"Not supported volume instace {volume}")

    def get_volume_mounts(self) -> Iterator[client.V1VolumeMount]:
        """gets the volume mounts"""
        for name, volume in self._volumes():
            if isinstance(volume, VolumeMountPVC):
                yield k8s_client.Kubernetes.get_container_volume_mount(
                    name, volume.mount_path, volume.sub_path
                )
            elif isinstance(volume, VolumeMountPVCTemplate):
                yield k8s_client.Kubernetes.get_container_volume_mount(
                    volume.pvc_template.name, volume.mount_path, volume.sub_path
                )
            elif isinstance(
                volume, (VolumeMountConfigMap, VolumeMountSecret, VolumeMountEmptyDir)
            ):
                yield k8s_client.Kubernetes.get_container_volume_mount(
                    name, volume.mount_path, None
                )
            else:
                raise ValueError(f"Not supported volume instace {volume}")

    def get_container_spec(self, k8s: k8s_client.Kubernetes) -> client.V1Container:
        """gets the Pod template spec definition"""
        env = k8s.get_env_from_source(
            [DEFAULT_CONFIGMAP_NAME, DEFAULT_SECRET_NAME] + self.extra_env_sources
        )
        if self.env:
            _env = self.env.copy()
            for key, value in self.env.items():
                if isinstance(value, (ValueFromField, ValueFromResourceField)):
                    _env[key] = value.get()
            env = k8s.upsert_envvars(env, k8s.get_env_from_dict(_env))
        if self.resources is None:
            self.resources = PodResources()
        return k8s_client.Kubernetes.get_container_spec(
            image=self.image,
            name=self.name,
            image_pull_policy=self.image_pull_policy,
            command=self.command,
            args=self.args,
            resources_request=self.resources.get_k8s_request(),
            env=env if env else None,
            ports=self.ports_dict,
            volume_mounts=list(self.get_volume_mounts()) if self.volumes else None,
            readiness_command=self.readiness_command,
            liveness_command=self.liveness_command,
            liveness_pre_stop_command=self.liveness_pre_stop_command,
            liveness_post_start_command=self.liveness_post_start_command,
            security_context_uid=self.security_context_uid,
        )


@dataclass  # type: ignore
class Pod(K8sModel):
    "k8s_client.Kubernetes common pod definition"

    containers: list[Container] = field(default_factory=list)
    init_containers: list[Container] = field(default_factory=list)
    service_account: Optional[ServiceAccount] = None
    port: Optional[int] = None
    restart_policy: k8s_client.RestartPolicy = k8s_client.PodRestartPolicy.NEVER
    security_context_uid: Optional[int] = None
    labels: Dict[str, str] = field(default_factory=dict)
    cloud_sql_proxy: bool = False
    pull_secret: str = PULL_SECRET_NAME
    termination_grace_period_seconds: Optional[int] = None
    API: ClassVar[str] = "core"
    API_FUNC: ClassVar[str] = "job"

    def __post_init__(self) -> None:
        if isinstance(self, Job):
            if not isinstance(self.restart_policy, k8s_client.JobRestartPolicy):
                raise ValueError(
                    "Use specific deployment restart policy class JobRestartPolicy"
                )
        elif not isinstance(self.restart_policy, k8s_client.PodRestartPolicy):
            raise ValueError(
                "Use specific deployment restart policy class PodRestartPolicy"
            )

    @property
    def container_map(self) -> Dict[str, Container]:
        """returns a dict of containers by name"""
        return {container.name: container for container in self.containers}

    def get_pod_spec(
        self, k8s: k8s_client.Kubernetes
    ) -> client.V1PodTemplateSpec:  # pylint: disable=too-many-branches
        """gets the Pod template spec definition"""
        containers = []
        init_containers = []
        _volume_claims: Dict[str, client.V1Volume] = {}
        env: Optional[client.V1EnvVar] = None

        if self.service_account:
            env = k8s.get_env_pair("POD_SERVICE_ACCOUNT", self.service_account.name)

        def get_container_spec_and_update_volumes(
            container: Container,
        ) -> client.V1Container:
            container_spec = container.get_container_spec(k8s)
            if env:
                if container_spec.env:
                    container_spec.env.append(env)
                else:
                    container_spec.env = [env]

            for volume_claim in container.get_volume_claims():
                if volume_claim.name in _volume_claims:
                    if _volume_claims[volume_claim.name] != volume_claim:
                        raise ValueError(
                            f"Volume claim {volume_claim} is already defined with a different configuration {_volume_claims[volume_claim.name]}"
                        )
                    continue
                _volume_claims[volume_claim.name] = volume_claim
            return container_spec

        for container in self.containers:
            containers.append(get_container_spec_and_update_volumes(container))
        for container in self.init_containers:
            init_containers.append(get_container_spec_and_update_volumes(container))

        if self.cloud_sql_proxy:
            containers.append(k8s_client.Kubernetes.get_sql_proxy_container_spec())

        service_account_name = (
            self.service_account.name if self.service_account else None
        )
        self.labels["component"] = self.labels.get("component", self.name)
        return k8s_client.Kubernetes.get_pod_template_spec(
            pod_name=self.name,
            containers=containers,
            init_containers=init_containers,
            labels=self.labels,
            pull_secret=self.pull_secret,
            service_account_name=service_account_name,
            restart_policy=self.restart_policy,
            volumes=list(_volume_claims.values()) if _volume_claims else None,
            security_context_uid=self.security_context_uid,
            termination_grace_period_seconds=self.termination_grace_period_seconds,
        )

    def get_label_selector(self, k8s: k8s_client.Kubernetes) -> str:
        """Concatenate deployment pod labes to use in selector field"""
        labels = [f"{k}={v}" for k, v in self.get_pod_spec(k8s).metadata.labels.items()]
        return ",".join(labels)

    def delete_all_pods(
        self,
        k8s: k8s_client.Kubernetes,
        async_req: bool = False,
        dry_run: k8s_client.DryRun = k8s_client.DryRun.OFF,
    ) -> None:
        """delete all pods that match label_selector"""
        log.info(
            "deleting all pods matching %s",
            label_selector := self.get_label_selector(k8s),
        )
        for pod in k8s.core_api.list_namespaced_pod(
            DEFAULT_NAMESPACE, label_selector=label_selector
        ).items:
            log.info(
                "deleting pod %s from labled_Selector %s",
                name := pod.metadata.name,
                label_selector,
            )
            k8s.core_api.delete_namespaced_pod(
                name, DEFAULT_NAMESPACE, async_req=async_req, dry_run=dry_run.value
            )

    def wait_pod_deletion(
        self, k8s: k8s_client.Kubernetes, dry_run: k8s_client.DryRun
    ) -> None:
        """Wait for all the pods to be deleted"""
        log.info(
            "Waiting for the deletation of all the pods from %s %s, with labels %s",
            _type := type(self).__name__,
            self.name,
            label_selector := self.get_label_selector(k8s),
        )
        timeout = time.time() + WAIT_TIMEOUT
        while pods := k8s.core_api.list_namespaced_pod(
            DEFAULT_NAMESPACE, label_selector=label_selector
        ).items:
            if dry_run == k8s_client.DryRun.ON:
                log.warning(
                    "Running apply with dry_run:ON, aborting wait for %s deletion "
                    "because the previous delete on dry_run did nothing, "
                    "so this will loop until timeout and return an error",
                    _type,
                )
                break
            log.info(
                "%s %s deleted, but still %s pods need to be terminated: %s",
                _type,
                self.name,
                len(pods),
                [pod.metadata.name for pod in pods],
            )
            if time.time() > timeout:
                break
            time.sleep(1)

    def wait(self, k8s: k8s_client.Kubernetes) -> None:
        self._wait(
            k8s=k8s,
            func=k8s.core_api.list_namespaced_pod,
            args=(DEFAULT_NAMESPACE,),
            label_selector=self.get_label_selector(k8s),
            condition=k8s_client.WaitConditionPod.READY,
        )


@dataclass
class Job(Pod):
    """k8s_client.Kubernetes Job"""

    restart_policy: k8s_client.RestartPolicy = k8s_client.JobRestartPolicy.NEVER
    cleanup_after_seconds: Optional[int] = None
    backoff_limit: int = BACKOFF_LIMIT_JOB
    API: ClassVar[str] = "batch"
    API_FUNC: ClassVar[str] = "job"

    def get(self, k8s: k8s_client.Kubernetes) -> client.V1Job:
        """gets the Job definition"""
        return k8s_client.Kubernetes.get_job(
            self.name,
            self.get_pod_spec(k8s),
            self.labels,
            self.backoff_limit,
            self.cleanup_after_seconds,
        )

    def wait(self, k8s: k8s_client.Kubernetes) -> None:
        self._wait(
            k8s=k8s,
            func=k8s.batch_api.list_namespaced_job,
            args=(DEFAULT_NAMESPACE,),
            condition=k8s_client.WaitConditionJob.COMPLETE,
        )


@dataclass(kw_only=True)
class Cronjob(Pod):
    "Kuberentes cronjob definition"
    schedule: k8s_client.CronTab
    API_FUNC: ClassVar[str] = "cron_job"

    def api(
        self, k8s: k8s_client.Kubernetes
    ) -> client.CoreV1Api | client.BatchV1Api | client.AppsV1Api:
        return k8s.batch_api

    def get(self, k8s: k8s_client.Kubernetes) -> client.V1CronJob:
        """gets the CronJob definition"""
        if not self.schedule:
            raise ValueError("Schedule must be specified")
        return k8s_client.Kubernetes.get_cronjob(
            self.name, self.get_pod_spec(k8s), self.schedule
        )


@dataclass
class HPA:
    """HPA specification for ReplicaManager"""

    min_replicas: int
    max_replicas: int
    target_cpu_utilization_percentage: int

    def get_hpa(self, name: str, target_kind: str) -> HorizontalPodAutoscaler:
        """Creates the K8s HPA spec"""
        return HorizontalPodAutoscaler(
            name,
            target_kind,
            name,
            self.min_replicas,
            self.max_replicas,
            self.target_cpu_utilization_percentage,
        )


@dataclass
class VerticalPodAutoscaler:
    """VerticalPodAutoscaler"""

    name: str
    target_kind: str
    target_name: str
    container_name: Optional[str]
    min_allowed: Optional[PodResources]
    max_allowed: Optional[PodResources]
    control_cpu: bool
    control_memory: bool

    def __post_init__(self) -> None:
        # SO FAR WE ONLY WORK WITH GENERAL_PURPOSE COMPUTE CLASS
        if not self.min_allowed:
            log.warning(
                f"min_allowed not specified, using default values of {ComputeClasses.GENERAL_PURPOSE=}"
            )
            self.min_allowed = PodResources(
                cpu=ComputeClasses.GENERAL_PURPOSE.min_cpu,
                memory=ComputeClasses.GENERAL_PURPOSE.min_memory,
            )
        if not self.max_allowed:
            log.warning(
                f"max_allowed not specified, using default values of {ComputeClasses.GENERAL_PURPOSE=}"
            )
            self.max_allowed = PodResources(
                cpu=ComputeClasses.GENERAL_PURPOSE.max_cpu,
                memory=ComputeClasses.GENERAL_PURPOSE.max_memory,
            )

    def get(self) -> dict:
        """Creates the K8s VPA spec"""
        return k8s_client.Kubernetes.get_vpa(
            self.name,
            self.target_kind,
            self.target_name,
            self.container_name or "*",
            self.min_allowed,
            self.max_allowed,
            self.control_cpu,
            self.control_memory,
        )

    def apply(
        self,
        k8s: k8s_client.Kubernetes,
        async_req: bool = False,
        dry_run: k8s_client.DryRun = k8s_client.DryRun.OFF,
    ) -> ApplyResult:
        """Applied the VPA"""

        group = "autoscaling.k8s.io"
        version = "v1"
        plural = "verticalpodautoscalers"

        vpa = self.get()
        vpa["apiVersion"] = f"{group}/{version}"
        try:
            return k8s.custom_api.patch_namespaced_custom_object(
                group,
                version,
                DEFAULT_NAMESPACE,
                plural,
                self.name,
                vpa,
                async_req=async_req,
                dry_run=dry_run.value,
            )
        except ApiException as ex:
            if json.loads(ex.body).get("reason") == "NotFound":
                return k8s.custom_api.create_namespaced_custom_object(
                    group,
                    version,
                    DEFAULT_NAMESPACE,
                    plural,
                    vpa,
                    async_req=async_req,
                    dry_run=dry_run.value,
                )
            raise


@dataclass
class VPA:
    """VPA specification for ReplicaManager"""

    # TODO, if more objects use custom_api, create a subclass from K8sModel and automatize it

    min_allowed: Optional[PodResources] = None
    max_allowed: Optional[PodResources] = None
    control_cpu: bool = True
    control_memory: bool = True

    def get_vpa(self, name: str, target_kind: str) -> VerticalPodAutoscaler:
        """Creates the K8s VPA spec for the deployment"""
        return VerticalPodAutoscaler(
            name,
            target_kind,
            name,
            None,
            self.min_allowed,
            self.max_allowed,
            self.control_cpu,
            self.control_memory,
        )


@dataclass
class ReplicaManager(Pod, ABC):
    """Common definition for ReplicaSet and StatefulSet"""

    restart_policy: k8s_client.RestartPolicy = k8s_client.PodRestartPolicy.ALWAYS
    replicas: int = 1
    create_service: bool = False
    hpa: Optional[HPA] = None
    vpa: Optional[VPA] = None
    API: ClassVar[str] = "apps"
    KIND: ClassVar[str] = ""

    def __post_init__(self) -> None:
        super().__post_init__()
        if len(self.name) > 15:
            # deployment name can be longer
            # but we use same name for the service ports name, that is limited to 15
            raise ValueError(f"Deployment name:'{self.name}' too long (max 15 chars)")
        if not self.KIND:
            raise ValueError("KIND must be specified")

    def delete(
        self,
        k8s: k8s_client.Kubernetes,
        async_req: bool = False,
        dry_run: k8s_client.DryRun = k8s_client.DryRun.OFF,
    ) -> Any | ApplyResult:
        result = super().delete(k8s, async_req, dry_run)
        self.wait_pod_deletion(k8s, dry_run)
        if self.create_service:
            self.get_service(k8s).delete(k8s, async_req, dry_run)
        return result

    @property
    def ports(self) -> List[ServicePort]:
        """gets the ports used by the containers"""
        return [
            ServicePort(p.name, p.port, p.target_port or p.port)
            for c in self.containers
            if c.ports
            for p in c.ports
        ]

    def get_service(self, k8s: k8s_client.Kubernetes) -> Service:
        """Gets the service related to this Deployment"""
        if not self.ports:
            raise AttributeError(
                "To create a service is necessary to specify ports in at least one container"
            )
        return Service(self.name, self.ports, self.get_pod_spec(k8s).metadata.labels)

    # TODO: patch will not refresh configmap, check if workaround
    # def apply(
    #     self, k8s: k8s_client.Kubernetes, async_req: bool = False, dry_run: k8s_client.DryRun = k8s_client.DryRun.OFF
    # ) -> client.V1Deployment | client.V1StatefulSet | ApplyResult:
    #     try:
    #         self.read(k8s)
    #         log.debug(f"{self.__class__.__name__} {self.name} already exists, patching it")
    #         replica_manager = super().patch(k8s, async_req, dry_run)
    #     except ApiException as ex:
    #         if (reason := json.loads(ex.body).get("reason")) == "NotFound":
    #             replica_manager = super().create(k8s, async_req, dry_run)
    #         elif reason == "Invalid":
    #             replica_manager = super().apply(k8s, async_req, dry_run)
    #         else:
    #             raise
    #     if self.create_service:
    #         self.get_service(k8s).apply(k8s, async_req, dry_run)
    #     if self.hpa:
    #         self.hpa.get_hpa(self.name, self.KIND).apply(k8s, async_req, dry_run)
    #     if self.vpa:
    #         self.vpa.get_vpa(self.name, self.KIND).apply(k8s, async_req, dry_run)
    #     return replica_manager

    def apply(
        self,
        k8s: k8s_client.Kubernetes,
        async_req: bool = False,
        dry_run: k8s_client.DryRun = k8s_client.DryRun.OFF,
    ) -> client.V1Deployment | ApplyResult:
        replica_manager = super().apply(k8s, async_req, dry_run)
        if self.create_service:
            self.get_service(k8s).apply(k8s, async_req, dry_run)
        if self.hpa:
            self.hpa.get_hpa(self.name, self.KIND).apply(k8s, async_req, dry_run)
        if self.vpa:
            self.vpa.get_vpa(self.name, self.KIND).apply(k8s, async_req, dry_run)
        return replica_manager

    @staticmethod
    @abstractmethod
    def get_list_method(k8s: k8s_client.Kubernetes) -> Callable:
        """returns the method to list the deployment or statefulset"""

    def wait(self, k8s: k8s_client.Kubernetes) -> None:
        log.info(f"Waiting for {self.__class__.__name__} {self.name} pods running")
        self._wait(
            k8s=k8s,
            func=k8s.core_api.list_namespaced_pod,
            args=(DEFAULT_NAMESPACE,),
            check_readiness=True,
            label_selector=self.get_label_selector(k8s),
        )
        if self.create_service:
            log.info(
                f"Waiting for service associated to the {self.__class__.__name__} {self.name}"
            )
            self.get_service(k8s).wait(k8s)
        log.info(f"Waiting for {self.__class__.__name__} {self.name} Available")
        self.wait_for_replica_manager(k8s)

    @abstractmethod
    def wait_for_replica_manager(self, k8s: k8s_client.Kubernetes) -> None:
        """Waits for the replica manager to be available"""


@dataclass
class Deployment(ReplicaManager):
    """Simplification of K8s deployment"""

    API_FUNC: ClassVar[str] = "deployment"
    KIND: ClassVar[str] = "Deployment"

    def get(self, k8s: k8s_client.Kubernetes) -> client.V1Deployment:
        """gets the Job definition"""
        return k8s_client.Kubernetes.get_deployment(
            self.name, self.get_pod_spec(k8s), self.replicas
        )

    @staticmethod
    def get_list_method(k8s: k8s_client.Kubernetes) -> Callable:
        return k8s.apps_api.list_namespaced_deployment

    def wait_for_replica_manager(self, k8s: k8s_client.Kubernetes) -> None:
        """Waits for the replica manager to be available"""
        self._wait(
            k8s=k8s,
            func=self.get_list_method(k8s),
            args=(DEFAULT_NAMESPACE,),
            condition=k8s_client.WaitConditionDeployment.AVAILABLE,
        )


@dataclass
class StatefulSet(ReplicaManager):
    """Simplification of K8s sateteful set"""

    replicas: int = 2
    # pvcs: Optional[List[PersistentVolumeClaimTemplate]] = None
    API_FUNC: ClassVar[str] = "stateful_set"
    KIND: ClassVar[str] = "StatefulSet"

    def get(self, k8s: k8s_client.Kubernetes) -> client.V1StatefulSet:
        """gets the Job definition"""
        pvc_templates = []
        for container in self.containers:
            for volume in container.volumes or []:
                if isinstance(volume, VolumeMountPVCTemplate):
                    pvc_templates.append(volume.pvc_template.get_template(k8s))
        return k8s_client.Kubernetes.get_statefulset(
            self.name, self.get_pod_spec(k8s), self.replicas, pvc_templates
        )

    @staticmethod
    def get_list_method(k8s: k8s_client.Kubernetes) -> Callable:
        return k8s.apps_api.list_namespaced_stateful_set

    def wait_for_replica_manager(self, k8s: k8s_client.Kubernetes) -> None:
        """Waits for the replica manager to be available"""
        self._wait(
            k8s=k8s,
            func=self.get_list_method(k8s),
            args=(DEFAULT_NAMESPACE,),
            check_replicas=True,
        )
