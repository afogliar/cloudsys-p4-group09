"""Idempotently mirror the immutable course data into the object store."""

import argparse
import hashlib
from pathlib import Path

from botocore.exceptions import ClientError

from app.storage import create_s3_client, get_bucket_name, initialize_storage


def mirror_data(data_dir: Path) -> int:
    data_dir = data_dir.resolve()
    if not data_dir.is_dir():
        raise SystemExit(f"data directory not found: {data_dir}")

    client = create_s3_client()
    bucket = get_bucket_name()
    uploaded = 0
    for source in sorted(path for path in data_dir.rglob("*") if path.is_file()):
        relative = source.relative_to(data_dir).as_posix()
        key = f"base/{relative}"
        content = source.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        try:
            head = client.head_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            if exc.response["ResponseMetadata"]["HTTPStatusCode"] != 404:
                raise
            head = None
        if head and head.get("Metadata", {}).get("sha256") == digest:
            continue
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=content,
            ContentType="application/octet-stream",
            Metadata={"sha256": digest},
        )
        uploaded += 1
    return uploaded


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    # Kept in the CLI for compatibility with the Compose contract. The kit is
    # mounted read-only by the same job but its models are bootstrapped by
    # models-init, not copied into base/.
    parser.add_argument("--kit-root", type=Path, required=True)
    args = parser.parse_args()
    if not args.kit_root.is_dir():
        raise SystemExit(f"kit directory not found: {args.kit_root}")
    initialize_storage()
    print(f"Base data objects updated: {mirror_data(args.data_dir)}")
