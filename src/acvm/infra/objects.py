"""Object storage. Production: Cloudflare R2 (S3 API, zero egress, custom domain) — Instagram and
TikTok pull media from a public HTTPS URL, so we hand them a 2-hour presigned URL (§4 M8)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import shutil
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from acvm.config import ObjectStoreConfig
from acvm.ports import SecretStore


class LocalObjectStore:
    """Filesystem store served by the API at /media (dev). Signed URLs carry an HMAC + expiry so the
    dev server enforces the same TTL semantics as R2."""

    def __init__(self, cfg: ObjectStoreConfig, signing_key: bytes = b"dev-media-key") -> None:
        self.root = Path(cfg.local_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.public_base = cfg.public_base_url.rstrip("/")
        self._key = signing_key

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root not in p.parents and p != self.root:
            raise ValueError("object key escapes the store root")
        return p

    async def put_file(self, local: Path, key: str, content_type: str) -> str:
        dst = self._path(key)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if Path(local).resolve() != dst:  # noqa: ASYNC240 — path normalisation, no I/O wait
            await asyncio.to_thread(shutil.copyfile, local, dst)
        return f"file://{key}"

    async def put_bytes(self, data: bytes, key: str, content_type: str) -> str:
        dst = self._path(key)
        dst.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(dst.write_bytes, data)
        return f"file://{key}"

    def _key_of(self, uri: str) -> str:
        if not uri.startswith("file://"):
            raise ValueError(f"not a local object uri: {uri}")
        return uri[len("file://"):]

    async def get_bytes(self, uri: str) -> bytes:
        return await asyncio.to_thread(self._path(self._key_of(uri)).read_bytes)

    def sign(self, key: str, expires: int) -> str:
        return hmac.new(self._key, f"{key}:{expires}".encode(), hashlib.sha256).hexdigest()[:32]

    def verify(self, key: str, expires: int, sig: str) -> bool:
        return expires >= int(time.time()) and hmac.compare_digest(self.sign(key, expires), sig)

    async def signed_url(self, uri: str, ttl_seconds: int) -> str:
        key = self._key_of(uri)
        exp = int(time.time()) + ttl_seconds
        return f"{self.public_base}/{quote(key)}?exp={exp}&sig={self.sign(key, exp)}"

    async def local_path(self, uri: str) -> Path:
        return self._path(self._key_of(uri))


class S3ObjectStore:
    """R2/S3 via boto3 (threaded). Credentials come from the SecretStore, never from env defaults."""

    def __init__(self, cfg: ObjectStoreConfig, secrets: SecretStore, cache_dir: Path) -> None:
        self.cfg = cfg
        self.secrets = secrets
        self.cache_dir = cache_dir
        self._client: Any = None
        self._lock = asyncio.Lock()

    async def _s3(self) -> Any:
        async with self._lock:
            if self._client is None:
                import boto3

                self._client = boto3.client(
                    "s3", endpoint_url=self.cfg.endpoint_url or None, region_name="auto",
                    aws_access_key_id=await self.secrets.get(self.cfg.access_key_ref),
                    aws_secret_access_key=await self.secrets.get(self.cfg.secret_key_ref))
            return self._client

    def _uri(self, key: str) -> str:
        return f"s3://{self.cfg.bucket}/{key}"

    @staticmethod
    def _split(uri: str) -> tuple[str, str]:
        u = urlparse(uri)
        return u.netloc, u.path.lstrip("/")

    async def put_file(self, local: Path, key: str, content_type: str) -> str:
        s3 = await self._s3()
        await asyncio.to_thread(s3.upload_file, str(local), self.cfg.bucket, key,
                                ExtraArgs={"ContentType": content_type})
        return self._uri(key)

    async def put_bytes(self, data: bytes, key: str, content_type: str) -> str:
        s3 = await self._s3()
        await asyncio.to_thread(s3.put_object, Bucket=self.cfg.bucket, Key=key, Body=data, ContentType=content_type)
        return self._uri(key)

    async def get_bytes(self, uri: str) -> bytes:
        s3 = await self._s3()
        bucket, key = self._split(uri)
        obj = await asyncio.to_thread(s3.get_object, Bucket=bucket, Key=key)
        return await asyncio.to_thread(obj["Body"].read)

    async def signed_url(self, uri: str, ttl_seconds: int) -> str:
        s3 = await self._s3()
        bucket, key = self._split(uri)
        return await asyncio.to_thread(s3.generate_presigned_url, "get_object",
                                       Params={"Bucket": bucket, "Key": key}, ExpiresIn=ttl_seconds)

    async def local_path(self, uri: str) -> Path:
        bucket, key = self._split(uri)
        dst = self.cache_dir / bucket / key
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            s3 = await self._s3()
            await asyncio.to_thread(s3.download_file, bucket, key, str(dst))
        return dst


def build_object_store(cfg: ObjectStoreConfig, secrets: SecretStore, cache_dir: Path) -> LocalObjectStore | S3ObjectStore:
    return S3ObjectStore(cfg, secrets, cache_dir) if cfg.backend == "s3" else LocalObjectStore(cfg)
