#!/bin/sh
set -eu
psql --username "$POSTGRES_USER" --dbname postgres --set ON_ERROR_STOP=1 \
  --set app_password="$APP_DB_PASSWORD" --set owner_password="$COMMERCE_OWNER_PASSWORD" \
  --set reader_password="$COMMERCE_READER_PASSWORD" <<'SQL'
CREATE ROLE operations_app LOGIN PASSWORD :'app_password';
CREATE ROLE commerce_owner LOGIN PASSWORD :'owner_password';
CREATE ROLE commerce_reader LOGIN PASSWORD :'reader_password';
CREATE DATABASE operations OWNER operations_app;
CREATE DATABASE commerce OWNER commerce_owner;
REVOKE CONNECT ON DATABASE operations FROM PUBLIC;
GRANT CONNECT ON DATABASE operations TO operations_app;
REVOKE CONNECT ON DATABASE commerce FROM PUBLIC;
GRANT CONNECT ON DATABASE commerce TO commerce_owner, commerce_reader;
ALTER ROLE commerce_reader SET default_transaction_read_only = on;
\connect operations
CREATE EXTENSION IF NOT EXISTS vector;
\connect commerce
GRANT USAGE ON SCHEMA public TO commerce_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE commerce_owner IN SCHEMA public GRANT SELECT ON TABLES TO commerce_reader;
SQL
