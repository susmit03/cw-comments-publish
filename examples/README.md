# Examples

## `seed_catalogs.sql`

Creates three catalogs (`cwc_dev`, `cwc_uat`, `cwc_prod`) with two schemas
(`sales`, `finance`) and three tables. Comments are intentionally drifted:

| Catalog    | Comments state                                                |
|------------|---------------------------------------------------------------|
| `cwc_dev`  | Rich, fully documented — the source of truth                  |
| `cwc_uat`  | Partial / stale comments — promotion should produce a diff     |
| `cwc_prod` | No comments at all — first promotion is all adds              |

### Run

In a Databricks SQL editor or notebook tied to a Serverless SQL warehouse:

```sql
%sql
$INCLUDE seed_catalogs.sql
```

Or via the CLI:

```bash
databricks sql query --warehouse-id "$DATABRICKS_WAREHOUSE_ID" \
  --statement "$(cat examples/seed_catalogs.sql)"
```

(Run statements individually if your SQL client doesn't support batching.)

### After running

Update your `config.yaml` (in whichever POC folder you're using) to point at:

```yaml
dev_catalog: cwc_dev
uat_catalog: cwc_uat
prod_catalog: cwc_prod
warehouse_id: <your-warehouse-id>
```

### Tear down

Uncomment the `DROP CATALOG ... CASCADE` lines at the bottom of `seed_catalogs.sql`.
