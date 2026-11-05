"""Read-only export of visit / prescription / prescription_item data from the
Supabase Postgres database to CSV files.

Usage (PowerShell):
    $env:VITA_DB_URL = "postgresql://user:password@host:6543/postgres"
    python scripts/export_visit_prescription_data.py [--out db_export] [--limit 0]

The connection string is read from the VITA_DB_URL environment variable so the
password is never written into the repo. A literal '#' (or '@', '/') in the
password is fine: the URL is split by hand rather than by urllib, which would
treat '#' as the start of a fragment and cut the password short.

Columns requested per table are checked against information_schema first; any
that do not exist are reported and skipped instead of failing the whole run.
prescriptionitem has no column list given, so all its columns are exported.
"""
import argparse
import csv
import os
import sys
from pathlib import Path

import psycopg
from psycopg import sql

# table -> requested columns (None = every column in the table)
TABLES = {
    "visit": ["id", "patient_id", "chief_complaint", "vitals_json"],
    "prescription": ["id", "provisional_diagnosis", "confirmed_diagnosis"],
    "prescriptionitem": None,  # actual table name has no underscore
}

# Common misspelling in the request ("cheif"), mapped to the real column name.
ALIASES = {"cheif_complaint": "chief_complaint"}


def parse_db_url(url):
    """Split postgresql://user:password@host:port/db without urllib so that
    unescaped special characters in the password survive."""
    if "://" not in url:
        raise ValueError("VITA_DB_URL must look like postgresql://user:password@host:port/db")
    rest = url.split("://", 1)[1]
    creds, hostpart = rest.rsplit("@", 1)  # last '@' separates credentials from host
    user, password = creds.split(":", 1)
    hostport, _, dbname = hostpart.partition("/")
    host, _, port = hostport.partition(":")
    return {
        "host": host,
        "port": int(port) if port else 5432,
        "dbname": dbname.split("?")[0] or "postgres",
        "user": user,
        "password": password,
    }


def existing_columns(cur, table):
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position",
        (table,),
    )
    return [r[0] for r in cur.fetchall()]


def export_table(cur, table, wanted, out_dir, limit):
    available = existing_columns(cur, table)
    if not available:
        print(f"[{table}] table not found in schema 'public' - skipped")
        return

    if wanted is None:
        columns = available
    else:
        wanted = [ALIASES.get(c, c) for c in wanted]
        columns = [c for c in wanted if c in available]
        missing = [c for c in wanted if c not in available]
        if missing:
            print(f"[{table}] columns not found, skipped: {', '.join(missing)}")
            print(f"[{table}] columns that do exist: {', '.join(available)}")

    query = sql.SQL("SELECT {cols} FROM {tbl}").format(
        cols=sql.SQL(", ").join(sql.Identifier(c) for c in columns),
        tbl=sql.Identifier(table),
    )
    if limit:
        query += sql.SQL(" LIMIT {}").format(sql.Literal(limit))

    cur.execute(query)
    rows = cur.fetchall()

    path = out_dir / f"{table}.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        writer.writerows(rows)

    print(f"[{table}] {len(rows)} rows, columns: {', '.join(columns)} -> {path}")
    for row in rows[:3]:
        print("    ", dict(zip(columns, row)))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", default="db_export", help="output directory for CSV files")
    parser.add_argument("--limit", type=int, default=0, help="max rows per table (0 = all)")
    args = parser.parse_args()

    url = os.environ.get("VITA_DB_URL")
    if not url:
        sys.exit("Set the VITA_DB_URL environment variable to the Postgres connection string.")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    with psycopg.connect(**parse_db_url(url), connect_timeout=15) as conn:
        conn.read_only = True  # guarantees this script cannot modify data
        with conn.cursor() as cur:
            for table, wanted in TABLES.items():
                export_table(cur, table, wanted, out_dir, args.limit)


if __name__ == "__main__":
    main()
