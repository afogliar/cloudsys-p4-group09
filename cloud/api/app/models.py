"""Read published models and bootstrap the immutable initial kit versions."""
import argparse
import hashlib
import json
from pathlib import Path

from botocore.exceptions import ClientError

from app.database import connect
from app.storage import create_s3_client, get_bucket_name, initialize_storage


class ModelNotFoundError(Exception):
    pass


class ModelIntegrityError(Exception):
    pass


def initialize_model_tables():
    with connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS model_versions (
                category TEXT NOT NULL CHECK (category IN ('cons', 'pros')),
                version INTEGER NOT NULL CHECK (version >= 1),
                object_key TEXT NOT NULL UNIQUE,
                sha256 TEXT NOT NULL,
                size_bytes BIGINT NOT NULL CHECK (size_bytes > 0),
                status TEXT NOT NULL CHECK (status IN ('pending', 'released', 'rejected')),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (category, version)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS model_releases (
                category TEXT PRIMARY KEY,
                version INTEGER NOT NULL,
                FOREIGN KEY (category, version)
                    REFERENCES model_versions(category, version)
            )
        """)


def model_metadata(category: str, version: int | None = None) -> dict:
    with connect() as conn:
        if version is None:
            row = conn.execute("""
                SELECT m.version, m.object_key, m.sha256, m.size_bytes
                FROM model_releases r JOIN model_versions m
                  ON m.category = r.category AND m.version = r.version
                WHERE r.category = %s AND m.status = 'released'
            """, (category,)).fetchone()
        else:
            row = conn.execute("""
                SELECT version, object_key, sha256, size_bytes
                FROM model_versions
                WHERE category = %s AND version = %s AND status = 'released'
            """, (category, version)).fetchone()
    if row is None:
        raise ModelNotFoundError("Modèle publié introuvable")
    return dict(category=category, version=row[0], object_key=row[1],
                sha256=row[2], size_bytes=row[3])


def read_model(metadata: dict) -> bytes:
    try:
        response = create_s3_client().get_object(
            Bucket=get_bucket_name(), Key=metadata["object_key"],
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] in {"NoSuchKey", "NoSuchBucket", "404"}:
            raise ModelIntegrityError("Fichier publié absent du stockage") from exc
        raise
    with response["Body"] as body:
        content = body.read()
    if (len(content) != metadata["size_bytes"] or
            hashlib.sha256(content).hexdigest() != metadata["sha256"]):
        raise ModelIntegrityError("Intégrité du modèle invalide")
    return content


def import_initial_model(category: str, content: bytes, expected_hash: str):
    """Bootstrap version 1 only. Never overwrite different bytes or downgrade latest."""
    if category not in {"cons", "pros"}:
        raise ValueError("Catégorie inconnue")
    digest = hashlib.sha256(content).hexdigest()
    if not content or digest != expected_hash:
        raise ModelIntegrityError("Le modèle initial ne correspond pas au hash du kit")
    key = f"models/{category}/v0001/model.txt"
    with connect() as conn:
        conn.execute("SET LOCAL lock_timeout = '5s'")
        conn.execute("""
            INSERT INTO model_versions
              (category, version, object_key, sha256, size_bytes, status)
            VALUES (%s, 1, %s, %s, %s, 'pending')
            ON CONFLICT (category, version) DO NOTHING
        """, (category, key, digest, len(content)))
    with connect() as conn:
        conn.execute("SET LOCAL lock_timeout = '5s'")
        existing = conn.execute("""
            SELECT sha256, status FROM model_versions
            WHERE category = %s AND version = 1 FOR UPDATE
        """, (category,)).fetchone()
        if existing[0] != digest or existing[1] == "rejected":
            raise ModelIntegrityError("La version initiale existe avec un contenu ou statut incompatible")
        if existing[1] == "released":
            # Check, rather than silently repair, already published immutable bytes.
            read_model(dict(object_key=key, sha256=digest, size_bytes=len(content)))
        else:
            create_s3_client().put_object(
                Bucket=get_bucket_name(), Key=key, Body=content,
                ContentType="text/plain; charset=utf-8",
            )
            conn.execute("""
                UPDATE model_versions SET status = 'released'
                WHERE category = %s AND version = 1
            """, (category,))
        conn.execute("""
            INSERT INTO model_releases (category, version) VALUES (%s, 1)
            ON CONFLICT (category) DO NOTHING
        """, (category,))


def bootstrap(kit_root: Path):
    config = json.loads((kit_root / "meters.json").read_text(encoding="utf-8"))
    # Validate both source files before modifying the database or object store.
    inputs = []
    root = kit_root.resolve()
    for category in ("cons", "pros"):
        entry = config["categories"][category]
        if entry["initial_version"] != 1:
            raise ValueError("La version initiale attendue est 1")
        source = (root / entry["initial_model"]).resolve()
        if not source.is_relative_to(root):
            raise ValueError("Le chemin du modèle doit rester dans le kit")
        content = source.read_bytes()
        digest = entry["initial_model_sha256"]
        if hashlib.sha256(content).hexdigest() != digest or not content:
            raise ModelIntegrityError(f"Hash du kit invalide : {category}")
        inputs.append((category, content, digest))
    initialize_model_tables()
    initialize_storage()
    for category, content, digest in inputs:
        import_initial_model(category, content, digest)
        print(f"Modèle initial prêt : {category} v0001 sha256={digest}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--kit-root", type=Path, required=True)
    bootstrap(parser.parse_args().kit_root)
