from typing import TYPE_CHECKING

import typer
from rich.console import Console
from rich.tree import Tree

from piceli.k8s.cli import common
from piceli.k8s.ops import loader
from piceli.k8s.ops.deploy import strategy_auto

if TYPE_CHECKING:
    from piceli.k8s.cli.context import ContextObject


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
    dep_graph = strategy.build_deployment_graph(k8s_objects)
    deployment_plan_tree = Tree(
        "[bold green]Kubernetes Deployment Plan", guide_style="bold bright_blue"
    )
    common.print_ctx_options(console, ctx_obj)
    if validate:
        try:
            console.print("[bold blue]Validating deployment graph...[/]")
            dep_graph.validate()
            console.print("[bold green]Validation successful![/]")
        except ValueError as e:
            console.print(f"[bold red]Validation error: {e}[/]")
            return  # Stop further execution if validation fails

    # Traverse the deployment graph and create the structured output
    for level_index, level in enumerate(dep_graph.traverse_graph()):
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
