"""DAB job entrypoint for comment promotion in 01-azure-devops."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SCRIPT_PATH = Path(globals().get("__file__") or sys.argv[0]).resolve()
SHARED_DIR = _SCRIPT_PATH.parent.parent / "shared"
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))

from databricks.sdk import WorkspaceClient

from comments_core import (
    CatalogConfig,
    compute_changes,
    execute,
    extract_comments,
    generate_sql,
    read_comment_set,
)


def _load_config() -> CatalogConfig:
    local_cfg = _SCRIPT_PATH.with_name("config.yaml")
    if local_cfg.exists():
        return CatalogConfig.from_file(local_cfg)
    return CatalogConfig.from_env()


def _config_from_args(args: argparse.Namespace) -> CatalogConfig | None:
    required = [args.dev_catalog, args.uat_catalog, args.prod_catalog, args.warehouse_id]
    if not all(required):
        return None
    allowed = [s.strip() for s in (args.allowed_schemas or "").split(",") if s.strip()]
    return CatalogConfig(
        dev_catalog=args.dev_catalog,
        uat_catalog=args.uat_catalog,
        prod_catalog=args.prod_catalog,
        warehouse_id=args.warehouse_id,
        workspace_url=args.workspace_url,
        allowed_schemas=allowed,
    )


def _default_source_dir() -> Path:
    return _SCRIPT_PATH.with_name("comments")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run diff/apply promotion for Git PR comment workflow."
    )
    parser.add_argument("--env", choices=["uat", "prod"], required=True)
    parser.add_argument(
        "--source-dir",
        default=str(_default_source_dir()),
        help="Directory containing comments YAML (default: 01-azure-devops/comments)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="If set, execute statements. If omitted, run diff only.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Render SQL without executing (implies --apply behavior path).",
    )
    parser.add_argument(
        "--stop-on-error",
        action="store_true",
        help="Abort on first failing statement.",
    )
    parser.add_argument("--dev-catalog", default=None)
    parser.add_argument("--uat-catalog", default=None)
    parser.add_argument("--prod-catalog", default=None)
    parser.add_argument("--warehouse-id", default=None)
    parser.add_argument("--workspace-url", default=None)
    parser.add_argument("--allowed-schemas", default="")
    args = parser.parse_args()

    cfg = _config_from_args(args) or _load_config()
    source_dir = Path(args.source_dir)
    if not source_dir.exists():
        print(f"Source dir does not exist: {source_dir}", file=sys.stderr)
        return 2

    client = WorkspaceClient()
    target_catalog = cfg.catalog_for(args.env)

    desired = read_comment_set(source_dir, catalog="<from-yaml>")
    current = extract_comments(
        client,
        cfg.warehouse_id,
        target_catalog,
        schema_filter=cfg.allowed_schemas or None,
    )
    changes = compute_changes(desired, current)
    print(f"[{args.env}] {len(changes)} pending change(s) against {target_catalog}")

    if not args.apply and not args.dry_run:
        return 0

    if not changes:
        print("No changes to apply.")
        return 0

    statements = generate_sql(changes, target_catalog)
    if args.dry_run:
        print("Dry run SQL:")
        for stmt in statements:
            print(f"  {stmt}")
        return 0

    results = execute(
        client,
        cfg.warehouse_id,
        statements,
        dry_run=False,
        stop_on_error=args.stop_on_error,
    )
    failed = [r for r in results if r.status == "ERROR"]
    ok = len(results) - len(failed)
    print(f"Executed {len(results)} statement(s): {ok} OK, {len(failed)} failed")
    for item in failed:
        print(f"FAIL: {item.sql}\n  {item.error}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

