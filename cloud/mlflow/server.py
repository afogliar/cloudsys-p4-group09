"""Dedicated MLflow database and proxied S3 artifacts; no credentials in argv."""
import os

import boto3
import psycopg
from psycopg import sql
from sqlalchemy.engine import URL
from botocore.exceptions import ClientError

host = os.environ.get("POSTGRES_HOST", "postgres")
user = os.environ["POSTGRES_USER"]
password = os.environ["POSTGRES_PASSWORD"]
database = os.environ.get("MLFLOW_DATABASE", "p4_mlflow")
with psycopg.connect(host=host, user=user, password=password,
                     dbname=os.environ["POSTGRES_DB"], autocommit=True) as conn:
    if not conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (database,)).fetchone():
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))

os.environ["MLFLOW_BACKEND_STORE_URI"] = URL.create(
    "postgresql+psycopg", username=user, password=password, host=host,
    port=5432, database=database,
).render_as_string(hide_password=False)
os.environ["AWS_ACCESS_KEY_ID"] = os.environ["MINIO_ROOT_USER"]
os.environ["AWS_SECRET_ACCESS_KEY"] = os.environ["MINIO_ROOT_PASSWORD"]
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
os.environ["MLFLOW_S3_ENDPOINT_URL"] = os.environ["S3_ENDPOINT_URL"]
bucket = os.environ["S3_BUCKET"]
s3 = boto3.client("s3", endpoint_url=os.environ["S3_ENDPOINT_URL"])
try:
    s3.head_bucket(Bucket=bucket)
except ClientError as exc:
    if exc.response["ResponseMetadata"]["HTTPStatusCode"] != 404:
        raise
    s3.create_bucket(Bucket=bucket)

os.execvp("mlflow", [
    "mlflow", "server", "--host", "0.0.0.0", "--port", "5000",
    "--serve-artifacts", "--artifacts-destination", f"s3://{bucket}/mlflow",
    "--allowed-hosts", os.environ.get("MLFLOW_ALLOWED_HOSTS", "mlflow:*,localhost:*,127.0.0.1:*"),
    "--workers", "1",
])
