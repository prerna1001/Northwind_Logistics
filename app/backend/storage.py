from __future__ import annotations

import json
import mimetypes
import shutil
from dataclasses import dataclass
from pathlib import Path

import boto3

from .config import get_settings


settings = get_settings()


@dataclass
class StorageResult:
    backend: str
    s3_key: str
    uri: str
    local_path: Path


def _prefixed_key(key: str) -> str:
    prefix = settings.raw_bucket_prefix.strip("/")
    return f"{prefix}/{key}" if prefix else key


def _local_path_for_key(key: str) -> Path:
    return settings.raw_store_dir / key


def _content_type_for(path: Path) -> str | None:
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed


def _upload_to_s3(local_path: Path, key: str) -> str | None:
    if not settings.r2_enabled:
        return None

    client = boto3.client(
        "s3",
        region_name=settings.r2_region,
        endpoint_url=settings.r2_endpoint_url,
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
    )
    extra_args = {}
    content_type = _content_type_for(local_path)
    if content_type:
        extra_args["ContentType"] = content_type
    client.upload_file(str(local_path), settings.raw_bucket_name, key, ExtraArgs=extra_args)
    return f"s3://{settings.raw_bucket_name}/{key}"


def store_bytes(key: str, content: bytes) -> StorageResult:
    full_key = _prefixed_key(key)
    local_path = _local_path_for_key(full_key)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(content)
    uri = _upload_to_s3(local_path, full_key)
    backend = settings.storage_backend_label if uri else "local_s3_mirror"
    return StorageResult(
        backend=backend,
        s3_key=full_key,
        uri=uri or f"s3://{settings.raw_bucket_name}/{full_key}",
        local_path=local_path,
    )


def store_json(key: str, payload: dict) -> StorageResult:
    return store_bytes(key, json.dumps(payload, indent=2).encode("utf-8"))


def store_file(key: str, source_path: Path) -> StorageResult:
    full_key = _prefixed_key(key)
    local_path = _local_path_for_key(full_key)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, local_path)
    uri = _upload_to_s3(local_path, full_key)
    backend = settings.storage_backend_label if uri else "local_s3_mirror"
    return StorageResult(
        backend=backend,
        s3_key=full_key,
        uri=uri or f"s3://{settings.raw_bucket_name}/{full_key}",
        local_path=local_path,
    )


def load_text(s3_key: str) -> str:
    local_path = _local_path_for_key(s3_key)
    if not local_path.exists():
        return ""
    return local_path.read_text(errors="ignore")


def load_bytes(s3_key: str) -> bytes:
    local_path = _local_path_for_key(s3_key)
    return local_path.read_bytes()
