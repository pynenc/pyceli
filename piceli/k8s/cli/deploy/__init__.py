import typer

from piceli.k8s.cli.deploy import plan, run, detail

app = typer.Typer()

app.command()(plan.plan)
app.command()(run.run)
app.command()(detail.detail)

if __name__ == "__main__":
    app()
