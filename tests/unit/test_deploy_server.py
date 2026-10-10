"""Single-host deployment (appctl/Caddy server): file secret store, staging hardening, launcher/install contracts."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from acvm.config import SecurityConfig, Settings
from acvm.infra.secrets import DevFileSecretStore, build_secret_store

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "deploy" / "server"


async def test_file_secret_backend_persists_console_keys(tmp_path: Path) -> None:
    cfg = SecurityConfig(secret_backend="file")
    store = build_secret_store(cfg, data_dir=tmp_path, env="staging")
    assert isinstance(store, DevFileSecretStore) and store.path == tmp_path / "secrets.json"
    link_key = await store.get(cfg.link_signing_key_ref)  # bootstrapped once, never regenerated
    assert len(link_key) == 64
    await store.put("gemini/api_key", "g-key-0000")
    again = build_secret_store(cfg, data_dir=tmp_path, env="staging")
    assert await again.get("gemini/api_key") == "g-key-0000" and await again.get(cfg.link_signing_key_ref) == link_key
    assert await again.delete("gemini/api_key") is True
    with pytest.raises(KeyError):
        await build_secret_store(cfg, data_dir=tmp_path, env="staging").get("gemini/api_key")
    # without the file backend a staging host keeps console values only in memory
    assert not isinstance(build_secret_store(SecurityConfig(), data_dir=tmp_path, env="staging"), DevFileSecretStore)


def test_prod_still_requires_vault() -> None:
    s = Settings(env="prod")
    s.security.secret_backend = "file"
    with pytest.raises(RuntimeError, match="vault"):
        s.assert_production_safe()


async def test_staging_hides_api_docs_and_requires_the_token() -> None:
    from acvm.api.app import create_app
    from acvm.context import build_test_context

    ctx = await build_test_context()
    staging = ctx.settings.model_copy(update={"env": "staging"})
    staging.security = staging.security.model_copy(update={"dev_api_token": "t" * 48})
    ctx.settings = staging  # one Settings object per process, exactly as on the server
    app = create_app(staging, ctx=ctx)
    async with (app.router.lifespan_context(app),
                httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://acvm.test") as c):
        assert (await c.get("/api/docs")).status_code == 404 and (await c.get("/api/openapi.json")).status_code == 404
        assert (await c.get("/api/v1/me")).status_code == 401
        assert (await c.get("/api/v1/me", headers={"Authorization": "Bearer dev-owner-token"})).status_code == 401
        ok = await c.get("/api/v1/me", headers={"Authorization": f"Bearer {'t' * 48}", "X-Dev-Role": "viewer"})
        assert ok.status_code == 200 and ok.json()["role"] == "owner"  # role header ignored outside dev/test
        assert (await c.get("/healthz")).status_code == 200
    await ctx.db.dispose()


def test_install_script_contract() -> None:
    sh = (SERVER / "install.sh").read_text(encoding="utf-8")
    assert b"\r\n" not in (SERVER / "install.sh").read_bytes()
    for needle in ("appctl add", "ACVM_SECURITY__SECRET_BACKEND file", "keep_env ACVM_SECURITY__DEV_API_TOKEN",
                   "NEXT_PUBLIC_ACVM_API=/api/v1", "uv\" sync --frozen --no-dev", "acvm seed --mature",
                   "lb_try_duration", "redir https://%s/ 308", "chown -R caddy:caddy /var/log/caddy",
                   "ACVM_SECURITY__AUTH_MODE session", "ACVM_SECURITY__SERVICE_TOKEN", "acvm.sh\" user list"):
        assert needle in sh, needle
    # the generated access token is written to the root:apps 640 env file and never echoed
    assert "echo \"$TOKEN" not in sh and "cat \"$ENVF\"" not in sh
    bash = shutil.which("bash")
    if bash:
        assert subprocess.run([bash, "-n", str(SERVER / "install.sh")], capture_output=True).returncode == 0  # noqa: S603


def test_launcher_routes_and_never_exposes_metrics() -> None:
    js = (SERVER / "launcher.mjs").read_text(encoding="utf-8")
    for prefix in ('"/api/"', '"/hub/"', '"/r/"', '"/media/"', '"/sandbox/"', '"/healthz"', '"/readyz"'):
        assert prefix in js, prefix
    assert 'BLOCKED = new Set(["/metrics"])' in js and '"--forwarded-allow-ips", "127.0.0.1"' in js
    assert 'ACVM_LOOPS_METRICS_ADDR: "127.0.0.1"' in js and 'HOSTNAME: "127.0.0.1"' in js
    node = shutil.which("node")
    if node:
        assert subprocess.run([node, "--check", str(SERVER / "launcher.mjs")], capture_output=True).returncode == 0  # noqa: S603


def test_server_cli_wrapper_never_takes_passwords_as_arguments() -> None:
    sh = (SERVER / "acvm.sh").read_text(encoding="utf-8")
    assert "sudo -u apps" in sh and ". \"$0\"" in sh and "--password" not in sh
    assert b"\r\n" not in (SERVER / "acvm.sh").read_bytes()
