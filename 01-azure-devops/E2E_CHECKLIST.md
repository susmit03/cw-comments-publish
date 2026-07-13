# E2E checklist — Azure DevOps comment promotion (C&W)

Use this checklist to walk a C&W engineer through **setup + one full promotion cycle**
(DEV → PR → UAT → tag → PROD).

**Time estimate:** ~2–3 hours first run (mostly permissions and pipeline wiring).

**Who does what**

| Role | Typical owner |
|------|----------------|
| Databricks admin (catalogs, SP, grants) | Platform / UC admin |
| Azure DevOps (pipeline, variables) | DevOps / platform |
| Day-to-day test (extract, PR, verify) | Data engineer |

Fill in before you start:

```
Azure DevOps repo URL:     ___________________________________
Databricks workspace URL:  ___________________________________
SQL warehouse ID:          ___________________________________
DEV catalog:               ___________________________________
UAT catalog:               ___________________________________
PROD catalog:              ___________________________________
Test table (schema.table): ___________________________________   ← pick one table that exists in all 3 catalogs
Service principal name:    ___________________________________
Engineer running test:     ___________________________________
Date:                      ___________________________________
```

---

## Part 0 — Pre-flight

- [ ] Repo is cloned in Azure DevOps and engineer can `git clone` it locally
- [ ] Engineer has **git push** access and can open PRs to `main`
- [ ] Engineer has a Databricks login (for editing comments in DEV)
- [ ] A service principal (or automation identity) exists for CI/DAB jobs
- [ ] One **test table** is chosen that already exists in DEV, UAT, and PROD
      (comment promotion does **not** create tables)

**Pass:** All blanks above are filled in; test table confirmed in all three catalogs.

---

## Part 1 — Databricks prerequisites

### 1.1 Catalogs and schemas

- [ ] DEV catalog exists and engineer can browse it
- [ ] UAT catalog exists
- [ ] PROD catalog exists
- [ ] Test table's schema is listed in `allowed_schemas` (default POC: `sales`, `finance`)

**Verify in Databricks SQL:**

```sql
SHOW TABLES IN <dev_catalog>.<schema>;
SHOW TABLES IN <uat_catalog>.<schema>;
SHOW TABLES IN <prod_catalog>.<schema>;
```

### 1.2 Service principal grants

- [ ] SP has `USE CATALOG` on DEV, UAT, and PROD catalogs
- [ ] SP has `MODIFY` (or equivalent) on the test table in UAT and PROD
- [ ] SP can run queries on the chosen SQL warehouse

**Verify:** Run as the SP (or with its token):

```bash
export DATABRICKS_HOST=https://<workspace>.cloud.databricks.com
export DATABRICKS_TOKEN=<sp-token>
databricks current-user me   # or: curl -H "Authorization: Bearer $DATABRICKS_TOKEN" ...
```

Then in SQL (warehouse):

```sql
SELECT table_catalog, table_schema, table_name, comment
FROM <uat_catalog>.information_schema.tables
WHERE table_schema = '<schema>' AND table_name = '<table>';
```

### 1.3 SQL warehouse

- [ ] Warehouse is **running** (or serverless and reachable)
- [ ] Warehouse ID copied for config and pipeline variables

**Pass:** SP token works; `information_schema` query returns the test table in all three catalogs.

---

## Part 2 — Local setup (engineer's laptop)

Run from a fresh clone:

```bash
git clone <repo-url>
cd <repo>/01-azure-devops
python3 -m venv .venv && source .venv/bin/activate
pip install -e "../shared[dev]"
pip install -e ".[dev]"
cp config.example.yaml config.yaml
```

### 2.1 Edit `config.yaml`

- [ ] `dev_catalog`, `uat_catalog`, `prod_catalog` set to C&W names
- [ ] `warehouse_id` set
- [ ] `workspace_url` set
- [ ] `allowed_schemas` includes the test table's schema

### 2.2 Export environment variables

```bash
export DATABRICKS_HOST=https://<workspace>.cloud.databricks.com
export DATABRICKS_TOKEN=<sp-or-personal-token-for-local-test>
export DATABRICKS_WAREHOUSE_ID=<warehouse-id>
export DEV_CATALOG=<dev_catalog>
export UAT_CATALOG=<uat_catalog>
export PROD_CATALOG=<prod_catalog>
export ALLOWED_SCHEMAS=<schema>   # comma-separated if multiple
```

- [ ] `comments-sync --help` runs without import errors
- [ ] `databricks auth env` / CLI can reach the workspace (install CLI if needed)

**Pass:**

```bash
comments-sync extract --env dev
# Expect: "Wrote N table file(s) to comments"
```

---

## Part 3 — DAB bundle (one-time per workspace)

### 3.1 Update bundle config for C&W

Edit `01-azure-devops/databricks.yml` — replace POC defaults under `variables:`:

- [ ] `dev_catalog`, `uat_catalog`, `prod_catalog`
- [ ] `warehouse_id`
- [ ] `workspace_url`
- [ ] `allowed_schemas`

Commit and push these changes (or confirm they are already on `main`).

### 3.2 Deploy and validate

```bash
cd 01-azure-devops
databricks bundle validate
databricks bundle deploy
```

- [ ] `bundle validate` succeeds
- [ ] `bundle deploy` succeeds
- [ ] Four jobs visible in Databricks **Workflows**:
  - `comments-diff-uat`
  - `comments-promote-uat`
  - `comments-diff-prod`
  - `comments-promote-prod`

### 3.3 Smoke test (diff only, no writes)

```bash
databricks bundle run comments_diff_uat
databricks bundle run comments_diff_prod
```

- [ ] Both jobs complete successfully (exit 0)
- [ ] Job logs show pending change count (may be > 0 on first run — that's OK)

**Pass:** Diff jobs run green; no permission errors in job run output.

---

## Part 4 — Azure DevOps pipeline

Pipeline file: [`../azure-pipelines.yml`](../azure-pipelines.yml)

### 4.1 Create pipeline

- [ ] **Pipelines → New pipeline → Existing YAML** → path `/azure-pipelines.yml`
- [ ] Pipeline saved and named (e.g. `comments-promote`)

### 4.2 Pipeline variables (mark secrets as secret)

| Variable | Set? |
|----------|------|
| `DATABRICKS_HOST` | [ ] |
| `DATABRICKS_TOKEN` | [ ] |
| `DEV_CATALOG` | [ ] |
| `UAT_CATALOG` | [ ] |
| `PROD_CATALOG` | [ ] |
| `DATABRICKS_WAREHOUSE_ID` | [ ] |

### 4.3 Trigger behavior (know before testing)

- [ ] **UAT:** runs on push to `main` when paths under `01-azure-devops/`, `shared/`, or `azure-pipelines.yml` change
- [ ] **PROD:** runs only on tags matching `release/*`
- [ ] PRs to `main` do **not** promote — merge is the UAT gate

**Pass:** Variables saved; engineer understands merge → UAT, tag → PROD.

---

## Part 5 — Baseline comment YAML (optional but recommended)

If `comments/` in the repo doesn't match C&W DEV yet:

```bash
cd 01-azure-devops
comments-sync extract --env dev --clean
git status comments/
```

- [ ] Review diff — only expected tables/schemas appear
- [ ] Commit baseline to a branch and merge to `main` **or** skip if sample YAML is fine for POC

**Pass:** `comments/<schema>/<table>.yaml` exists for the test table.

---

## Part 6 — End-to-end test (the walkthrough)

This is the scripted demo. Use a **harmless, reversible** comment change on the test table.

### 6.1 Make a comment change in DEV

In Databricks (Catalog Explorer or SQL), on the **test table only**:

- [ ] Change **table comment** or **one column comment** to something unique, e.g.  
      `E2E test comment — <engineer> — <date>`

**Verify in DEV:**

```sql
DESCRIBE TABLE EXTENDED <dev_catalog>.<schema>.<table>;
-- or
SELECT comment FROM <dev_catalog>.information_schema.tables
WHERE table_schema = '<schema>' AND table_name = '<table>';
```

### 6.2 Extract → branch → PR

```bash
git checkout main && git pull
git checkout -b e2e/comment-promotion-test
cd 01-azure-devops
comments-sync extract --env dev
git diff comments/
```

- [ ] YAML diff shows **only** the intended comment change(s)
- [ ] No unrelated tables churned (if many files changed, check `allowed_schemas`)

```bash
git add comments/
git commit -m "E2E test: update comment on <schema>.<table>"
git push -u origin e2e/comment-promotion-test
```

- [ ] Open PR to `main` (Azure DevOps UI or `az repos pr create ...`)
- [ ] Reviewer confirms PR diff is readable and matches DEV change
- [ ] **Do not merge yet** — optional: run local preview:

```bash
comments-sync diff --env uat
comments-sync apply --env uat --dry-run
```

- [ ] Local diff/dry-run shows the expected SQL (`COMMENT ON TABLE` / `ALTER COLUMN`)

### 6.3 Merge → UAT promotion

- [ ] Approve and **merge PR to `main`**
- [ ] Azure DevOps pipeline run starts (**Promote comments to UAT** stage)
- [ ] Pipeline stage **succeeds** (green)

**Verify in UAT:**

```sql
SELECT comment FROM <uat_catalog>.information_schema.tables
WHERE table_schema = '<schema>' AND table_name = '<table>';
```

- [ ] UAT table/column comment matches the new text from DEV/YAML

**Optional cross-check:**

```bash
comments-sync diff --env uat
# Expect: "No changes pending for <uat_catalog>"
```

### 6.4 Tag → PROD promotion

```bash
git checkout main && git pull
git tag release/e2e-<date>-<initials>    # must match release/*
git push origin release/e2e-<date>-<initials>
```

- [ ] Pipeline run triggered on **tag** (not branch push)
- [ ] **Promote comments to PROD** stage succeeds

**Verify in PROD:**

```sql
SELECT comment FROM <prod_catalog>.information_schema.tables
WHERE table_schema = '<schema>' AND table_name = '<table>';
```

- [ ] PROD comment matches UAT/DEV/YAML

```bash
comments-sync diff --env prod
# Expect: no pending changes
```

### 6.5 Audit trail check

- [ ] Git history on `main` shows the merge commit with YAML diff
- [ ] Azure DevOps pipeline runs are retained and show successful promote steps
- [ ] Databricks job run history shows `comments-promote-uat` and `comments-promote-prod` runs

**Pass:** Same comment text in DEV (source), YAML in git, UAT, and PROD; pipeline and jobs auditable.

---

## Part 7 — Sign-off

| Criterion | OK? |
|-----------|-----|
| Engineer can extract DEV comments to YAML locally | [ ] |
| PR review shows meaningful YAML diff | [ ] |
| Merge to `main` auto-promotes to UAT via pipeline | [ ] |
| `release/*` tag auto-promotes to PROD via pipeline | [ ] |
| Manual DAB jobs work as fallback (`databricks bundle run ...`) | [ ] |
| No permission or warehouse errors in any step | [ ] |

**Notes / issues:**

```
_________________________________________________________________
_________________________________________________________________
_________________________________________________________________
```

---

## Troubleshooting (quick reference)

| Symptom | Likely cause | Check |
|---------|----------------|-------|
| `Missing required env vars` | Config not loaded | `config.yaml` or exports in Part 2.2 |
| Extract writes 0 files | Wrong catalog or schema filter | `allowed_schemas`, catalog names |
| `MODIFY` / permission denied on apply | SP grants | Part 1.2 — UAT/PROD table ACLs |
| Pipeline skips UAT stage | Push wasn't to `main`, or path filter | Merge PR; touch `01-azure-devops/*` |
| PROD stage skipped | Tag doesn't match `release/*` | `git tag release/...` not `v1.0` |
| Table skipped silently | Table missing in target catalog | Table must exist in UAT/PROD |
| Many unrelated YAML files in diff | Schema too broad | Tighten `allowed_schemas` |

---

## Cleanup after test (optional)

- [ ] Revert test comment in DEV if desired
- [ ] Re-extract, PR, and promote again **or** manually restore previous comment in UAT/PROD
- [ ] Delete test tag `release/e2e-...` if your process requires it (tag cannot be "un-pushed" without force)

---

## Related docs

- [01-azure-devops README](./README.md) — day-to-day commands
- [Repo root README](../README.md) — shared model and auth
- [`azure-pipelines.yml`](../azure-pipelines.yml) — CI definition
