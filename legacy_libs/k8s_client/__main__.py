from typing import Optional
import click

from util.cli_util import check_option_and_upsert_environ, get_name_style
from settings.settings_model import SETTINGS, Settings
from k8s_client import k8s_client, k8s_ops, k8s_resources, k8s_model_lib, constants
from infra_client.infra_model import INFRASTRUCTURE
from infra_client.gcp.gcp_client import GCPClient
from sql_client.db_users import DBUser


@click.group(chain=True)
# @click.option("--docker_username", default=SETTINGS.docker_username, show_default=True, type=str)
# @click.option("--docker_password", default=Password(SETTINGS.docker_password), show_default=True, type=Password)
@click.option("--docker_image", default=SETTINGS.docker_image, show_default=True, type=str)
@click.option("--backup-bucket_name", default=SETTINGS.backup_bucket_name, show_default=True, type=str)
@click.option("--system-account-bucket_name", default=SETTINGS.system_account_bucket_name, show_default=True, type=str)
def cli(**kwargs: dict) -> None:
    """Define db_ops command line tool using click"""
    check_option_and_upsert_environ(SETTINGS, **kwargs)


def get_current_context_cluster_name() -> str:
    """get the cluster name of the current k8s context"""
    contexts, active_context = k8s_client.config.list_kube_config_contexts()
    if not contexts:
        return "Cannot find any kubectl context"
    return active_context["name"]


def get_current_context_cloud_sql_connection_string() -> str:
    """get the name of the cloud sql instance related with current context"""
    cluster_name = get_current_context_cluster_name()
    return INFRASTRUCTURE.get_cloud_sql_connection_string_by_cluster_name(cluster_name)


def print_deploy_msg(headline: str, cluster_name: str) -> None:
    """print a promt message for the deployemnt command"""
    click.echo(
        click.style(headline, blink=True, bold=True, fg="red")
        + "\n  cluster: "
        + get_name_style(cluster_name)
        + f"\n  cluster_region: {SETTINGS.cluster_region}"
        + f"\n  docker_image: {SETTINGS.docker_image}"
        + f"\n  backup_bucket_name: {get_name_style(SETTINGS.backup_bucket_name)}"
        + f"\n  system_account_bucket_name: {get_name_style(SETTINGS.system_account_bucket_name)}"
        + f"\n  postgres_user: {SETTINGS.postgres_username}"
        + f"\n  cloud_sql_connection_string: {SETTINGS.cloud_sql_connection_string}"
    )


@cli.command()
def context() -> None:
    """Gets the current kubernetes context"""
    print_deploy_msg("Current context", get_current_context_cluster_name())


def get_current_context_service_accounts() -> list[str]:
    """Gets a list with all the current context service accounts"""
    return list(k8s_client.Kubernetes().get_all_service_accounts(constants.DEFAULT_NAMESPACE))


@cli.command()
def service_accounts() -> None:
    """Gets the service accounts available in the current kubernetes context"""
    click.echo("Current context service accounts:")
    for service_account in get_current_context_service_accounts():
        click.echo(f"   - '{service_account}'")


@cli.command()
def clusters() -> None:
    """Gets the available clusters (infra_model)"""
    for env, infra in INFRASTRUCTURE.get_all():
        click.echo("Environment " + get_name_style(env) + ":")
        for cluster in infra.k8s_clusters:
            click.echo("  - " + get_name_style(cluster.name))


# common optinos
option_force = click.option("-f", "--force", is_flag=True, default=False, show_default=True, type=bool)
option_dry_run = click.option(
    "-d",
    "--dry-run",
    is_flag=True,
    default=False,
    show_default=True,
    type=bool,
    help="True: changes will not persist in k8s",
)


@cli.command()
@click.option(
    "-e",
    "--env",
    default=SETTINGS.environment,
    show_default=True,
    type=click.Choice(INFRASTRUCTURE.environments, case_sensitive=True),
)
@option_force
@option_dry_run
def deploy_env(env: str, force: bool, dry_run: bool) -> None:
    """Deploy to the kubernetes cluster"""
    gcp_client = GCPClient()
    infrastructure = INFRASTRUCTURE.get_infrastructure(env)
    click.echo("Using backup bucket: " + get_name_style(infrastructure.backup_bucket.name))
    click.echo("Using system account bucket: " + get_name_style(infrastructure.system_account_bucket.name))
    if not force and not dry_run:
        click.confirm("Confirm ?", abort=True)
    SETTINGS.backup_bucket_name = infrastructure.backup_bucket.name
    SETTINGS.system_account_bucket_name = infrastructure.system_account_bucket.name
    SETTINGS.cloud_sql_connection_string = infrastructure.cloud_sql.instance_connection_string
    if not SETTINGS.docker_image:
        raise ValueError("docker_image should be specified by arg or .env")
    for cluster in infrastructure.k8s_clusters:
        if cluster.exists(gcp_client):
            SETTINGS.cluster_name = cluster.get_name(gcp_client, only_check=dry_run)
            SETTINGS.cluster_region = cluster.region
            kubeconfig = cluster.get_kubeconfig(gcp_client, only_check=dry_run)
            if not force and not dry_run:
                print_deploy_msg("ATTENTION: You're about to deploy!!!", SETTINGS.cluster_name)
                click.confirm("Are you sure?", abort=True)
                click.echo("Deploying...")
            if dry_run:
                print_deploy_msg("DryRun Deployment...", SETTINGS.cluster_name)
                k8s_ops.test_deploy(SETTINGS.docker_image, kubeconfig)
            else:
                print_deploy_msg("Deploying...", SETTINGS.cluster_name)
                k8s_ops.deploy_to_kuberentes(SETTINGS.docker_image, kubeconfig)
        elif dry_run:
            click.echo(
                f"Ignoring checks for cluster {cluster.name}, doesn't exists in project {cluster.project.project_id}"
            )
        else:
            msg = f"Cluster {cluster.name} does not exists in project {cluster.project.project_id}"
            click.style(msg, blink=True, bold=True, fg="red")
            raise RuntimeError(msg)


@cli.command()
@option_force
@option_dry_run
def deploy(force: bool, dry_run: bool) -> None:
    """Deploy to the kubernetes cluster"""
    SETTINGS.cluster_name = get_current_context_cluster_name()
    _, _, region, _ = SETTINGS.cluster_name.split("_")
    SETTINGS.cluster_region = region
    if not force and not dry_run:
        print_deploy_msg("ATTENTION: You're about to deploy!!!", SETTINGS.cluster_name)
        click.confirm("Are you sure?", abort=True)
        click.echo("Deploying...")
    else:
        print_deploy_msg("Deploying...", SETTINGS.cluster_name)
    if not SETTINGS.docker_image:
        raise ValueError("docker_image should be specified by arg or .env")
    if dry_run:
        k8s_ops.test_deploy(SETTINGS.docker_image)
    else:
        k8s_ops.deploy_to_kuberentes(SETTINGS.docker_image)


@cli.command()
@click.option("--docker_image", default=SETTINGS.docker_image, show_default=True, type=str)
@click.option("-k", "--kill", is_flag=True, default=False, show_default=True, type=bool)
@click.option(
    "-sa",
    "--service_account",
    default=None,
    type=str,
    help="specify one of the available service accounts (command service-accounts)",
)
@click.option("-eph", "--ephemeral_storage", default=None, type=str)
@click.option("-mem", "--memory", default=None, type=str)
@click.option("-cpu", default=None, type=str)
@click.option("-p", "--sql_auth_proxy", is_flag=True, default=False, show_default=True, type=bool)
@click.option(
    "--postgres_user",
    default=None,
    type=click.Choice([user.value for user in DBUser.__members__.values()], case_sensitive=True),
    help="injects POSTGRES_USER in env var of the pod (determines the default user to connect to postgres)",
)
def debug_job(
    docker_image: str,
    kill: bool,
    service_account: Optional[str],
    ephemeral_storage: Optional[str],
    memory: Optional[str],
    cpu: Optional[str],
    sql_auth_proxy: bool,
    postgres_user: Optional[str],
) -> None:
    """Deploy/Kills a fake debug-job"""
    check_option_and_upsert_environ(SETTINGS, docker_image=docker_image)
    _service_account: Optional[k8s_model_lib.ServiceAccount] = None
    if service_account:
        if service_account not in (all_sa := get_current_context_service_accounts()):
            raise ValueError(f"{service_account=} should be one of {all_sa}")
        _service_account = k8s_model_lib.ServiceAccount(name=service_account, roles=[])
    resources: Optional[k8s_resources.PodResources] = None
    if ephemeral_storage or memory or cpu:
        resources = k8s_resources.PodResources(ephemeral_storage=ephemeral_storage, memory=memory, cpu=cpu)
    if sql_auth_proxy:
        SETTINGS.cloud_sql_connection_string = get_current_context_cloud_sql_connection_string()
    env: Optional[dict] = {Settings.docker_image.setting_id: docker_image or SETTINGS.docker_image}  # type: ignore
    if postgres_user:
        env["POSTGRES_USERNAME"] = postgres_user  # type: ignore
        # env["CLUSTER_NAME"] = "pcaas-dev"  # type: ignore  # TODO GSM remove, should be in the pod default configmap already
    if not docker_image or not SETTINGS.docker_image:
        raise ValueError("docker_image should be specified by arg or .env")
    job = k8s_model_lib.Job(
        name="debug-job",
        containers=[
            k8s_model_lib.Container(
                name="debug-job",
                command=["tail", "-f", "/dev/null"],
                resources=resources,
                env=env,
                image=docker_image or SETTINGS.docker_image,
            )
        ],
        service_account=_service_account,
        cloud_sql_proxy=sql_auth_proxy,
    )
    print(job)
    if kill:
        click.echo("Killing debug-job")
        job.delete(k8s_client.Kubernetes(), async_req=False)
        job.delete_all_pods(k8s_client.Kubernetes(), async_req=True)
    else:
        click.echo("Starting debug-job")
        job.apply(k8s_client.Kubernetes())


cli()
