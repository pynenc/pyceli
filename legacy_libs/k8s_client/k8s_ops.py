import json
from multiprocessing.pool import ApplyResult
from typing import Callable, Optional

from kubernetes.client.exceptions import ApiException

import logger

from alerts.teams_client import alert
from k8s_client import k8s_client
from k8s_client.k8s_model import K8sModel
from k8s_client import constants as const
from k8s_client.k8s_client import client
from src.errors import K8sDeploymentError


log = logger.get_logger(__name__)


def delete(name: str, _type: str, delete_func: Callable, dry_run: k8s_client.DryRun) -> None:
    """delete and ignore NotFound errors"""
    try:
        log.warning("Deleting %s %s because is not specify in the model", _type, name)
        delete_func(name, const.DEFAULT_NAMESPACE, dry_run=dry_run.value)
    except ApiException as ex:
        if json.loads(ex.body).get("reason") != "NotFound":
            raise
        log.info("%s %s do not exists, nothing to delete", _type, name)


def delete_out_of_model(k8s_model: K8sModel, k8s: k8s_client.Kubernetes, dry_run: k8s_client.DryRun) -> None:
    """delete any deployment, cronjob or job that is not specified in the model"""
    # Delete any cronjob not defined in the model
    for cronjob in k8s.batch_api.list_namespaced_cron_job(const.DEFAULT_NAMESPACE).items:
        if (name := cronjob.metadata.name) not in k8s_model.all_pod_keys:
            delete(name, "Cronjob", k8s.batch_api.delete_namespaced_cron_job, dry_run)
    # delete any deployment not defined in the model
    for deployment in k8s.apps_api.list_namespaced_deployment(const.DEFAULT_NAMESPACE).items:
        if (name := deployment.metadata.name) not in k8s_model.all_pod_keys:
            if "postgresql" in name:
                log.warning(f"Skipping deletion of old postgres deployment {name}")
                continue
            if name.startswith("pg"):
                log.warning(f"Skipping deletion of old distributed postgres deployment {name}")
                continue
            delete(name, "Deployment", k8s.apps_api.delete_namespaced_deployment, dry_run)
            for service in k8s.core_api.list_namespaced_service(
                const.DEFAULT_NAMESPACE, field_selector=f"metadata.name={name}"
            ).items:
                delete(service.metadata.name, "Deployment-Service", k8s.core_api.delete_namespaced_service, dry_run)

    # delete any job not defined in the model
    def is_in_model_job_owner_ref(job: client.V1Job) -> bool:
        for owner_ref in job.metadata.owner_references or []:
            if owner_ref.name in k8s_model.all_pod_keys:
                return True
        return False

    for job in k8s.batch_api.list_namespaced_job(const.DEFAULT_NAMESPACE).items:
        if (name := job.metadata.name) in k8s_model.all_pod_keys:
            continue
        if is_in_model_job_owner_ref(job):
            continue
        delete(name, "Job", k8s.batch_api.delete_namespaced_job, dry_run)


def clean_up_pods(k8s: k8s_client.Kubernetes, dry_run: k8s_client.DryRun) -> None:
    """Clean up all the pods that are completed/error"""
    for job in k8s.batch_api.list_namespaced_job(const.DEFAULT_NAMESPACE).items:
        if job.metadata.labels.get("component") == const.TASKER_SCHEDULER_NAME:
            delete(job.metadata.name, "Job", k8s.batch_api.delete_namespaced_job, dry_run)
    for pod in k8s.core_api.list_namespaced_pod(const.DEFAULT_NAMESPACE).items:
        if (phase := pod.status.phase) in [k8s_client.PhasePod.FAILED.value, k8s_client.PhasePod.SUCCEEDED.value]:
            log.info("deleting %s Pod %s", phase, name := pod.metadata.name)
            delete(name, "Pod", k8s.core_api.delete_namespaced_pod, dry_run)
        elif pod.metadata.labels.get("component") == const.TASKER_SCHEDULER_NAME:
            log.info("deleting tasker Pod %s", name := pod.metadata.name)
            delete(name, "Pod", k8s.core_api.delete_namespaced_pod, dry_run)


def canary_test(k8s: k8s_client.Kubernetes, dry_run: k8s_client.DryRun) -> None:
    """Test a canary pod before proceding with the deployment"""
    k8s.create_namespace_if_not_exists(const.DEFAULT_NAMESPACE, dry_run)
    canary_name = "canary"
    try:
        K8sModel.canary_job(k8s, dry_run, canary_name)
    except Exception as ex:
        raise K8sDeploymentError(f"Aborting GKE deployment, canary job:{canary_name} failed") from ex
    finally:
        for job in k8s.batch_api.list_namespaced_job(const.DEFAULT_NAMESPACE).items:
            if (name := job.metadata.name) == canary_name:
                delete(name, "Job", k8s.batch_api.delete_namespaced_job, dry_run)
        for pod in k8s.core_api.list_namespaced_pod(
            const.DEFAULT_NAMESPACE, label_selector=f"job-name={canary_name}"
        ).items:
            delete(pod.metadata.name, "Pod", k8s.core_api.delete_namespaced_pod, dry_run)


def test_deploy(docker_image: str, kubeconfig: Optional[dict] = None) -> None:
    """Test the deployment to kubernetes"""
    del docker_image  # why? just to have image in the alert
    deploy(k8s=k8s_client.Kubernetes(kubeconfig), dry_run=k8s_client.DryRun.ON)


@alert(ignore_attributes=["kubeconfig"], database_logging=False)
def deploy_to_kuberentes(docker_image: str, kubeconfig: Optional[dict] = None) -> None:
    """deploys to kubernetes"""
    del docker_image
    deploy(k8s=k8s_client.Kubernetes(kubeconfig), dry_run=k8s_client.DryRun.OFF)


def deploy(k8s: k8s_client.Kubernetes, dry_run: k8s_client.DryRun = k8s_client.DryRun.OFF) -> None:
    """deploys to the kubernetes cluster"""
    # disable cronjobs, so nothing unexpected run during deployment
    # parallel jobs are not killed (can still try to finish)
    k8s_model = K8sModel()
    # before deploying, check the status of the databases in the cluster
    # during deployment other processes may be stopped, and the dbs cpu/mem consumption decrease
    cluster_resources = k8s.get_cluster_resources(
        const.DEFAULT_NAMESPACE, label_selector={"k8s_model_type": "database"}, get_pods=True
    )
    canary_test(k8s, dry_run)
    k8s.disable_all_cronjobs(const.DEFAULT_NAMESPACE, dry_run=dry_run)
    # delete the scheduler cronjob, to avoid creating new pods
    delete(const.TASKER_SCHEDULER_NAME, "Cronjob", k8s.batch_api.delete_namespaced_cron_job, dry_run)
    try:
        delete_out_of_model(k8s_model, k8s, dry_run)
        clean_up_pods(k8s, dry_run)
        # apply all the configmaps and secrets
        for _k8s_map in k8s_model.maps:
            _k8s_map.apply(k8s, dry_run=dry_run)
        # after configmaps and secrets, delete again any pod that could have been created
        # that ensure that any new pod will run with the new image
        clean_up_pods(k8s, dry_run)
        k8s_model.deploy_volumes(k8s, dry_run)
        # create service accounts
        for service_account in k8s_model.service_accounts:
            service_account.apply(k8s, dry_run=dry_run)
        k8s_model.deploy_databases(k8s, dry_run, cluster_resources)
        # create all the other pods
        results = []
        for pod in k8s_model.pods:
            results.append(pod.apply(k8s, async_req=True, dry_run=dry_run))
        # wait for request response of all the pods
        for result in results:
            if isinstance(result, ApplyResult):
                log.info("waiting for async call to k8s api %s", result)
                result = result.get(timeout=30)
            if hasattr(result, "kind") and hasattr(result, "metadata"):
                log.info("K8s object %s %s created", result.kind, getattr(result.metadata, "name", "n/a"))
    finally:
        k8s.enable_all_cronjobs(const.DEFAULT_NAMESPACE, dry_run=dry_run)
    log.info("deployment completed")


if __name__ == "__main__":
    k8s_model = K8sModel()
    k8s = k8s_client.Kubernetes()
    dry_run = k8s_client.DryRun.OFF

    ###### DEPLOY EVERYTHING ######
    deploy(k8s, dry_run)

    #     #     # # # # Run db
    #     #     # # k8s_model.job_init_mongo_db.apply(k8s, dry_run=dry_run)
    k8s_model.job_init_db.apply(k8s, dry_run=dry_run)

    #     #     # # from k8s_client import k8s_model_util as util

    #     #     # # util.wait_for_items(k8s, dry_run, [k8s_model.job_init_db, k8s_model.job_init_mongo_db])
    clean_up_pods(k8s, dry_run)

    # APPLY CONFIGMAPS AND SECRETS
    for _k8s_map in k8s_model.maps:
        _k8s_map.apply(k8s, dry_run=dry_run)

    ###################################
    ## Deploy only project-sync
    for pod in k8s_model.pods:
        if pod.name == const.TASKER_SCHEDULER_NAME:
            # if pod.name == "project-sync":
            # if pod.name in (const.TASKER_SCHEDULER_NAME, "project-sync"):
            result = pod.apply(k8s, async_req=True, dry_run=dry_run)
            result = result.get(timeout=30)
    ###################################

#     #     ####################################################################
# #     ### TEST PROJECT_SYNC
# #     for pod in k8s_model.pods:
# #         if pod.name == "project-sync":
# #             pod.apply(k8s, dry_run=dry_run)

# ## DEPLOY POSTGRES ######
# from k8s_client.k8s_model import PostgresStatefulSetComponents

# pg_components = PostgresStatefulSetComponents()
# for cm in pg_components.configmaps:
#     cm.apply(k8s, dry_run=dry_run)
# pg_components.tls_secret.apply(k8s, dry_run=dry_run)
# for service in pg_components.services:
#     service.apply(k8s, dry_run=dry_run)
# pg_components.auto_config_service.delete(k8s, dry_run=dry_run)
# pg_components.stateful_set.apply(k8s, dry_run=dry_run)

# ## DEPLOY MONGO ######
# for pvc in k8s_model.persistent_volume_claims:
#     if pvc.name == "mongodb-storage-pvc":
#         pvc.apply(k8s, dry_run=dry_run)
#         break
# k8s_model.deployment_mongodb.apply(k8s, dry_run=dry_run)


#     #     # ####################################################################
#     #     # ### DEPLOY MONGODB
#     #     # for pvc in k8s_model.persistent_volume_claims:
#     #     #     if pvc.name == "mongodb-storage-pvc":
#     #     #         pvc.apply(k8s, dry_run=dry_run)
#     #     #         break
#     #     # k8s_model.deployment_mongodb.apply(k8s, dry_run=dry_run)
#     #     # k8s_model.job_init_mongo_db.apply(k8s, dry_run=dry_run)

#     ## DEPLY POSTGRES
#     cluster_resources = k8s.get_cluster_resources(
#         const.DEFAULT_NAMESPACE, label_selector={"k8s_model_type": "database"}, get_pods=True
#     )
#     k8s_model.postgres_access_node.configmap.apply(k8s, dry_run=dry_run)
#     k8s_model.postgres_access_node.secret.apply(k8s, dry_run=dry_run)
#     k8s_model.postgres_access_node.pvc.apply(k8s, dry_run=dry_run)
#     k8s_model.deploy_databases(k8s, dry_run, cluster_resources)

#     k8s_model.postgresql_node_config_configmap.apply(k8s, dry_run=dry_run)

#     pvc_timescale = lib.PersistentVolumeClaim("timescale-node0-storage-pvc", "100Gi")
#     pvc_timescale.apply(k8s)
#     db_volume_mount = model.lib.VolumeMountPVC(
#         mount_path="/var/lib/postgresql/data/pg_data",
#         sub_path="postgres",
#         pvc=pvc_timescale,
#     )

#     # mount the config files from the configmap and specify location arguments
#     db_config_mount = lib.VolumeMountConfigMap(
#         mount_path="/var/lib/postgresql/config",
#         config_map=k8s_model.postgresql_node_config_configmap,
#     )
#     args = []
#     for filename in db_config_mount.config_map.data.keys():
#         if filename == model.const.POSTGRES_NODE_CONFIG_FILENAME:
#             config_arg = "config_file"
#         elif filename == model.const.POSTGRES_HOST_CONFIG_FILENAME:
#             config_arg = "hba_file"
#         else:
#             raise ValueError(f"Unexpected {filename=} in postgres configmap {db_config_mount.config_map}")
#         args.extend(["-c", config_arg + "=" + model.os.path.join(db_config_mount.mount_path, filename)])

#     cluster_permissions = model.ClusterPermissions(SETTINGS.cluster_name)

#     deployment_postgres = lib.Deployment(
#         name="pgts-node0",
#         replicas=1,
#         image="gcr.io/pcaas-infra/pcaas:dev_2f864d38_timescale",
#         image_pull_policy="IfNotPresent",
#         port=5432,
#         security_context_uid=1000,
#         args=args,  # specify location of config files
#         env={
#             "PGDATA": db_volume_mount.mount_path,
#             "TS_TUNE_MAX_CONNS": "1000",
#             "POSTGRES_USER": SETTINGS.postgres_root_username,
#             "POSTGRES_USERNAME": SETTINGS.postgres_root_username,
#             "POSTGRES_DB": model.const.POSTGRES_DB,
#         },
#         volumes=[db_config_mount, db_volume_mount],
#         # SETTINGS.postgres_root_username depends on the environment, and it's never null
#         readiness_command=["pg_isready", "-U", SETTINGS.postgres_root_username, "-d", model.const.POSTGRES_DB],  # type: ignore
#         create_service=True,
#         resources=model.PodResources(memory="2Gi", cpu="600m", ephemeral_storage="5Gi"),
#         service_account=cluster_permissions.timescale_db_init.k8s_workload_identity,
#     )

#     deployment_postgres.apply(k8s)


#     ####################################################################
#     ### TEST PROJECT_SYNC
#     for pod in k8s_model.pods:
#         if pod.name == "project-sync":
#             pod.apply(k8s, dry_run=dry_run)

# ####################################################################
# ### TEST DEPLOY POSTGRES
# from k8s_client.k8s_model import wait_for_items

# ### Create PVC
# k8s_model.deploy_volumes(k8s, dry_run)
# ### Init PVC permissions
# k8s_model.job_init_pvc.apply(k8s, dry_run=dry_run)
# wait_for_items(k8s, dry_run, [k8s_model.job_init_pvc])
# ### Deploy postgres
# k8s_model.deployment_postgres.apply(k8s, dry_run=dry_run)

####################################################################
# #### CREATE ALL THE PODS AFTER A FAILED DEPLOYMENT
# try:
#     results = []
#     for pod in k8s_model.pods:
#         results.append(pod.apply(k8s, async_req=True, dry_run=dry_run))
#     # wait for request response of all the pods
#     for result in results:
#         if isinstance(result, ApplyResult):
#             log.info("waiting for async call to k8s api %s", result)
#             result = result.get(timeout=30)
#         if hasattr(result, "kind") and hasattr(result, "metadata"):
#             log.info("K8s object %s %s created", result.kind, getattr(result.metadata, "name", "n/a"))
# finally:
#     k8s.enable_all_cronjobs(const.DEFAULT_NAMESPACE, dry_run=dry_run)

#     #     ###### TEST CANARY
#     #     # k8s_model.canary_job(k8s, dry_run=k8s_client.DryRun.OFF)
#     #     canary_test(k8s, dry_run=k8s_client.DryRun.OFF)

#     ##### TEST pubsub HPA #####
#     pubsub = k8s_model.pods[0]
#     if not pubsub.name == "project-sync":
#         raise Exception("wrong pod")
#     pubsub.apply(k8s, dry_run=k8s_client.DryRun.OFF)
