import os

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


def get_bucket_name() -> str:
    return os.environ["S3_BUCKET"]


def create_s3_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["S3_ENDPOINT_URL"],
        aws_access_key_id=os.environ["MINIO_ROOT_USER"],
        aws_secret_access_key=os.environ["MINIO_ROOT_PASSWORD"],
        region_name="us-east-1",
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            connect_timeout=5,
            read_timeout=15,
            retries={"mode": "standard", "max_attempts": 2},
        ),
    )


def initialize_storage():
    client = create_s3_client()
    bucket = get_bucket_name()

    try:
        client.head_bucket(Bucket=bucket)
    except ClientError as exc:
        if exc.response["ResponseMetadata"]["HTTPStatusCode"] != 404:
            raise
        try:
            client.create_bucket(Bucket=bucket)
        except ClientError as create_exc:
            if create_exc.response["Error"]["Code"] != "BucketAlreadyOwnedByYou":
                raise
        client.head_bucket(Bucket=bucket)

    print(f"Bucket accessible : {bucket}")


def put_batch_json(object_key: str, content: bytes):
    """Write to a deterministic key; an identical retry replaces the same object."""
    create_s3_client().put_object(
        Bucket=get_bucket_name(),
        Key=object_key,
        Body=content,
        ContentType="application/json",
    )


if __name__ == "__main__":
    initialize_storage()
