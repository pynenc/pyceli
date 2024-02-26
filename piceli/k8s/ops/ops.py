# pylint: disable=too-many-lines
import json
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from multiprocessing.pool import ApplyResult
from typing import Any, Callable, ClassVar, Iterator, List, Optional

import logger
from k8s_client import k8s_client
from k8s_client.constants import DEFAULT_NAMESPACE, DELETE_TIMEOUT, WAIT_TIMEOUT
from k8s_client.k8s_client import client
from kubernetes.client.exceptions import ApiException
from src.errors import K8SPullImageError
from tenacity import (
    after_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_fixed,
)
from urllib3.exceptions import HTTPError

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
