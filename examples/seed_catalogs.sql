-- ============================================================================
-- seed_catalogs.sql
--
-- Sets up three catalogs (cwc_dev / cwc_uat / cwc_prod) inside a single
-- metastore with two schemas (sales, finance) and a handful of tables. The
-- DEV catalog has rich comments. UAT has partial / stale comments. PROD has
-- almost no comments. This lets every POC demonstrate a real promotion:
--
--   DEV  --(approve)--> UAT  --(approve)--> PROD
--
-- Run this notebook-style script with a SQL warehouse the running principal
-- owns. Adjust the catalog names if `cwc_*` collides with existing catalogs.
--
-- Cleanup at the bottom (commented) drops everything when you're done.
-- ============================================================================

-- ---------------- Catalogs ----------------
CREATE CATALOG IF NOT EXISTS cwc_dev  COMMENT 'Comment-promotion POC: DEV';
CREATE CATALOG IF NOT EXISTS cwc_uat  COMMENT 'Comment-promotion POC: UAT';
CREATE CATALOG IF NOT EXISTS cwc_prod COMMENT 'Comment-promotion POC: PROD';

-- ---------------- Schemas ----------------
CREATE SCHEMA IF NOT EXISTS cwc_dev.sales;
CREATE SCHEMA IF NOT EXISTS cwc_dev.finance;
CREATE SCHEMA IF NOT EXISTS cwc_uat.sales;
CREATE SCHEMA IF NOT EXISTS cwc_uat.finance;
CREATE SCHEMA IF NOT EXISTS cwc_prod.sales;
CREATE SCHEMA IF NOT EXISTS cwc_prod.finance;

-- ============================================================================
-- DEV: the source of truth. Rich, fully-documented.
-- ============================================================================
CREATE TABLE IF NOT EXISTS cwc_dev.sales.orders (
  order_id      BIGINT  COMMENT 'Primary key, monotonically increasing',
  customer_id   BIGINT  COMMENT 'FK to cwc_dev.sales.customers.customer_id',
  order_date    DATE    COMMENT 'Date order was placed in the storefront timezone',
  total_amount  DECIMAL(18,2) COMMENT 'Order total in USD, includes tax but not shipping',
  status        STRING  COMMENT 'One of: pending, paid, shipped, delivered, refunded'
) COMMENT 'Customer orders, one row per order_id. Refreshed every 15 minutes from the OLTP store.';

CREATE TABLE IF NOT EXISTS cwc_dev.sales.customers (
  customer_id BIGINT COMMENT 'Primary key',
  email       STRING COMMENT 'Lowercased canonical email; unique within active rows',
  signup_date DATE   COMMENT 'Date the customer first registered',
  region      STRING COMMENT 'ISO 3166-1 alpha-2 country code'
) COMMENT 'Customer master table. SCD Type 1 (no history retained).';

CREATE TABLE IF NOT EXISTS cwc_dev.finance.gl_entries (
  entry_id    BIGINT        COMMENT 'Surrogate key',
  account     STRING        COMMENT 'GL account code, e.g. 4000-REVENUE',
  amount      DECIMAL(18,4) COMMENT 'Signed amount; debits positive, credits negative',
  posted_at   TIMESTAMP     COMMENT 'When the entry was posted, UTC',
  source_doc  STRING        COMMENT 'Originating document reference'
) COMMENT 'General-ledger journal entries. Append-only, partition by posted_at date.';

-- ============================================================================
-- UAT: same DDL, but comments are stale / partial. Promotion should be a diff.
-- ============================================================================
CREATE TABLE IF NOT EXISTS cwc_uat.sales.orders (
  order_id      BIGINT  COMMENT 'order id',  -- shorter, stale wording
  customer_id   BIGINT,                       -- missing comment
  order_date    DATE    COMMENT 'Order date',
  total_amount  DECIMAL(18,2),
  status        STRING  COMMENT 'order status'
) COMMENT 'Orders table';

CREATE TABLE IF NOT EXISTS cwc_uat.sales.customers (
  customer_id BIGINT COMMENT 'PK',
  email       STRING,
  signup_date DATE,
  region      STRING COMMENT 'country code'
);

CREATE TABLE IF NOT EXISTS cwc_uat.finance.gl_entries (
  entry_id    BIGINT,
  account     STRING COMMENT 'GL account',
  amount      DECIMAL(18,4),
  posted_at   TIMESTAMP,
  source_doc  STRING
);

-- ============================================================================
-- PROD: bare DDL, no comments at all. Worst case.
-- ============================================================================
CREATE TABLE IF NOT EXISTS cwc_prod.sales.orders (
  order_id      BIGINT,
  customer_id   BIGINT,
  order_date    DATE,
  total_amount  DECIMAL(18,2),
  status        STRING
);

CREATE TABLE IF NOT EXISTS cwc_prod.sales.customers (
  customer_id BIGINT,
  email       STRING,
  signup_date DATE,
  region      STRING
);

CREATE TABLE IF NOT EXISTS cwc_prod.finance.gl_entries (
  entry_id    BIGINT,
  account     STRING,
  amount      DECIMAL(18,4),
  posted_at   TIMESTAMP,
  source_doc  STRING
);

-- ============================================================================
-- Sanity check: how many comments do we have in each catalog?
-- ============================================================================
-- SELECT 'dev' AS env, COUNT(*) AS tables_with_comment FROM cwc_dev.information_schema.tables  WHERE comment IS NOT NULL
-- UNION ALL
-- SELECT 'uat', COUNT(*) FROM cwc_uat.information_schema.tables  WHERE comment IS NOT NULL
-- UNION ALL
-- SELECT 'prod', COUNT(*) FROM cwc_prod.information_schema.tables WHERE comment IS NOT NULL;

-- ============================================================================
-- Cleanup (uncomment to tear down)
-- ============================================================================
-- DROP CATALOG IF EXISTS cwc_dev  CASCADE;
-- DROP CATALOG IF EXISTS cwc_uat  CASCADE;
-- DROP CATALOG IF EXISTS cwc_prod CASCADE;
