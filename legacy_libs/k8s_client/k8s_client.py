# pylint: disable=too-many-lines
from collections import UserDict
from enum import Enum
import re
import os
import tempfile
import json
import base64
from typing import Dict, List, Optional, Iterator

from cron_validator import CronValidator
from kubernetes import client, config, watch

from k8s_client import constants as const
from k8s_client.k8s_resources import ClusterResources, PodResources
from settings.settings_model import SETTINGS
import logger

log = logger.get_logger(__name__)


class SecretType(Enum):
    """Type of Secrets"""

    GENERIC = "generic"
    DOCKER_JSON = "kubernetes.io/dockerconfigjson"
    DOCKER_REGISTRY = "docker-registry"
    OPAQUE = "Opaque"


class WaitCondition(Enum):
    """Wait conditions for wait_for method"""


class WaitConditionPod(WaitCondition):
    """Wait conditions specific for pods"""

    POD_SCHEDULED = "PodScheduled"
    # the Pod has been scheduled to a node.

    POD_HAS_NETWORK = "PodHasNetwork"
    # (alpha feature; must be enabled explicitly) the Pod sandbox has been successfully created and networking configured.

    CONTAINERS_READY = "ContainersReady"
    # all containers in the Pod are ready.

    INITIALIZED = "Initialized"
    # all init containers have completed successfully.

    READY = "Ready"
    # the Pod is able to serve requests and should be added to the load balancing pools of all matching Services.


class WaitConditionJob(WaitCondition):
    """Wait conditions specific for jobs"""

    COMPLETE = "Complete"
    FAILED = "Failed"
    SUSPENDED = "Suspended"


class WaitConditionDeployment(WaitCondition):
    """Wait conditions specific for deployments"""

    AVAILABLE = "Available"
    # means that your Deployment has minimum availability

    PROGRESSING = "Progressing"
    # Kubernetes marks a Deployment as progressing when one of the following tasks is performed:
    # The Deployment creates a new ReplicaSet.
    # The Deployment is scaling up its newest ReplicaSet.
    # The Deployment is scaling down its older ReplicaSet(s).
    # New Pods become ready or available (ready for at least MinReadySeconds).


class RestartPolicy(Enum):
    """Restart policy"""


class PodRestartPolicy(RestartPolicy):
    """Restart policy"""

    NEVER = "Never"
    ALWAYS = "Always"
    ON_FAILURE = "OnFailure"


class JobRestartPolicy(RestartPolicy):
    """Pod restart policy"""

    NEVER = "Never"
    # From k8s error: "spec.template.spec.restartPolicy: Required value: valid values: "OnFailure", "Never""
    # Job does not support Retry Policy Always
    # https://kubernetes.io/docs/concepts/workloads/controllers/job/#single-job-starts-controller-pod
    ON_FAILURE = "OnFailure"


class ConcurrencyPolicy(Enum):
    """Specifies how to treat concurrent executions of a Job"""

    ALLOW = "Allow"
    # (default): The cron job allows concurrently running jobs

    FORBID = "Forbid"
    # Forbid: The cron job does not allow concurrent runs;
    # if it is time for a new job run and the previous job run hasn't finished yet,
    # the cron job skips the new job run

    REPLACE = "Replace"
    # Replace: If it is time for a new job run and the previous job run hasn't finished yet,
    # the cron job replaces the currently running job run with a new job run


class DeploymentStrategyType(Enum):
    """Strategy of the deployment"""

    RECREATE = "Recreate"
    # Recreate: All existing Pods are killed before new ones are created when

    ROLLING_UPDATE = "RollingUpdate"
    # RollingUpdate: The Deployment updates Pods in a rolling update fashion
    # You can specify maxUnavailable and maxSurge to control the rolling update process.
    # https://kubernetes.io/docs/concepts/workloads/controllers/deployment/
    # so far only one replica, if we have more we need to check this


class Phase(Enum):
    """Phases of k8s objects"""


class PhaseVolume(Phase):
    """Phases of Persistent Volumes and Persistent Volume Claims"""

    # a free resource that is not yet bound to a claim
    AVAILABLE = "Available"

    # the volume is bound to a claim
    BOUND = "Bound"

    # the claim has been deleted, but the resource is not yet reclaimed by the cluster
    RELEASED = "Released"

    # the volume has failed its automatic reclamation
    FAILED = "Failed"


class PhasePVC(Phase):
    """Specific class for Persistent Volume Claims"""

    # this doesn't appear in the kubernetes docs phases for volumes
    PENDING = "Pending"


class PhasePod(Phase):
    """Phases of Pods"""

    # The Pod has been accepted by the Kubernetes cluster,
    # but one or more of the containers has not been set up and made ready to run.
    # This includes time a Pod spends waiting to be scheduled
    # as well as the time spent downloading container images over the network.
    PENDING = "Pending"

    # The Pod has been bound to a node, and all of the containers have been created.
    # At least one container is still running, or is in the process of starting or restarting.
    RUNNING = "Running"

    # All containers in the Pod have terminated in success, and will not be restarted.
    SUCCEEDED = "Succeeded"

    # All containers in the Pod have terminated, and at least one container has terminated in failure.
    # That is, the container either exited with non-zero status or was terminated by the system.
    FAILED = "Failed"

    # For some reason the state of the Pod could not be obtained.
    # This phase typically occurs due to an error in communicating with the node where the Pod should be running.
    UNKNOWN = "Unknown"


class APIRequestVerb(Enum):
    """Accepted request verbs for kubernetes api permissions"""

    GET = "get"
    LIST = "list"
    CREATE = "create"
    UPDATE = "update"
    PATCH = "patch"
    WATCH = "watch"
    DELETE = "delete"
    DELETE_COLLECTION = "deletecollection"

    @classmethod
    def get_read_only(cls) -> List["APIRequestVerb"]:
        """get only the read APIRequestVerb"""
        return [cls.GET, cls.LIST, cls.WATCH]

    @classmethod
    def get_all_exc_delete_collection(cls) -> List["APIRequestVerb"]:
        """get all the APIRequestVerb"""
        return [cls.GET, cls.LIST, cls.CREATE, cls.UPDATE, cls.PATCH, cls.WATCH, cls.DELETE]


class DryRun(Enum):
    """dry_run accepted options"""

    OFF: Optional[str] = None
    ON: Optional[str] = "All"


class CronTab(str):
    """Validates crontab and facilitates son helpers to create them"""

    # https://cloud.google.com/scheduler/docs/configuring/cron-job-schedules

    def __new__(cls, ct_expr: str) -> "CronTab":
        """validates that the string is a valid crontab expression"""
        CronValidator.parse(ct_expr)
        return str.__new__(cls, ct_expr)

    @classmethod
    def every_x_minutes(cls, minutes: int) -> "CronTab":
        """Runs every x minutes"""
        return cls(f"*/{minutes} * * * *")

    @classmethod
    def every_x_hours(cls, hours: int) -> "CronTab":
        "Runs every x hours (at minute 0)"
        return cls(f"0 */{hours} * * *")

    @classmethod
    def every_x_days(cls, days: int) -> "CronTab":
        "Runs every x days (at minute 0)"
        return cls(f"0 0 */{days} * *")

    @classmethod
    def daily_at_x(cls, hour: int, minute: int) -> "CronTab":
        """Runs every day at hour:minutes"""
        return cls(f"{minute} {hour} * * *")

    @classmethod
    def hourly_at_minutes_x(cls, minutes: list[int]) -> "CronTab":
        """Runs every hour at the minutes specified
        ie. minutes:[1,31,51] -> it will run every hour at minutes 1, 31 and 51
        """
        return cls(",".join(str(min) for min in minutes) + " * * * *")


class Labels(UserDict):
    """Adding some extra validations to k8s labels"""

    CLUSTER_LABELS: set[str] = {"team", "component", "state"}

    @staticmethod
    def validate_cluster_label(key: str, value: str) -> None:
        """validate format of cluster label
        https://cloud.google.com/kubernetes-engine/docs/how-to/creating-managing-labels
        """
        values_re = re.compile("^[a-z][-_a-z0-9]*$")
        if not value or not 0 < len(value) < 64:
            raise ValueError(f"value {value} for cluster label {key} must be a string between 1 and 63 chars")
        if not bool(values_re.fullmatch(value)):
            raise ValueError(
                f"value {value} for cluster label {key} can contain only 'a-z', '0-9', '_', and '-' and starts only by 'a-z'"
            )

    @staticmethod
    def validate_label(key: str, value: str) -> None:
        """validate format of standard label
        https://kubernetes.io/docs/concepts/overview/working-with-objects/labels/
        """
        keys_re = re.compile("^[a-z0-9A-Z][-_.a-z0-9A-Z]*(?<![-_.])$")
        values_re = re.compile("^[a-z0-9A-Z][-_.a-z0-9A-Z]*$")
        if not key or not 0 < len(key) < 64:
            raise ValueError(f"label {key} must be a string between 1 and 63 chars")
        if not bool(keys_re.fullmatch(key)):
            raise ValueError(
                f"label {key} invalid can contain only 'a-z', 'A-Z', '0-9', '.', '_', and '-' and starts and ends only by 'a-z', 'A-Z', '0-9' "
            )
        if value:
            if len(value) > 63:
                raise ValueError(f"value {value} for label {key} cannot exceed 63 chars")
            if not bool(values_re.fullmatch(value)):
                raise ValueError(
                    f"value {value} for label {key} can contain only 'a-z', 'A-Z', '0-9', '.', '_', and '-' and starts only by 'a-z', 'A-Z', '0-9' "
                )

    def __setitem__(self, key: str, item: str) -> None:
        if key in self.CLUSTER_LABELS:
            Labels.validate_cluster_label(key, item)
        else:
            Labels.validate_label(key, item)
        return super().__setitem__(key, item)


class Kubernetes:  # pylint: disable=too-many-public-methods
    """Kubernetes client"""

    # add/modify resources easily checking:
    #    https://github.com/kubernetes-client/python/tree/master/kubernetes/docs
    def __init__(self, kubeconfig: Optional[dict] = None) -> None:
        api_client: Optional[client.ApiClient] = None
        if kubeconfig:
            log.debug("connection to k8s cluster using kubeconfig and service account credentials")
            credentials = json.loads(base64.b64decode(SETTINGS.gce_sa_info).decode("utf-8"))  # type: ignore
            with tempfile.TemporaryDirectory():
                with open(sa_json_name := "sa.json", "w", encoding="utf-8") as sa_file:
                    json.dump(credentials, sa_file)
                os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = sa_json_name
                configuration = client.Configuration()
                loader = config.kube_config.KubeConfigLoader(kubeconfig)
                loader.load_and_set(configuration)
            api_client = client.ApiClient(configuration)
        else:
            try:
                config.load_incluster_config()
                log.debug("in cluster connection to k8s")
            except config.ConfigException:
                config.load_kube_config()
                log.debug("local connection to k8s")
        self.core_api = client.CoreV1Api(api_client)
        self.batch_api = client.BatchV1Api(api_client)
        self.apps_api = client.AppsV1Api(api_client)
        self.auth_api = client.AuthorizationV1Api(api_client)
        self.rbacauthorization_api = client.RbacAuthorizationV1Api(api_client)
        self.hpa_api = client.AutoscalingV1Api(api_client)
        self.custom_api = client.CustomObjectsApi(api_client)
        self.watch = watch.Watch()

    def check_authorization(self, group: str, resource: str, verb: str, namespace: str) -> bool:
        """check if current user is authorized"""
        res = self.auth_api.create_self_subject_access_review(
            client.V1SelfSubjectAccessReview(
                spec=client.V1SelfSubjectAccessReviewSpec(
                    resource_attributes=client.V1ResourceAttributes(
                        group=group,
                        resource=resource,
                        verb=verb,
                        namespace=namespace,
                    )
                )
            )
        )
        return res.status.allowed

    def exists_namespace(self, namespace: str) -> bool:
        """checks if namespace already exists"""
        for existing_ns in self.core_api.list_namespace().items:
            if existing_ns.metadata.name == namespace:
                return True
        return False

    def create_namespace_if_not_exists(self, namespace: str, dry_run: DryRun) -> None:
        """creates namespace if don't exists in the cluster"""
        if self.exists_namespace(namespace):
            log.info(f"namespace {namespace} already exists")
        else:
            log.info(f"Creating namespace {namespace}")
            namespace_metadata = client.V1ObjectMeta(name=namespace)
            self.core_api.create_namespace(client.V1Namespace(metadata=namespace_metadata), dry_run=dry_run.value)

    def get_all_service_accounts(self, namespace: str) -> Iterator[str]:
        """gets all the service accounts from the current k8s context"""
        for service_account in self.core_api.list_namespaced_service_account(namespace).items:
            yield service_account.metadata.name

    @staticmethod
    def get_service_account(name: str, annotations: Optional[dict[str, str]]) -> client.V1ServiceAccount:
        """gets a service account"""
        return client.V1ServiceAccount(
            api_version="v1",
            kind="ServiceAccount",
            metadata=client.V1ObjectMeta(name=name, labels=Labels({"sa_name": name}).data, annotations=annotations),
        )

    @staticmethod
    def _get_role_binding(
        role_binding_cls: type[client.V1RoleBinding | client.V1ClusterRoleBinding],
        name: str,
        namespace: str,
        service_account_name: Optional[str],
        users: Optional[list[str]],
        role_name: str,
    ) -> client.V1RoleBinding | client.V1ClusterRoleBinding:
        """gets a service account"""
        kind = "RoleBinding" if role_binding_cls == client.V1RoleBinding else "ClusterRoleBinding"
        ref_kind = "Role" if role_binding_cls == client.V1RoleBinding else "ClusterRole"
        subjects = []
        if service_account_name:
            subjects.append(client.V1Subject(kind="ServiceAccount", name=service_account_name, namespace=namespace))
        if users:
            for user in users:
                subjects.append(client.V1Subject(kind="User", name=user))
        if not subjects:
            raise ValueError("service_account_name and users cannot be both None")
        if namespace is None and role_binding_cls == client.V1RoleBinding:
            raise ValueError("namespace cannot be None for a RoleBinding")
        return role_binding_cls(
            api_version="rbac.authorization.k8s.io/v1",
            kind=kind,
            metadata=client.V1ObjectMeta(
                name=name,
                labels=Labels({"rb_name": name, "sa_name": service_account_name, "role_name": role_name}).data,
            ),
            subjects=subjects,
            role_ref=client.V1RoleRef(api_group="rbac.authorization.k8s.io", kind=ref_kind, name=role_name),
        )

    @staticmethod
    def get_role_binding(
        name: str, namespace: str, service_account_name: Optional[str], users: Optional[list[str]], role_name: str
    ) -> client.V1RoleBinding:
        """gets a service account"""
        return Kubernetes._get_role_binding(
            client.V1RoleBinding, name, namespace, service_account_name, users, role_name
        )

    @staticmethod
    def get_cluster_role_binding(
        name: str, namespace: str, service_account_name: Optional[str], users: Optional[list[str]], role_name: str
    ) -> client.V1ClusterRoleBinding:
        """gets a service account"""
        return Kubernetes._get_role_binding(
            client.V1ClusterRoleBinding, name, namespace, service_account_name, users, role_name
        )

    @staticmethod
    def _get_role(
        role_cls: type[client.V1Role | client.V1ClusterRole],
        name: str,
        api_group: str,
        resource: str,
        resource_names: list[str],
        verbs: List[APIRequestVerb],
    ) -> client.V1Role | client.V1ClusterRole:
        kind = "Role" if role_cls == client.V1Role else "ClusterRole"
        api_group = "" if api_group == "core" else api_group
        return role_cls(
            api_version="rbac.authorization.k8s.io/v1",
            kind=kind,
            metadata=client.V1ObjectMeta(name=name, labels=Labels({"role_name": name}).data),
            rules=[
                client.V1PolicyRule(
                    api_groups=[api_group],
                    resources=[resource],
                    resource_names=resource_names if resource_names else None,
                    verbs=[v.value for v in verbs],
                )
            ],
        )

    @staticmethod
    def get_role(
        name: str, api_group: str, resource: str, resource_names: list[str], verbs: List[APIRequestVerb]
    ) -> client.V1Role:
        """Get the a Role"""
        # https://kubernetes.io/docs/reference/access-authn-authz/rbac/
        # "" indicates the core API group
        return Kubernetes._get_role(client.V1Role, name, api_group, resource, resource_names, verbs)

    @staticmethod
    def get_cluster_role(
        name: str,
        api_group: str,
        resource: str,
        resource_names: list[str],
        verbs: List[APIRequestVerb],
    ) -> client.V1ClusterRole:
        """Get the a Role"""
        return Kubernetes._get_role(client.V1ClusterRole, name, api_group, resource, resource_names, verbs)

    @staticmethod
    def get_pod_volume_claim(
        name: str,
        claim_name: str,
    ) -> client.V1Volume:
        """Get the Volume claim related to a PVC for the pod"""
        return client.V1Volume(
            name=name, persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(claim_name=claim_name)
        )

    @staticmethod
    def get_pod_volume_from_configmap(
        name: str,
        configmap_name: str,
        keys: list[str],
        default_mode: int,
    ) -> client.V1Volume:
        """Get the Volume claim related to a Configmap (and the keys to mount) for the pod"""
        return client.V1Volume(
            name=name,
            config_map=client.V1ConfigMapVolumeSource(
                name=configmap_name, items=[{"key": k, "path": k} for k in keys], default_mode=default_mode
            ),
        )

    @staticmethod
    def get_pod_volume_from_secret(
        name: str,
        secret_name: str,
        keys: list[str],
        default_mode: int,
    ) -> client.V1Volume:
        """Get the Volume claim related to a Configmap (and the keys to mount) for the pod"""
        return client.V1Volume(
            name=name,
            secret=client.V1SecretVolumeSource(
                secret_name=secret_name, items=[{"key": k, "path": k} for k in keys], default_mode=default_mode
            ),
        )

    @staticmethod
    def get_empty_dir_volume(name: str) -> client.V1Volume:
        """Get the Volume claim related to a Configmap (and the keys to mount) for the pod"""
        return client.V1Volume(name=name, empty_dir=client.V1EmptyDirVolumeSource())

    @staticmethod
    def get_container_volume_mount(name: str, mount_path: str, sub_path: Optional[str]) -> client.V1VolumeMount:
        """get the pod VolumeMount for a Pod Volume Claim"""
        return client.V1VolumeMount(
            name=name,
            mount_path=mount_path,
            sub_path=sub_path,
        )

    @staticmethod
    def get_container_spec(
        image: str,
        name: str,
        image_pull_policy: str,
        command: Optional[List[str]],
        args: Optional[List[str]],
        resources_request: dict,
        env: Optional[List[client.V1EnvVar]] = None,
        ports: Optional[dict] = None,
        volume_mounts: Optional[List[client.V1VolumeMount]] = None,
        readiness_command: Optional[List[str]] = None,
        liveness_command: Optional[List[str]] = None,
        liveness_pre_stop_command: Optional[List[str]] = None,
        liveness_post_start_command: Optional[List[str]] = None,
        security_context_uid: Optional[int] = None,
    ) -> client.V1Container:
        """gets the specification for a k8s container"""
        _ports, readiness_probe, liveness_probe, life_cycle = None, None, None, None
        if ports:
            _ports = [client.V1ContainerPort(container_port=p, name=n) for n, p in ports.items()]
        if readiness_command:
            readiness_probe = client.V1Probe(
                _exec=client.V1ExecAction(readiness_command), initial_delay_seconds=5, period_seconds=5
            )
        if liveness_command:
            liveness_probe = client.V1Probe(
                _exec=client.V1ExecAction(liveness_command),
                initial_delay_seconds=30,
                period_seconds=30,
                failure_threshold=3,
            )
        if liveness_pre_stop_command or liveness_post_start_command:
            pre_stop, post_start = None, None
            if liveness_pre_stop_command:
                pre_stop = client.V1LifecycleHandler(_exec=client.V1ExecAction(command=liveness_pre_stop_command))
            if liveness_post_start_command:
                post_start = client.V1LifecycleHandler(_exec=client.V1ExecAction(command=liveness_post_start_command))
            life_cycle = client.V1Lifecycle(pre_stop=pre_stop, post_start=post_start)

        return client.V1Container(
            image=image,
            name=name,
            image_pull_policy=image_pull_policy,
            command=command,
            args=args,
            env=env,
            ports=_ports,
            volume_mounts=volume_mounts,
            readiness_probe=readiness_probe,
            liveness_probe=liveness_probe,
            lifecycle=life_cycle,
            # we do not specify resources for now, will pick minimum
            resources=client.V1ResourceRequirements(requests=resources_request),
            security_context=Kubernetes.get_security_context(security_context_uid),
        )

    @staticmethod
    def get_sql_proxy_container_spec() -> client.V1Container:
        """gets the sidecard cotainer spec necessary to connect to Cloud SQL"""
        # https://github.com/GoogleCloudPlatform/cloud-sql-proxy/blob/main/examples/k8s-sidecar/proxy_with_workload_identity.yaml
        return client.V1Container(
            name="cloud-sql-proxy",
            image="gcr.io/cloud-sql-connectors/cloud-sql-proxy:2.1.0",
            args=[
                # Enables Automatic IAM Authentication for all instances
                "--auto-iam-authn",
                # If connecting from a VPC-native GKE cluster, you can use the
                # following flag to have the proxy connect over private IP
                "--private-ip",
                # Enable structured logging with LogEntry format:
                "--structured-logs",
                # Replace DB_PORT with the port the proxy should listen on
                f"--port={const.CLOUD_SQL_PROXY_PORT}",
                SETTINGS.cloud_sql_connection_string,
            ],
            # The default Cloud SQL Auth Proxy image runs as the
            # "nonroot" user and group (uid: 65532) by default.
            security_context=client.V1SecurityContext(run_as_non_root=True),
            resources=client.V1ResourceRequirements(
                requests={
                    # The proxy's memory use scales linearly with the number of active
                    # connections. Fewer open connections will use less memory. Adjust
                    # this value based on your application's requirements.
                    "memory": "1Gi",
                    # The proxy's CPU use scales linearly with the amount of IO between
                    # the database and the application. Adjust this value based on your
                    # application's requirements.
                    "cpu": 1,
                }
            ),
        )

    @staticmethod
    def get_security_context(security_context_uid: Optional[int]) -> Optional[client.V1PodSecurityContext]:
        """get the security context"""
        if not security_context_uid:
            return None
        return client.V1PodSecurityContext(
            fs_group=security_context_uid,
            run_as_group=security_context_uid,
            run_as_user=security_context_uid,
            run_as_non_root=True,
            # fs_group_change_policy="Always",
            seccomp_profile=client.V1SeccompProfile(type="RuntimeDefault"),
        )

    @staticmethod
    def get_pod_template_spec(
        pod_name: str,
        containers: list[client.V1Container],
        init_containers: List[client.V1Container],
        labels: Dict[str, str],
        pull_secret: str,
        service_account_name: Optional[str],
        restart_policy: RestartPolicy,
        volumes: Optional[List[client.V1Volume]] = None,
        security_context_uid: Optional[int] = None,
        termination_grace_period_seconds: Optional[int] = None,
    ) -> client.V1PodTemplateSpec:
        """get the specification of a new pod template"""
        image_pull_secrets: Optional[list] = None
        if pull_secret:
            image_pull_secrets = [client.V1LocalObjectReference(name=pull_secret)]
        pod_template = client.V1PodTemplateSpec(
            spec=client.V1PodSpec(
                restart_policy=restart_policy.value,
                containers=containers,
                init_containers=init_containers,
                image_pull_secrets=image_pull_secrets,
                service_account_name=service_account_name,
                automount_service_account_token=True,
                volumes=volumes,
                security_context=Kubernetes.get_security_context(security_context_uid),
                termination_grace_period_seconds=termination_grace_period_seconds,
            ),
            metadata=client.V1ObjectMeta(name=pod_name, labels=Labels({"pod_name": pod_name, **labels}).data),
        )
        return pod_template

    @staticmethod
    def get_job(
        name: str,
        pod_template: client.V1PodTemplateSpec,
        labels: Dict[str, str],
        backoff_limit: int,
        cleanup_after_seconds: Optional[int] = None,
    ) -> client.V1Job:
        """get the specification of a new job"""
        return client.V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=client.V1ObjectMeta(name=name, labels=Labels({"job_name": name, **labels}).data),
            spec=client.V1JobSpec(
                backoff_limit=backoff_limit,
                template=pod_template,
                ttl_seconds_after_finished=cleanup_after_seconds,
            ),
        )

    @staticmethod
    def get_cronjob(name: str, pod_template: client.V1PodTemplateSpec, schedule: CronTab) -> client.V1CronJob:
        """get the specification of a new job"""
        job = client.V1CronJob(
            api_version="batch/v1",
            kind="CronJob",
            metadata=client.V1ObjectMeta(name=name, labels=Labels({"cronjob_name": name, "component": name}).data),
            spec=client.V1CronJobSpec(
                schedule=schedule,
                job_template=client.V1JobTemplateSpec(
                    spec=client.V1JobSpec(backoff_limit=const.BACKOFF_LIMIT_CRONJOB, template=pod_template)
                ),
                # failed_jobs_history_limit=3
                concurrency_policy=ConcurrencyPolicy.ALLOW.value,
            ),
        )
        return job

    @staticmethod
    def get_statefulset(
        name: str,
        pod_template: client.V1PodTemplateSpec,
        replicas: int,
        pvc_templates: list[client.V1PersistentVolumeClaimTemplate],
    ) -> client.V1StatefulSet:
        """get the specification of a new deployment"""
        steateful_set = client.V1StatefulSet(
            metadata=client.V1ObjectMeta(name=name, labels=Labels({"deployment_name": name, "component": name}).data),
            spec=client.V1StatefulSetSpec(
                replicas=replicas,
                template=pod_template,
                selector=client.V1LabelSelector(match_labels=pod_template.metadata.labels),
                volume_claim_templates=pvc_templates,
                service_name=name,
            ),
        )
        return steateful_set

    @staticmethod
    def get_deployment(name: str, pod_template: client.V1PodTemplateSpec, replicas: int) -> client.V1Deployment:
        """get the specification of a new deployment"""
        deployment = client.V1Deployment(
            api_version="apps/v1",
            kind="Deployment",
            metadata=client.V1ObjectMeta(name=name, labels=Labels({"deployment_name": name, "component": name}).data),
            spec=client.V1DeploymentSpec(
                replicas=replicas,
                template=pod_template,
                selector=client.V1LabelSelector(match_labels=pod_template.metadata.labels),
                strategy=client.V1DeploymentStrategy(type=DeploymentStrategyType.RECREATE.value),
            ),
        )
        return deployment

    @staticmethod
    def get_service(name: str, ports: list[client.V1ServicePort], selector: dict) -> client.V1Service:
        """get a service"""
        return client.V1Service(
            api_version="v1",
            kind="Service",
            metadata=client.V1ObjectMeta(name=name, labels=Labels({"service": name, "component": name}).data),
            spec=client.V1ServiceSpec(ports=ports, type="ClusterIP", selector=selector),
        )
        # spec.ports[0].nodePort: Forbidden: may not be used when `type` is 'ClusterIP

    @staticmethod
    def get_hpa(
        name: str,
        target_kind: str,
        target_name: str,
        min_replicas: int,
        max_replicas: int,
        target_cpu_utilization_percentage: int,
    ) -> client.V1HorizontalPodAutoscaler:
        """get a service"""
        if target_kind in ["Deployment", "StatefulSet"]:
            # should match the version of get_deployment
            api_version = "apps/v1"
        else:
            raise ValueError(f"target_kind {target_kind} not supported")

        return client.V2HorizontalPodAutoscaler(
            kind="HorizontalPodAutoscaler",
            metadata=client.V1ObjectMeta(name=name, labels=Labels({"hpa_name": name, "component": name}).data),
            spec=client.V2HorizontalPodAutoscalerSpec(
                scale_target_ref=client.V2CrossVersionObjectReference(
                    api_version=api_version, kind=target_kind, name=target_name
                ),
                min_replicas=min_replicas,
                max_replicas=max_replicas,
                # target_cpu_utilization_percentage=target_cpu_utilization_percentage,
                metrics=[
                    client.V2MetricSpec(
                        type="Resource",
                        resource=client.V2ResourceMetricSource(
                            name="cpu",
                            target=client.V2MetricTarget(
                                type="Utilization", average_utilization=target_cpu_utilization_percentage
                            ),
                        ),
                    )
                ],
            ),
        )

    @staticmethod
    def get_vpa(
        name: str,
        target_kind: str,
        target_name: str,
        container_name: Optional[str],
        min_allowed: Optional[PodResources],
        max_allowed: Optional[PodResources],
        control_cpu: bool,
        control_memory: bool,
    ) -> dict:
        """get the custom spec for the VPA"""
        spec: dict = {
            "kind": "VerticalPodAutoscaler",
            "metadata": {"name": name},
            "spec": {
                "targetRef": {"apiVersion": "apps/v1", "kind": target_kind, "name": target_name},
            },
        }

        update_policy: dict = {"updateMode": "Auto"}
        if not control_cpu and not control_memory:
            raise ValueError("at least one of control_cpu or control_memory must be True")
        if not control_cpu or not control_memory:
            update_policy["controlledResources"] = ["cpu"] if control_cpu else ["memory"]
        if min_allowed or max_allowed:
            container_policy: dict = {"containerName": container_name} if container_name else {}
            if min_allowed:
                container_policy["minAllowed"] = {
                    k: v for k, v in min_allowed.to_dict().items() if k != "ephemeral-storage"
                }
            if max_allowed:
                container_policy["maxAllowed"] = {
                    k: v for k, v in max_allowed.to_dict().items() if k != "ephemeral-storage"
                }
            spec["spec"]["resourcePolicy"] = {"containerPolicies": [container_policy]}
        spec["spec"]["updatePolicy"] = update_policy
        return spec

    @staticmethod
    def get_persistent_volume(name: str, storage: str, disk_name: str) -> client.V1PersistentVolume:
        """gets a persistent volume"""
        return client.V1PersistentVolume(
            api_version="v1",
            kind="PersistentVolume",
            metadata=client.V1ObjectMeta(
                name=name, labels=Labels({"volume": name, "component": name.replace("-pv", "")}).data
            ),
            spec=client.V1PersistentVolumeSpec(
                capacity={"storage": storage},
                storage_class_name="manual",
                access_modes=["ReadWriteOnce"],
                gce_persistent_disk=client.V1GCEPersistentDiskVolumeSource(pd_name=disk_name, fs_type="ext4"),
            ),
        )

    @staticmethod
    def get_persistent_volume_claim(name: str, storage: str) -> client.V1PersistentVolumeClaim:
        """gets a persistent volume claim"""
        return client.V1PersistentVolumeClaim(
            api_version="v1",
            kind="PersistentVolumeClaim",
            metadata=client.V1ObjectMeta(name=name, labels=Labels({"component": name.replace("-pvc", "")}).data),
            spec=client.V1PersistentVolumeClaimSpec(
                access_modes=["ReadWriteOnce"],
                resources=client.V1ResourceRequirements(requests={"storage": storage}),
            ),
        )

    @staticmethod
    def get_persistent_volume_claim_template(name: str, storage: str) -> client.V1PersistentVolumeClaimTemplate:
        """gets a persistent volume claim"""
        return client.V1PersistentVolumeClaimTemplate(
            metadata=client.V1ObjectMeta(name=name, labels=Labels({"component": name.replace("-pvc", "")}).data),
            spec=client.V1PersistentVolumeClaimSpec(
                access_modes=["ReadWriteOnce"],
                volume_mode="Filesystem",
                resources=client.V1ResourceRequirements(requests={"storage": storage}),
            ),
        )

    @staticmethod
    def get_configmap(name: str, data: Dict[str, str]) -> client.V1ConfigMap:
        """gets a config map"""
        return client.V1ConfigMap(
            api_version="v1",
            kind="ConfigMap",
            metadata=client.V1ObjectMeta(name=name),
            data=data,
        )

    @staticmethod
    def get_secret(
        name: str,
        secret_type: SecretType,
        string_data: Optional[Dict[str, str]] = None,
        data: Optional[Dict[str, str]] = None,
    ) -> client.V1Secret:
        """gets a secret"""
        return client.V1Secret(
            api_version="v1",
            kind="Secret",
            type=secret_type.value,
            metadata=client.V1ObjectMeta(name=name),
            string_data=string_data,
            data=data,
        )

    def get_env_pair(self, key: str, value: str) -> client.V1EnvVar:
        """gets the list of env var of a pod from a dictionary"""
        return client.V1EnvVar(key, value)

    def get_env_from_dict(self, data: dict) -> List[client.V1EnvVar]:
        """gets the list of env var of a pod from a dictionary"""
        env = []
        for key, value in data.items():
            if isinstance(value, client.V1EnvVarSource):
                env.append(client.V1EnvVar(name=key, value_from=value))
            else:
                env.append(self.get_env_pair(key, value))
        return env

    @staticmethod
    def describe_envvar(env_var: client.V1EnvVar) -> str:
        """Gets a basica description of an environment variable"""
        if not env_var.value_from:
            return f"V1EnvVar({env_var.name} with a direct value)"
        if ref := env_var.value_from.config_map_key_ref:
            return f"V1EnvVar({env_var.name} from config-map:{ref})"
        if ref := env_var.value_from.secret_key_ref:
            return f"V1EnvVar({env_var.name} from secret:{ref})"
        return f"V1EnvVar({env_var.name} from unknow type)"

    def upsert_envvars(self, base_env: list[client.V1EnvVar], new_env: list[client.V1EnvVar]) -> list[client.V1EnvVar]:
        """return the base list of env variables (base_env) upserting (insert/update) the new_env"""
        env_map = {item.name: item for item in base_env}
        for new_item in new_env:
            if new_item.name in env_map:
                log.warning(
                    f"replacing existing {self.describe_envvar(env_map[new_item.name])} by new {self.describe_envvar(new_item)}"
                )
            else:
                log.debug(f"Adding new {self.describe_envvar(new_item)} to environment variables")
            env_map[new_item.name] = new_item
        return list(env_map.values())

    def get_env_from_source(self, item_names: List[str]) -> List[client.V1EnvVar]:
        """Gets a list of env variables for the specified list of Secrets or Configmaps"""
        # we could add filters if necessary
        list_envs = []

        def add_to_list(item: client.V1Secret | client.V1ConfigMap) -> None:
            if (name := item.metadata.name) in item_names:
                log.debug("adding %s %s to the environment variables", type(item).__name__, name)
                if item.__class__.__name__ == "V1ConfigMap":
                    selector = client.V1ConfigMapKeySelector
                    key_ref = "config_map_key_ref"
                elif item.__class__.__name__ == "V1Secret":
                    selector = client.V1SecretKeySelector
                    key_ref = "secret_key_ref"
                else:
                    raise ValueError(f"Unexpected type {type(item)}")
                for key in item.data.keys():
                    map_ref = selector(key=key, name=name)
                    env_var = client.V1EnvVarSource(**{key_ref: map_ref})
                    env_object = client.V1EnvVar(name=key, value_from=env_var)
                    list_envs.append(env_object)

        for configmap in self.core_api.list_namespaced_config_map("default").items:
            add_to_list(configmap)

        for secret in self.core_api.list_namespaced_secret("default").items:
            add_to_list(secret)

        return list_envs

    @staticmethod
    def get_suspend_cronjob_body(cronjob: client.V1CronJob, suspend: bool) -> client.V1CronJob:
        """Gets the cronjob body necessary to patch to suspend:bool the cronjob"""
        return client.V1CronJob(
            metadata=client.V1ObjectMeta(name=cronjob.metadata.name),
            spec=client.V1CronJobSpec(
                schedule=cronjob.spec.schedule, job_template=client.V1JobTemplateSpec(), suspend=suspend
            ),
        )

    def _change_all_cronjobs_suspend(self, namespace: str, suspend: bool, async_req: bool, dry_run: DryRun) -> None:
        for cronjob in self.batch_api.list_namespaced_cron_job(namespace).items:
            if bool(cronjob.spec.suspend) != suspend:
                cronjob.spec.suspend = suspend
                log.info("changing CronJob %s suspent to %s", cronjob.metadata.name, suspend)
                # specifying the minimal options to just patch original cronjob and suspend it
                self.batch_api.patch_namespaced_cron_job(
                    name=cronjob.metadata.name,
                    namespace=namespace,
                    body=self.get_suspend_cronjob_body(cronjob, suspend),
                    async_req=async_req,
                    dry_run=dry_run.value,
                )

    def enable_all_cronjobs(self, namespace: str, async_req: bool = False, dry_run: DryRun = DryRun.OFF) -> None:
        """Enable all the cronjobs from the namespace (suspend=False)"""
        self._change_all_cronjobs_suspend(namespace, False, async_req, dry_run)

    def disable_all_cronjobs(self, namespace: str, async_req: bool = False, dry_run: DryRun = DryRun.OFF) -> None:
        """Disable all the cronjobs from the namespace (suspend=True)"""
        self._change_all_cronjobs_suspend(namespace, True, async_req, dry_run)

    def get_cluster_resources(
        self, namescape: str, label_selector: Optional[dict[str, str]] = None, get_pods: bool = True
    ) -> ClusterResources:
        """get the cluster resources"""

        nodes = self.core_api.list_node()
        if get_pods:
            pods = self.get_cluster_pods(namescape, label_selector)
            pods_metrics = self.custom_api.list_namespaced_custom_object(
                group="metrics.k8s.io", version="v1beta1", namespace=namescape, plural="pods"
            )
            return ClusterResources.from_cluster_info(nodes.items, pods, pods_metrics["items"])
        return ClusterResources.from_cluster_info(nodes.items, [], [])

    def get_cluster_pods(self, namescape: str, label_selector: Optional[dict[str, str]] = None) -> list[client.V1Pod]:
        """get the cluster resources"""
        _label_selector = None
        if label_selector:
            _label_selector = ",".join(f"{k}={v}" for k, v in label_selector.items())
        return self.core_api.list_namespaced_pod(namespace=namescape, label_selector=_label_selector).items


# if __name__ == "__main__":
#     k8s = Kubernetes()
#     k8s.get_env_from_source(["db-config"])
#     pass
