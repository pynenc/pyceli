from piceli.k8s.k8s_client.client import ClientContext
from piceli.k8s.k8s_objects.base import K8sObject
from piceli.k8s.ops.deploy import deploy_op


def test_classify_k8s_objects(
    resources: list[K8sObject], resources_update: list[K8sObject]
) -> None:
    raise Exception("Disabled - integratin tests with minikube")
    ctx = ClientContext()
    filtered_resources = [
        resource for resource in resources if resource.kind != "PersistentVolume"
    ]
    deploy_op.deploy(ctx, filtered_resources, "example-namespace")
    deploy_op.deploy(ctx, resources_update, "example-namespace")

    print("done!")
