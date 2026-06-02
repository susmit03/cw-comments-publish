"""`comments-sync` CLI — extract / diff / apply.

The shape:

    comments-sync extract --env dev               # snapshot cwc_dev to ./comments/
    comments-sync diff    --env uat               # show pending changes for UAT
    comments-sync apply   --env uat               # apply ./comments/ to cwc_uat
    comments-sync apply   --env prod --dry-run    # render SQL, don't run
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.table import Table

from comments_core import (
    CatalogConfig,
    compute_changes,
    execute,
    extract_comments,
    generate_sql,
    read_comment_set,
    write_comment_set,
)
from databricks.sdk import WorkspaceClient


console = Console()


def _load_config(config_path: Optional[str]) -> CatalogConfig:
    if config_path:
        return CatalogConfig.from_file(config_path)
    candidate = Path("config.yaml")
    if candidate.exists():
        return CatalogConfig.from_file(candidate)
    return CatalogConfig.from_env()


def _comments_dir() -> Path:
    return Path(os.environ.get("COMMENTS_DIR", "comments"))


def _print_changes(changes, target_catalog: str) -> None:
    if not changes:
        console.print(f"[green]No changes pending for [bold]{target_catalog}[/bold].[/green]")
        return
    table = Table(title=f"Pending changes for {target_catalog}", show_lines=False)
    table.add_column("Kind")
    table.add_column("Object")
    table.add_column("Type")
    table.add_column("New value", overflow="fold")
    for change in changes:
        target = f"{change.table.schema}.{change.table.table}"
        if change.kind == "column":
            target = f"{target}.{change.column}"
        new_value = change.new_value or "[dim]<unset>[/dim]"
        table.add_row(change.kind, target, change.change_type, new_value)
    console.print(table)


@click.group()
@click.option(
    "--config",
    "config_path",
    type=click.Path(dir_okay=False),
    help="Path to config.yaml (defaults to ./config.yaml or env vars).",
)
@click.pass_context
def main(ctx: click.Context, config_path: Optional[str]) -> None:
    """Promote UC comments DEV -> UAT -> PROD via YAML + git."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path


def _config_from_ctx(ctx: click.Context) -> CatalogConfig:
    return _load_config(ctx.obj.get("config_path"))


@main.command()
@click.option(
    "--env",
    type=click.Choice(["dev", "uat", "prod"], case_sensitive=False),
    default="dev",
    show_default=True,
    help="Which catalog to snapshot.",
)
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False),
    default=None,
    help="Output directory (defaults to ./comments).",
)
@click.option(
    "--clean",
    is_flag=True,
    help="Wipe the output directory before writing.",
)
@click.pass_context
def extract(ctx: click.Context, env: str, out_dir: Optional[str], clean: bool) -> None:
    """Snapshot a catalog's comments to YAML files."""
    cfg = _config_from_ctx(ctx)
    target_dir = Path(out_dir) if out_dir else _comments_dir()
    catalog = cfg.catalog_for(env)
    client = WorkspaceClient()

    console.print(f"[bold]Extracting[/bold] comments from [cyan]{catalog}[/cyan] ...")
    comment_set = extract_comments(
        client,
        cfg.warehouse_id,
        catalog,
        schema_filter=cfg.allowed_schemas or None,
    )

    if clean and target_dir.exists():
        shutil.rmtree(target_dir)
    written = write_comment_set(comment_set, target_dir)
    console.print(f"[green]Wrote {len(written)} table file(s) to {target_dir}[/green]")


@main.command()
@click.option(
    "--env",
    type=click.Choice(["uat", "prod"], case_sensitive=False),
    required=True,
    help="Which environment to diff against.",
)
@click.option(
    "--source",
    "source_dir",
    type=click.Path(file_okay=False, exists=True),
    default=None,
    help="Directory of YAML files to compare against (defaults to ./comments).",
)
@click.pass_context
def diff(ctx: click.Context, env: str, source_dir: Optional[str]) -> None:
    """Show what would change in <env> if we applied ./comments/ right now."""
    cfg = _config_from_ctx(ctx)
    src_dir = Path(source_dir) if source_dir else _comments_dir()
    target_catalog = cfg.catalog_for(env)
    client = WorkspaceClient()

    desired = read_comment_set(src_dir, catalog="<from-yaml>")
    current = extract_comments(
        client,
        cfg.warehouse_id,
        target_catalog,
        schema_filter=cfg.allowed_schemas or None,
    )
    changes = compute_changes(desired, current)
    _print_changes(changes, target_catalog)


@main.command()
@click.option(
    "--env",
    type=click.Choice(["uat", "prod"], case_sensitive=False),
    required=True,
    help="Which environment to apply to.",
)
@click.option(
    "--source",
    "source_dir",
    type=click.Path(file_okay=False, exists=True),
    default=None,
    help="Directory of YAML files to apply (defaults to ./comments).",
)
@click.option("--dry-run", is_flag=True, help="Print SQL, don't run.")
@click.option(
    "--stop-on-error",
    is_flag=True,
    help="Abort after the first failure (default: continue and report all failures).",
)
@click.pass_context
def apply(
    ctx: click.Context,
    env: str,
    source_dir: Optional[str],
    dry_run: bool,
    stop_on_error: bool,
) -> None:
    """Apply ./comments/ to the chosen environment."""
    cfg = _config_from_ctx(ctx)
    src_dir = Path(source_dir) if source_dir else _comments_dir()
    target_catalog = cfg.catalog_for(env)
    client = WorkspaceClient()

    desired = read_comment_set(src_dir, catalog="<from-yaml>")
    current = extract_comments(
        client,
        cfg.warehouse_id,
        target_catalog,
        schema_filter=cfg.allowed_schemas or None,
    )
    changes = compute_changes(desired, current)
    if not changes:
        console.print(f"[green]No changes to apply to {target_catalog}.[/green]")
        return

    statements = generate_sql(changes, target_catalog)
    _print_changes(changes, target_catalog)

    if dry_run:
        console.print("\n[bold yellow]Dry run — SQL that would be executed:[/bold yellow]")
        for stmt in statements:
            console.print(f"  {stmt}")
        return

    results = execute(
        client,
        cfg.warehouse_id,
        statements,
        stop_on_error=stop_on_error,
    )

    ok = sum(1 for r in results if r.status == "OK")
    failed = [r for r in results if r.status == "ERROR"]
    console.print(
        f"\n[bold]{ok}/{len(results)} statements OK[/bold]"
        + (f"  [red]{len(failed)} failed[/red]" if failed else "")
    )
    for r in failed:
        console.print(f"  [red]FAIL[/red] {r.sql}\n        {r.error}")

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
