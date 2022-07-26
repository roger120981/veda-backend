"""
Custom resource lambda handler to bootstrap Postgres db.
Source: https://github.com/developmentseed/eoAPI/blob/master/deployment/handlers/db_handler.py
"""
import json

import boto3
import psycopg
import requests
from psycopg import sql
from psycopg.conninfo import make_conninfo
from pypgstac.db import PgstacDB
from pypgstac.migrate import Migrate


def send(
    event,
    context,
    responseStatus,
    responseData,
    physicalResourceId=None,
    noEcho=False,
):
    """
    Copyright 2016 Amazon Web Services, Inc. or its affiliates. All Rights Reserved.
    This file is licensed to you under the AWS Customer Agreement (the "License").
    You may not use this file except in compliance with the License.
    A copy of the License is located at http://aws.amazon.com/agreement/ .
    This file is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, express or implied.
    See the License for the specific language governing permissions and limitations under the License.

    Send response from AWS Lambda.

    Note: The cfnresponse module is available only when you use the ZipFile property to write your source code.
    It isn't available for source code that's stored in Amazon S3 buckets.
    For code in buckets, you must write your own functions to send responses.
    """
    responseUrl = event["ResponseURL"]

    print(responseUrl)

    responseBody = {}
    responseBody["Status"] = responseStatus
    responseBody["Reason"] = (
        "See the details in CloudWatch Log Stream: " + context.log_stream_name
    )
    responseBody["PhysicalResourceId"] = physicalResourceId or context.log_stream_name
    responseBody["StackId"] = event["StackId"]
    responseBody["RequestId"] = event["RequestId"]
    responseBody["LogicalResourceId"] = event["LogicalResourceId"]
    responseBody["NoEcho"] = noEcho
    responseBody["Data"] = responseData

    json_responseBody = json.dumps(responseBody)

    print("Response body:\n" + json_responseBody)

    headers = {"content-type": "", "content-length": str(len(json_responseBody))}

    try:
        response = requests.put(responseUrl, data=json_responseBody, headers=headers)
        print("Status code: " + response.reason)
    except Exception as e:
        print("send(..) failed executing requests.put(..): " + str(e))


def get_secret(secret_name):
    """Get Secrets from secret manager."""
    print(f"Fetching {secret_name}")
    client = boto3.client(
        service_name="secretsmanager",
    )
    response = client.get_secret_value(SecretId=secret_name)
    return json.loads(response["SecretString"])


def create_db(cursor, db_name: str) -> None:
    """Create DB."""
    cursor.execute(
        sql.SQL("SELECT 1 FROM pg_catalog.pg_database " "WHERE datname = %s"), [db_name]
    )
    if cursor.fetchone():
        print(f"database {db_name} exists, not creating DB")
    else:
        print(f"database {db_name} not found, creating...")
        cursor.execute(
            sql.SQL("CREATE DATABASE {db_name}").format(db_name=sql.Identifier(db_name))
        )


def create_user(cursor, username: str, password: str) -> None:
    """Create User."""
    cursor.execute(
        sql.SQL(
            "DO $$ "
            "BEGIN "
            "  IF NOT EXISTS ( "
            "       SELECT 1 FROM pg_roles "
            "       WHERE rolname = {user}) "
            "  THEN "
            "    CREATE USER {username} "
            "    WITH PASSWORD {password}; "
            "  ELSE "
            "    ALTER USER {username} "
            "    WITH PASSWORD {password}; "
            "  END IF; "
            "END "
            "$$; "
        ).format(username=sql.Identifier(username), password=password, user=username)
    )


def create_permissions(cursor, db_name: str, username: str) -> None:
    """Add permissions."""
    cursor.execute(
        sql.SQL(
            "GRANT CONNECT ON DATABASE {db_name} TO {username};"
            "GRANT CREATE ON DATABASE {db_name} TO {username};"  # Allow schema creation
            "GRANT USAGE ON SCHEMA public TO {username};"
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            "GRANT ALL PRIVILEGES ON TABLES TO {username};"
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            "GRANT ALL PRIVILEGES ON SEQUENCES TO {username};"
            "GRANT pgstac_read TO {username};"
            "GRANT pgstac_ingest TO {username};"
            "GRANT pgstac_admin TO {username};"
        ).format(
            db_name=sql.Identifier(db_name),
            username=sql.Identifier(username),
        )
    )


def register_extensions(cursor) -> None:
    """Add PostGIS extension."""
    cursor.execute(sql.SQL("CREATE EXTENSION IF NOT EXISTS postgis;"))


def create_dashboard_schema(cursor, username: str) -> None:
    """Create custom schema for dashboard-specific functions."""
    cursor.execute(
        sql.SQL(
            "CREATE SCHEMA IF NOT EXISTS dashboard;"
            "GRANT ALL ON SCHEMA dashboard TO {username};"
            "ALTER ROLE {username} SET SEARCH_PATH TO dashboard, pgstac, public;"
        ).format(username=sql.Identifier(username))
    )


def create_update_default_summaries_function(cursor) -> None:
    """Create function to update collection summary metadata about default item assets."""

    update_default_summary_sql = """
    CREATE OR REPLACE FUNCTION dashboard.update_default_summaries(_collection_id text) RETURNS VOID AS $$
    WITH coll_item_cte AS (
        SELECT jsonb_build_object(
            'summaries',
            jsonb_build_object(
                'datetime', (
                    CASE
                    WHEN (collections."content"->>'dashboard:is_periodic')::boolean
                    THEN (to_jsonb(array[
                        to_char(min(datetime) at time zone 'Z', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
                        to_char(max(datetime) at time zone 'Z', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')]))
                    ELSE jsonb_agg(distinct to_char(datetime at time zone 'Z', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'))
                    END
                ),
                'cog_default', (
                    CASE
                    WHEN collections."content"->'item_assets' ? 'cog_default'
                    THEN jsonb_build_object(
                        'min', min((items."content"->'assets'->'cog_default'->'raster:bands'-> 0 ->'statistics'->>'minimum')::float),
                        'max', max((items."content"->'assets'->'cog_default'->'raster:bands'-> 0 ->'statistics'->>'maximum')::float)
                        )
                    ELSE NULL
                    END
                )
            )
        ) summaries,
        collections.id coll_id
        FROM items
        JOIN collections on items.collection = collections.id
        WHERE collections.id = _collection_id
        GROUP BY collections."content" , collections.id
    )
    UPDATE collections SET "content" = "content" || coll_item_cte.summaries
    FROM coll_item_cte
    WHERE collections.id = coll_item_cte.coll_id;
    $$ LANGUAGE SQL SET SEARCH_PATH TO dashboard, pgstac, public;
    """

    cursor.execute(sql.SQL(update_default_summary_sql))


def create_update_all_default_summaries_function(cursor) -> None:
    """Create function to update default summaries for all collections with required properties"""

    update_all_default_summaries_sql = """
    CREATE OR REPLACE FUNCTION dashboard.update_all_default_summaries() RETURNS VOID AS $$
    SELECT
        update_default_summaries(id)
    FROM collections
    WHERE collections."content" ?| array['item_assets', 'dashboard:is_periodic'];
    $$ LANGUAGE SQL SET SEARCH_PATH TO dashboard, pgstac, public;
    """

    cursor.execute(sql.SQL(update_all_default_summaries_sql))


def handler(event, context):
    """Lambda Handler."""
    print(f"Handling {event}")

    if event["RequestType"] not in ["Create", "Update"]:
        return send(event, context, "SUCCESS", {"msg": "No action to be taken"})

    try:
        params = event["ResourceProperties"]
        connection_params = get_secret(params["conn_secret_arn"])
        user_params = get_secret(params["new_user_secret_arn"])

        print("Connecting to admin DB...")
        admin_db_conninfo = make_conninfo(
            dbname=connection_params.get("dbname", "postgres"),
            user=connection_params["username"],
            password=connection_params["password"],
            host=connection_params["host"],
            port=connection_params["port"],
        )
        with psycopg.connect(admin_db_conninfo, autocommit=True) as conn:
            with conn.cursor() as cur:
                print("Creating database...")
                create_db(
                    cursor=cur,
                    db_name=user_params["dbname"],
                )

                print("Creating user...")
                create_user(
                    cursor=cur,
                    username=user_params["username"],
                    password=user_params["password"],
                )

        # Install extensions on the user DB with
        # superuser permissions, since they will
        # otherwise fail to install when run as
        # the non-superuser within the pgstac
        # migrations.
        print("Connecting to STAC DB...")
        stac_db_conninfo = make_conninfo(
            dbname=user_params["dbname"],
            user=connection_params["username"],
            password=connection_params["password"],
            host=connection_params["host"],
            port=connection_params["port"],
        )
        with psycopg.connect(stac_db_conninfo, autocommit=True) as conn:
            with conn.cursor() as cur:
                print("Registering PostGIS ...")
                register_extensions(cursor=cur)

        stac_db_admin_dsn = (
            "postgresql://{user}:{password}@{host}:{port}/{dbname}".format(
                dbname=user_params.get("dbname", "postgres"),
                user=connection_params["username"],
                password=connection_params["password"],
                host=connection_params["host"],
                port=connection_params["port"],
            )
        )

        pgdb = PgstacDB(dsn=stac_db_admin_dsn, debug=True)
        print(f"Current {pgdb.version=}")

        # As admin, run migrations
        print("Running migrations...")
        Migrate(pgdb).run_migration(params["pgstac_version"])

        # Assign appropriate permissions to user (requires pgSTAC migrations to have run)
        with psycopg.connect(admin_db_conninfo, autocommit=True) as conn:
            with conn.cursor() as cur:
                print("Setting permissions...")
                create_permissions(
                    cursor=cur,
                    db_name=user_params["dbname"],
                    username=user_params["username"],
                )

        print("Adding mosaic index...")
        with psycopg.connect(
            stac_db_admin_dsn,
            autocommit=True,
            options="-c search_path=pgstac,public -c application_name=pgstac",
        ) as conn:
            conn.execute(
                sql.SQL(
                    "CREATE INDEX IF NOT EXISTS searches_mosaic ON searches ((true)) WHERE metadata->>'type'='mosaic';"
                )
            )

        # As admin, create custom dashboard schema and functions and grant privileges to bootstrapped user
        with psycopg.connect(stac_db_conninfo, autocommit=True) as conn:
            with conn.cursor() as cur:
                print("Creating dashboard schema...")
                create_dashboard_schema(cursor=cur, username=user_params["username"])

                print("Creating update_default_summaries functions...")
                create_update_default_summaries_function(cursor=cur)
                create_update_all_default_summaries_function(cursor=cur)

    except Exception as e:
        print(f"Unable to bootstrap database with exception={e}")
        return send(event, context, "FAILED", {"message": str(e)})

    print("Complete.")
    return send(event, context, "SUCCESS", {})
