"""CLI entry points for synthetic trace generation."""

from __future__ import annotations

import sys
import json

import click

from .._command import LayerLensGroup
from ...synthetic import SyntheticDataBuilder


@click.group(cls=LayerLensGroup)
def synthetic() -> None:
    """Generate synthetic traces from templates."""


@synthetic.command("templates")
@click.pass_context
def templates(ctx: click.Context) -> None:
    """List available templates."""
    found = SyntheticDataBuilder().list_templates()
    if ctx.obj["output_format"] == "json":
        click.echo(json.dumps([{"id": t.id, "category": t.category.value, "title": t.title} for t in found], indent=2))
        return
    for t in found:
        click.echo(f"{t.id:30s} {t.category.value:15s} {t.title}")


@synthetic.command("generate")
@click.option(
    "--template", "template_id", required=True, help="The template to generate from (`synthetic templates` lists them)."
)
@click.option(
    "--count",
    type=click.IntRange(min=1),
    default=10,
    show_default=True,
    help="How many traces to generate, at least 1; a template's own maximum caps it.",
)
@click.option(
    "--provider", default=None, help="The provider that generates them (default: the template's; built in: stochastic)."
)
@click.option("--project-id", default=None, help="The project id written into each trace (default: synthetic).")
@click.option(
    "--organization-id", default=None, help="The organization id written into each trace (default: synthetic)."
)
@click.option(
    "--out",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    help="Write traces to this JSONL file (default: stdout).",
)
def generate(
    template_id: str,
    count: int,
    provider: str | None,
    project_id: str | None,
    organization_id: str | None,
    out: str | None,
) -> None:
    """Generate N synthetic traces from TEMPLATE."""
    result = SyntheticDataBuilder().generate(
        template_id,
        count,
        provider_id=provider,
        project_id=project_id,
        organization_id=organization_id,
    )
    if result.errors:
        click.echo(f"errors: {result.errors}", err=True)
        sys.exit(1)
    try:
        sink = open(out, "w", encoding="utf-8") if out else sys.stdout
    except OSError as err:
        click.echo(f"Error: the traces could not be written to {out}: {err.strerror or err}", err=True)
        sys.exit(1)
    try:
        for trace in result.traces:
            sink.write(json.dumps(trace.model_dump()) + "\n")
    finally:
        if out:
            sink.close()
    click.echo(
        f"generated {len(result.traces)} traces (job={result.job_id})",
        err=True,
    )
