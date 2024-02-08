
import logging
import os
import tempfile
import json
import base64
from typing import Dict, List, Optional, Iterator

from kubernetes import client, config, watch

logger = logging.getLogger(__name__)

class K8sClient:
    """Kubernetes client"""

    def __init__(self, kubeconfig: Optional[dict] = None) -> None:
        api_client: Optional[client.ApiClient] = None
        if kubeconfig:
            logger.debug("connection to k8s cluster using kubeconfig and service account credentials")
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
                logger.debug("in cluster connection to k8s")
            except config.ConfigException:
                config.load_kube_config()
                logger.debug("local connection to k8s")
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

   

    def get_cluster_resources(
        self, namescape: str, label_selector: Optional[dict[str, str]] = None, get_pods: bool = True
    ) -> "ClusterResources":
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


if __name__ == "__main__":
    k8s = K8sClient()
    resources = k8s.get_cluster_resources("default")
    pass
