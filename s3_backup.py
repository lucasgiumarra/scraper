"""
s3_backup.py — Export SQLite tables to S3 as NDJSON.

Dumps price_history and products to S3 using date-partitioned paths:

    s3://<bucket>/price_history/year=YYYY/month=MM/day=DD/data.ndjson
    s3://<bucket>/products/year=YYYY/month=MM/day=DD/data.ndjson

Partitioned this way so AWS Athena can query the files directly with:

    CREATE EXTERNAL TABLE price_history (...)
    PARTITIONED BY (year STRING, month STRING, day STRING)
    ROW FORMAT SERDE 'org.openx.data.jsonserde.JsonSerDe'
    LOCATION 's3://<bucket>/price_history/';

Required .env vars:
    S3_BUCKET
    AWS_ACCESS_KEY_ID
    AWS_SECRET_ACCESS_KEY
    AWS_REGION

Usage:
    python s3_backup.py              # back up both tables for today
    python s3_backup.py --table price_history
    python s3_backup.py --table products
    python s3_backup.py --date 2026-05-19   # re-upload a specific date
    python s3_backup.py --dry-run           # print what would be uploaded, skip S3
"""

import argparse
import io
import json
import os
import sqlite3
from datetime import datetime

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv

load_dotenv()

DB = os.getenv("DB")
BUCKET = os.getenv("S3_BUCKET", "")
TABLES = ("price_history", "products")


# ── Core helpers ──────────────────────────────────────────────────────────────

def _s3_client():
    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION", "us-east-1")
    return boto3.client(
        "s3",
        region_name=region,
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
    )


def _s3_key(table: str, date_str: str) -> str:
    """Hive-style partition path so Athena can auto-discover partitions."""
    y, m, d = date_str.split("-")
    return f"{table}/year={y}/month={m}/day={d}/data.ndjson"


def _dump_table(table: str, date_str: str) -> tuple[bytes, int]:
    """
    Export all rows scraped on date_str from the given table.
    price_history uses scrape_date; products uses last_updated.
    Returns (ndjson_bytes, row_count).
    """
    date_col = "scrape_date" if table == "price_history" else "last_updated"
    with sqlite3.connect(DB) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT * FROM {table} WHERE {date_col} = ?", (date_str,)
        ).fetchall()

    buf = io.BytesIO()
    for row in rows:
        buf.write(json.dumps(dict(row), ensure_ascii=False).encode())
        buf.write(b"\n")
    return buf.getvalue(), len(rows)


def backup_table(table: str, date_str: str, dry_run: bool = False) -> int:
    """Upload one table's data for date_str to S3. Returns row count."""
    if not BUCKET:
        raise EnvironmentError("S3_BUCKET is not set in .env")

    data, count = _dump_table(table, date_str)
    key = _s3_key(table, date_str)
    size_kb = len(data) / 1024

    if dry_run:
        print(f"  [dry-run] would upload s3://{BUCKET}/{key}  ({count} rows, {size_kb:.1f} KB)")
        return count

    if count == 0:
        print(f"  [{table}] no rows for {date_str}, skipping upload")
        return 0

    s3 = _s3_client()
    s3.put_object(
        Bucket=BUCKET,
        Key=key,
        Body=data,
        ContentType="application/x-ndjson",
    )
    print(f"  [{table}] uploaded {count} rows → s3://{BUCKET}/{key}  ({size_kb:.1f} KB)")
    return count


def backup_all(date_str: str, dry_run: bool = False):
    """Back up both tables for a given date."""
    if not BUCKET and not dry_run:
        raise EnvironmentError("S3_BUCKET is not set in .env")

    total = 0
    for table in TABLES:
        try:
            total += backup_table(table, date_str, dry_run=dry_run)
        except (BotoCoreError, ClientError) as exc:
            print(f"  [{table}] S3 error: {exc}")
        except Exception as exc:
            print(f"  [{table}] error: {exc}")
    return total


def ensure_bucket_exists(bucket: str, region: str):
    """Create the S3 bucket if it doesn't exist yet."""
    s3 = _s3_client()
    try:
        s3.head_bucket(Bucket=bucket)
        print(f"  Bucket s3://{bucket} already exists")
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("404", "NoSuchBucket"):
            kwargs = {"Bucket": bucket}
            if region != "us-east-1":
                kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
            s3.create_bucket(**kwargs)
            # Block all public access
            s3.put_public_access_block(
                Bucket=bucket,
                PublicAccessBlockConfiguration={
                    "BlockPublicAcls": True,
                    "IgnorePublicAcls": True,
                    "BlockPublicPolicy": True,
                    "RestrictPublicBuckets": True,
                },
            )
            print(f"  Created bucket s3://{bucket} in {region} (public access blocked)")
        else:
            raise


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Back up SQLite tables to S3 as NDJSON")
    parser.add_argument("--table", choices=list(TABLES), metavar="TABLE",
                        help=f"Table to back up ({', '.join(TABLES)}). Default: both.")
    parser.add_argument("--date", metavar="YYYY-MM-DD",
                        help="Date to export (default: today)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be uploaded without touching S3")
    parser.add_argument("--create-bucket", action="store_true",
                        help="Create the S3 bucket if it doesn't exist")
    args = parser.parse_args()

    date_str = args.date or datetime.now().strftime("%Y-%m-%d")

    if args.create_bucket:
        region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION", "us-east-1")
        ensure_bucket_exists(BUCKET, region)

    print(f"Backing up to s3://{BUCKET or '(S3_BUCKET not set)'}  date={date_str}")
    if args.table:
        backup_table(args.table, date_str, dry_run=args.dry_run)
    else:
        backup_all(date_str, dry_run=args.dry_run)
