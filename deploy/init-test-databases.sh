#!/bin/sh
set -eu
psql --username "$POSTGRES_USER" --dbname postgres --set ON_ERROR_STOP=1 \
  --set app_password="$TEST_APP_DB_PASSWORD" --set owner_password="$TEST_COMMERCE_OWNER_PASSWORD" \
  --set reader_password="$TEST_COMMERCE_READER_PASSWORD" <<'SQL'
CREATE ROLE test_operations_app LOGIN PASSWORD :'app_password';
CREATE ROLE test_commerce_owner LOGIN PASSWORD :'owner_password';
CREATE ROLE test_commerce_reader LOGIN PASSWORD :'reader_password';
CREATE DATABASE operations_test OWNER test_operations_app;
CREATE DATABASE commerce_test OWNER test_commerce_owner;
REVOKE CONNECT ON DATABASE operations_test FROM PUBLIC;
GRANT CONNECT ON DATABASE operations_test TO test_operations_app;
REVOKE CONNECT ON DATABASE commerce_test FROM PUBLIC;
GRANT CONNECT ON DATABASE commerce_test TO test_commerce_owner, test_commerce_reader;
ALTER ROLE test_commerce_reader SET default_transaction_read_only = on;
\connect operations_test
CREATE EXTENSION IF NOT EXISTS vector;
\connect commerce_test
GRANT USAGE ON SCHEMA public TO test_commerce_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE test_commerce_owner IN SCHEMA public GRANT SELECT ON TABLES TO test_commerce_reader;
SQL
