import asyncio
from typing import TYPE_CHECKING, Annotated

import typer
from kubernetes.client.exceptions import ApiException
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, TaskID
from rich.table import Table
from rich.tree import Tree

from piceli.k8s.cli import common
from piceli.k8s.exceptions import api_exceptions
from piceli.k8s.k8s_client.client import ClientContext
from piceli.k8s.k8s_objects.base import K8sObjectIdentifier
from piceli.k8s.ops import loader
from piceli.k8s.ops.deploy import (
    deployment_executor,
    deployment_graph,
    strategy_auto,
)

if TYPE_CHECKING:
    from piceli.k8s.cli.context import ContextObject

app = typer.Typer()


@app.command()
def run(
    ctx: typer.Context,
    create_namespace: Annotated[
        bool,
        typer.Option(
            ...,
            "--create-namespace",
            "-c",
            help="Create the namespace if it does not exist.",
            is_flag=True,
            show_default=True,
        ),
    ] = True,
) -> None:
    """Deploy Kubernetes Object Model to the current cluster."""
    console = Console()
    common.print_command_name(console, "Running Deployment")
    ctx_obj: "ContextObject" = ctx.obj
    strategy = strategy_auto.StrategyAuto()
    k8s_objects = loader.load_all(
        module_name=ctx_obj.module_name,
        module_path=ctx_obj.module_path,
        folder_path=ctx_obj.folder_path,
        sub_elements=ctx_obj.sub_elements,
    )
    deploy_graph = strategy.build_deployment_graph(k8s_objects)
    deploy_graph.validate()
    executor = deployment_executor.DeploymentExecutor(deploy_graph)
    if create_namespace:
        _upsert_namespace(console, ctx_obj.namespace)
    asyncio.run(_run_deployment(console, executor, ctx_obj.namespace, create_namespace))


def _upsert_namespace(console: Console, namespace_name: str) -> None:
    body = {"metadata": {"name": namespace_name}}
    client_ctx = ClientContext()
    try:
        client_ctx.core_api.read_namespace(name=namespace_name)
    except ApiException as ex:
        api_op_ex = api_exceptions.ApiOperationException.from_api_exception(ex)
        if not api_op_ex.not_found:
            raise api_op_ex from ex
        client_ctx.core_api.create_namespace(body=body)


# Initialize progress bars for each level
def init_level_progress(progress: Progress, num_levels: int) -> dict[int, TaskID]:
    level_tasks = {}
    for level_index in range(num_levels):
        task_id = progress.add_task(
            f"[bold yellow]Level {level_index + 1}", total=100, start=False
        )
        level_tasks[level_index] = task_id
    return level_tasks


# Initialize node status indicators within a level
def init_node_status(
    progress: Progress,
    level_tasks: dict[int, TaskID],
    level_index: int,
    nodes: list[deployment_graph.ObjectNode],
) -> dict[K8sObjectIdentifier, TaskID]:
    node_status_tasks = {}
    for node in nodes:
        task_id = progress.add_task(
            f"[dim]{node.kind} [bold cyan]{node.identifier.name}[/] in namespace [bold magenta]{node.identifier.namespace or 'default'}[/]",
            total=1,  # Total is 1 since it's essentially a binary done/not done for each node
            start=False,  # Start as False; it gets started when deployment begins
            visible=False,  # Initially not visible; it becomes visible when deployment to this node begins
            parent=level_tasks[level_index],
        )
        node_status_tasks[node.identifier] = task_id
    return node_status_tasks


def create_deployment_table(
    nodes: list[deployment_graph.ObjectNode],
    level_index: int,
    progress_percentage: float,
    executor: deployment_executor.DeploymentExecutor,
) -> Table:
    """Creates a detailed table for a deployment level."""
    table = Table(title=f"Level {level_index + 1}")
    table.add_column("Status", style="cyan", no_wrap=True)
    table.add_column("Kind", style="magenta")
    table.add_column("Name", style="green")
    table.add_column("Namespace", style="yellow")

    for node in nodes:
        status = (
            "✓" if node.identifier in executor.waited_nodes else "⟲"
        )  # Change as per your status indicators
        namespace = node.deploying_object.k8s_object.namespace or "default"
        table.add_row(status, node.kind, node.identifier.name, namespace)

    # Add the progress bar as the last row
    progress_bar = (
        "[progress]"
        + "▇" * int(progress_percentage / 10)
        + " " * (10 - int(progress_percentage / 10))
        + f" {progress_percentage}%"
    )
    table.add_row("", "", "", "", progress_bar)

    return table


async def update_progress(
    console: Console,
    progress: Progress,
    level_tasks: dict[int, TaskID],
    node_status_tasks: dict[K8sObjectIdentifier, TaskID],
    executor: deployment_executor.DeploymentExecutor,
) -> None:
    graph_traverse = executor.graph.traverse_graph()
    last_completed_level: int | None = None
    total_task_id = progress.add_task(
        "[bold red]Deployment", total=len(graph_traverse), start=False
    )
    while not executor.is_final:
        for level_index, nodes in enumerate(graph_traverse):
            console.print(f"[bold green]Progress Level {level_index}[/]")
            if len(nodes) > 1:
                level_task_id = progress.add_task(
                    f"[bold yellow]Level {level_index + 1}",
                    total=len(nodes),
                    start=False,
                )
            else:
                level_task_id = None
            # print level plan
            console.print("Here level Plan")
            # while level not completed or not executor.is_final:
            for node in nodes:
                console.print(f"Updates of each node, just the changes {node}")

        # Refresh rate; tweak based on actual deployment speed and desired UI update frequency
        await asyncio.sleep(1)

    # After completion, you can print a final status message or summary
    console.print("[bold green]Deployment completed successfully[/]")


async def track_overall_progress(
    progress: Progress,
    deployment_task: TaskID,
    executor: deployment_executor.DeploymentExecutor,
) -> None:
    while not executor.is_done:
        # Update overall deployment progress based on nodes deployed
        completed_steps = sum(len(level) for level in executor.deployed_nodes)
        total_steps = sum(len(level) for level in executor.graph.traverse_graph())
        if total_steps > 0:
            progress.update(
                deployment_task, completed=(completed_steps / total_steps * 100)
            )
        await asyncio.sleep(1)  # Adjust sleep time as needed
    progress.update(deployment_task, completed=100)  # Ensure complete at end


async def track_level_progress(
    progress: Progress,
    executor: deployment_executor.DeploymentExecutor,
    level_tasks: dict[int, TaskID],
) -> None:
    while not executor.is_done:
        for level_index, level_nodes in enumerate(executor.graph.traverse_graph()):
            level_progress = sum(
                1 for node in level_nodes if node.identifier in executor.waited_nodes
            ) / len(level_nodes)
            level_task_id = level_tasks.setdefault(
                level_index,
                progress.add_task(f"[bold yellow]Level {level_index + 1}", total=100),
            )
            progress.update(level_task_id, completed=level_progress * 100)
        await asyncio.sleep(1)
    for task_id in level_tasks.values():
        progress.update(task_id, completed=100)


async def track_node_status(
    progress: Progress,
    executor: deployment_executor.DeploymentExecutor,
    node_status_tasks: dict[K8sObjectIdentifier, TaskID],
) -> None:
    while not executor.is_done:
        for level_nodes in executor.graph.traverse_graph():
            for node in level_nodes:
                node_task_id = node_status_tasks.setdefault(
                    node.identifier,
                    progress.add_task(
                        f"{node.kind} {node.identifier.name}", total=1, visible=False
                    ),
                )
                if node.identifier in executor.waited_nodes:
                    progress.update(node_task_id, completed=1, visible=True)
                else:
                    progress.start_task(node_task_id)
        await asyncio.sleep(1)
    for task_id in node_status_tasks.values():
        progress.update(task_id, completed=1, visible=True)


async def _run_deployment(
    console: Console,
    executor: deployment_executor.DeploymentExecutor,
    namespace: str,
    create_namespace: bool,
) -> None:
    """Run the deployment with live progress updates."""
    with Progress(console=console, transient=True) as progress:
        # deployment_task: TaskID = progress.add_task(
        #     "[cyan]Overall Deployment", total=100
        # )
        # level_tasks = {}
        # node_status_tasks = {}
        total_levels = len(executor.graph.traverse_graph())
        level_tasks = init_level_progress(progress, total_levels)
        node_status_tasks = {}

        try:
            client_ctx = ClientContext()
            await asyncio.gather(
                executor.deploy(client_ctx, namespace),
                executor.wait_for_all(client_ctx, namespace),
                # track_overall_progress(progress, deployment_task, executor),
                # track_level_progress(progress, executor, level_tasks),
                # track_node_status(progress, executor, node_status_tasks),
                update_progress(
                    console, progress, level_tasks, node_status_tasks, executor
                ),
            )
        except Exception as e:
            console.print(
                Panel(f"[bold red]Deployment failed:[/] {str(e)}", expand=False)
            )
            raise
        else:
            console.print(
                Panel("[bold green]Deployment completed successfully[/]", expand=False)
            )


@app.command()
def plan(
    ctx: typer.Context,
    validate: bool = typer.Option(
        False,
        "--validate",
        "-v",
        help="Validate the deployment graph for cycles and errors before showing the plan.",
    ),
) -> None:
    """
    Deployment plan for the kubernetes object model.

    Note: The command options are shared among commands and should be specified at the root level.
    """
    console = Console()
    common.print_command_name(console, "Deployment Plan")
    ctx_obj: "ContextObject" = ctx.obj
    common.print_ctx_options(console, ctx_obj)

    strategy = strategy_auto.StrategyAuto()
    k8s_objects = loader.load_all(
        module_name=ctx_obj.module_name,
        module_path=ctx_obj.module_path,
        folder_path=ctx_obj.folder_path,
        sub_elements=ctx_obj.sub_elements,
    )
    # Create the deployment graph
    deployment_graph = strategy.build_deployment_graph(k8s_objects)

    # Prepare for visual output

    deployment_plan_tree = Tree(
        "[bold green]Kubernetes Deployment Plan", guide_style="bold bright_blue"
    )

    common.print_ctx_options(console, ctx_obj)

    if validate:
        try:
            console.print("[bold blue]Validating deployment graph...[/]")
            deployment_graph.validate()
            console.print("[bold green]Validation successful![/]")
        except ValueError as e:
            console.print(f"[bold red]Validation error: {e}[/]")
            return  # Stop further execution if validation fails

    # Traverse the deployment graph and create the structured output
    for level_index, level in enumerate(deployment_graph.traverse_graph()):
        level_tree = deployment_plan_tree.add(
            f"[bold yellow]Step {level_index + 1}:", guide_style="bold bright_yellow"
        )
        for node in level:
            node_text = f"[dim]{node.kind} [bold cyan]{node.identifier.name}[/] in namespace [bold magenta]{node.identifier.namespace or 'default'}[/]"
            if node.previous_object:
                node_text += f" (after [cyan]{node.previous_object.identifier.name}[/])"
            level_tree.add(node_text)

    # Display the structured deployment plan
    console.print(deployment_plan_tree)


# @app.command()
# def update(
#     namespace: str = typer.Argument(
#         ..., help="The namespace where the resources are deployed."
#     ),
#     resource_files: list[str] = typer.Option(
#         ...,
#         "--resource-file",
#         "-f",
#         help="File paths of the Kubernetes resource definitions for updating.",
#     ),
# ) -> None:
#     """Update existing Kubernetes resources in a specified namespace."""
#     ctx = ClientContext()
#     resources_update = []  # Similarly, load from provided files

#     # Load update resources
#     for file_path in resource_files:
#         with open(file_path) as f:
#             # Assuming the same hypothetical from_file function
#             resource = K8sObject.from_file(f)
#             resources_update.append(resource)

#     # Perform update
#     try:
#         deploy_op.deploy(ctx, resources_update, namespace)
#         typer.echo(f"Successfully updated resources in {namespace}")
#     except ApiException as e:
#         typer.echo(f"Failed to update resources in {namespace}: {str(e)}")
#         raise typer.Exit(code=1) from e


if __name__ == "__main__":
    app()
