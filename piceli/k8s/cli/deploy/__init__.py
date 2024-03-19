import typer

from piceli.k8s.cli.deploy import plan, run

app = typer.Typer()

app.command()(plan.plan)
app.command()(run.run)

if __name__ == "__main__":
    app()
