"""Secret resolution (§10): tokens never leave Vault except into a short-lived in-process cache.

* ``EnvSecretStore`` (dev): ref ``coupang/access_key`` → env ``ACVM_SECRET__COUPANG__ACCESS_KEY``.
* ``VaultSecretStore`` (prod): AppRole login → KV v2 read; cached ≤ 5 min (the design's dynamic
  credential TTL) so a revoked/rotated token stops being used within one TTL.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

from acvm.config import SecurityConfig
from acvm.core.errors import ConfigError


def env_name(ref: str) -> str:
    return "ACVM_SECRET__" + ref.replace("/", "__").replace("-", "_").upper()


class EnvSecretStore:
    def __init__(self, overrides: dict[str, str] | None = None) -> None:
        self._mem: dict[str, str] = dict(overrides or {})

    async def get(self, ref: str) -> str:
        if ref in self._mem:
            return self._mem[ref]
        value = os.environ.get(env_name(ref))
        if value is None:
            raise KeyError(f"secret {ref!r} not set (expected env {env_name(ref)})")
        return value

    async def put(self, ref: str, value: str) -> None:
        self._mem[ref] = value

    async def delete(self, ref: str) -> bool:
        """Forget a console-entered value. A value pinned by the environment stays (it is not ours to remove)."""
        return self._mem.pop(ref, None) is not None


class DevFileSecretStore(EnvSecretStore):
    """Single-machine development store: env first, then a local JSON file that console-entered secrets are
    written to, so they survive restarts. Refused in prod (`Settings.assert_production_safe` requires Vault).

    On first use it generates the two secrets ACVM cannot run without (link signing key, click-IP salt) —
    rotating the signing key later invalidates existing link signatures, so they are generated exactly once.
    """

    BOOTSTRAP = ("acvm/link_signing_key", "acvm/click_ip_salt")

    def __init__(self, path: Path, bootstrap_refs: tuple[str, ...] = BOOTSTRAP) -> None:
        self.path = path
        data: dict[str, str] = {}
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
        changed = False
        for ref in bootstrap_refs:
            if ref not in data and os.environ.get(env_name(ref)) is None:
                data[ref] = secrets.token_hex(32)
                changed = True
        super().__init__(data)
        if changed:
            self._write()

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._mem, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    async def get(self, ref: str) -> str:
        value = os.environ.get(env_name(ref))  # explicit env always wins
        if value is not None:
            return value
        if ref in self._mem:
            return self._mem[ref]
        raise KeyError(f"secret {ref!r} not set (env {env_name(ref)} or console › 운영 준비)")

    async def put(self, ref: str, value: str) -> None:
        self._mem[ref] = value
        await asyncio.to_thread(self._write)

    async def delete(self, ref: str) -> bool:
        if self._mem.pop(ref, None) is None:
            return False
        await asyncio.to_thread(self._write)
        return True


class VaultSecretStore:
    def __init__(self, cfg: SecurityConfig) -> None:
        import hvac

        self.cfg = cfg
        self._client = hvac.Client(url=cfg.vault_addr)
        self._cache: dict[str, tuple[float, str]] = {}
        self._lock = asyncio.Lock()
        self._token_expiry = 0.0

    def _login(self) -> None:
        role_id = os.environ.get(self.cfg.vault_role_id_env)
        secret_id = os.environ.get(self.cfg.vault_secret_id_env)
        if not role_id or not secret_id:
            raise ConfigError("Vault AppRole credentials missing")
        resp: dict[str, Any] = self._client.auth.approle.login(role_id=role_id, secret_id=secret_id)
        ttl = float(resp["auth"].get("lease_duration", 300))
        self._token_expiry = time.monotonic() + ttl * 0.8

    def _read(self, ref: str) -> str:
        if time.monotonic() >= self._token_expiry:
            self._login()
        path, _, key = ref.rpartition("/")
        resp = self._client.secrets.kv.v2.read_secret_version(path=path or ref, mount_point=self.cfg.vault_mount,
                                                               raise_on_deleted_version=True)
        data: dict[str, str] = resp["data"]["data"]
        if key not in data:
            raise KeyError(f"secret {ref!r} not found in Vault")
        return data[key]

    async def get(self, ref: str) -> str:
        now = time.monotonic()
        hit = self._cache.get(ref)
        if hit and hit[0] > now:
            return hit[1]
        async with self._lock:
            value = await asyncio.to_thread(self._read, ref)
            self._cache[ref] = (now + self.cfg.secret_cache_ttl_seconds, value)
            return value

    async def put(self, ref: str, value: str) -> None:
        path, _, key = ref.rpartition("/")

        def _write() -> None:
            if time.monotonic() >= self._token_expiry:
                self._login()
            try:
                current = self._client.secrets.kv.v2.read_secret_version(
                    path=path, mount_point=self.cfg.vault_mount)["data"]["data"]
            except Exception:
                current = {}
            current[key] = value
            self._client.secrets.kv.v2.create_or_update_secret(path=path, secret=current,
                                                              mount_point=self.cfg.vault_mount)

        async with self._lock:
            await asyncio.to_thread(_write)
            self._cache.pop(ref, None)

    async def delete(self, ref: str) -> bool:
        path, _, key = ref.rpartition("/")

        def _remove() -> bool:
            if time.monotonic() >= self._token_expiry:
                self._login()
            try:
                current = self._client.secrets.kv.v2.read_secret_version(
                    path=path, mount_point=self.cfg.vault_mount)["data"]["data"]
            except Exception:
                return False
            if key not in current:
                return False
            current.pop(key)
            self._client.secrets.kv.v2.create_or_update_secret(path=path, secret=current,
                                                              mount_point=self.cfg.vault_mount)
            return True

        async with self._lock:
            removed = await asyncio.to_thread(_remove)
            self._cache.pop(ref, None)
            return removed


def build_secret_store(cfg: SecurityConfig, *, data_dir: Path | None = None,
                       env: str = "dev") -> EnvSecretStore | VaultSecretStore:
    if cfg.secret_backend == "vault":
        return VaultSecretStore(cfg)
    if cfg.secret_backend == "file" and data_dir is not None:
        return DevFileSecretStore(data_dir / "secrets.json", (cfg.link_signing_key_ref, cfg.click_ip_salt_ref))
    if env == "dev" and data_dir is not None:
        return DevFileSecretStore(data_dir / "secrets.dev.json",
                                  (cfg.link_signing_key_ref, cfg.click_ip_salt_ref))
    return EnvSecretStore()
