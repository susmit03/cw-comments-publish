"""Config loading for the per-env catalog mapping and warehouse choice."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


@dataclass
class CatalogConfig:
    """Maps logical env names (dev/uat/prod) to physical catalog names.

    Built from a YAML file or env vars. All four POCs read the same shape.
    """

    dev_catalog: str
    uat_catalog: str
    prod_catalog: str
    warehouse_id: str
    workspace_url: Optional[str] = None
    allowed_schemas: list[str] = field(default_factory=list)

    @classmethod
    def from_file(cls, path: str | Path) -> "CatalogConfig":
        data = yaml.safe_load(Path(path).read_text())
        if not isinstance(data, dict):
            raise ValueError(f"Config at {path} must be a YAML mapping")
        return cls(
            dev_catalog=data["dev_catalog"],
            uat_catalog=data["uat_catalog"],
            prod_catalog=data["prod_catalog"],
            warehouse_id=data["warehouse_id"],
            workspace_url=data.get("workspace_url"),
            allowed_schemas=data.get("allowed_schemas", []) or [],
        )

    @classmethod
    def from_env(cls) -> "CatalogConfig":
        required = ("DEV_CATALOG", "UAT_CATALOG", "PROD_CATALOG", "DATABRICKS_WAREHOUSE_ID")
        missing = [k for k in required if not os.environ.get(k)]
        if missing:
            raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")
        allowed_raw = os.environ.get("ALLOWED_SCHEMAS", "")
        allowed = [s.strip() for s in allowed_raw.split(",") if s.strip()]
        return cls(
            dev_catalog=os.environ["DEV_CATALOG"],
            uat_catalog=os.environ["UAT_CATALOG"],
            prod_catalog=os.environ["PROD_CATALOG"],
            warehouse_id=os.environ["DATABRICKS_WAREHOUSE_ID"],
            workspace_url=os.environ.get("DATABRICKS_HOST"),
            allowed_schemas=allowed,
        )

    def catalog_for(self, env: str) -> str:
        env_key = env.lower().strip()
        mapping = {
            "dev": self.dev_catalog,
            "uat": self.uat_catalog,
            "prod": self.prod_catalog,
        }
        if env_key not in mapping:
            raise ValueError(f"Unknown env {env!r}; expected one of {list(mapping)}")
        return mapping[env_key]
