"""Static validation of deployment artefacts (no Docker daemon, no cluster): parseability, cross-file contracts
(metric names ↔ src/acvm/core/metrics.py, Settings ↔ .env.example, runbook anchors, datasource UIDs) and the
security invariants of §10 (least-privilege Vault policies, private networks, hardened containers)."""

from __future__ import annotations

import json
import re
import runpy
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"

try:  # PyYAML ships transitively (uvicorn[standard]); keep a structural fallback so the suite never hard-fails
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


# ─────────────────────────── helpers ───────────────────────────


if yaml is not None:
    class _Loader(yaml.SafeLoader):
        """SafeLoader that tolerates compose's !reset / !override merge tags."""

    def _tagged(loader: Any, _suffix: str, node: Any) -> Any:
        if isinstance(node, yaml.MappingNode):
            return loader.construct_mapping(node, deep=True)
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node, deep=True)
        return loader.construct_scalar(node)

    _Loader.add_multi_constructor("!", _tagged)


def _structural_yaml_check(text: str, path: Path) -> None:
    assert "\t" not in text.replace("\t#", ""), f"{path}: tab indentation"
    for n, line in enumerate(text.splitlines(), 1):
        stripped = line.split(" #")[0].rstrip()
        if stripped.count('"') % 2 and not stripped.endswith(("|", ">", "|-", ">-")):
            raise AssertionError(f"{path}:{n}: unbalanced double quote")


def load_yaml_docs(path: Path) -> list[Any]:
    text = path.read_text(encoding="utf-8")
    if yaml is None:  # pragma: no cover
        _structural_yaml_check(text, path)
        pytest.skip("PyYAML not installed — structural check only")
    return [d for d in yaml.load_all(text, Loader=_Loader) if d is not None]


def load_yaml(path: Path) -> Any:
    docs = load_yaml_docs(path)
    assert len(docs) == 1, f"{path}: expected one YAML document"
    return docs[0]


def yaml_files() -> list[Path]:
    files = [p for p in DEPLOY.rglob("*") if p.suffix in {".yml", ".yaml"}]
    files += list((ROOT / ".github").rglob("*.yml"))
    return sorted(files)


def walk(obj: Any) -> Iterator[Any]:
    yield obj
    if isinstance(obj, dict):
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)


def all_rules() -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    for f in sorted((DEPLOY / "prometheus" / "rules").glob("*.yml")):
        for group in load_yaml(f)["groups"]:
            rules += group["rules"]
    return rules


def dashboards() -> dict[str, dict[str, Any]]:
    return {p.name: json.loads(p.read_text(encoding="utf-8"))
            for p in sorted((DEPLOY / "grafana" / "dashboards").glob("*.json"))}


def promql_strings() -> list[str]:
    exprs = [r["expr"] for r in all_rules()]
    for board in dashboards().values():
        for node in walk(board):
            if isinstance(node, dict) and node.get("datasource", {}).get("uid") == "prometheus" and "expr" in node:
                exprs.append(node["expr"])
    return exprs


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    return load_yaml(DEPLOY / "compose" / "docker-compose.yml")


@pytest.fixture(scope="module")
def compose_dev() -> dict[str, Any]:
    return load_yaml(DEPLOY / "compose" / "docker-compose.dev.yml")


# ─────────────────────────── parseability ───────────────────────────


@pytest.mark.parametrize("path", yaml_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_yaml_parses(path: Path) -> None:
    assert load_yaml_docs(path), f"{path} is empty"


@pytest.mark.parametrize("path", sorted(DEPLOY.rglob("*.json")), ids=lambda p: p.name)
def test_json_parses(path: Path) -> None:
    json.loads(path.read_text(encoding="utf-8"))


def test_text_files_use_lf_line_endings() -> None:
    suffixes = {".sh", ".yml", ".yaml", ".hcl", ".conf", ".service", ".timer", ".example", ".env", ".json", ".ini"}
    offenders = [p for p in [*DEPLOY.rglob("*"), ROOT / "Makefile", ROOT / ".env.example", ROOT / "alembic.ini"]
                 if p.is_file() and (p.suffix in suffixes or p.name in {"Makefile", "Dockerfile"})
                 and b"\r\n" in p.read_bytes()]
    assert offenders == []


# ─────────────────────────── metrics contract ───────────────────────────


def _exported_metric_names() -> set[str]:
    from prometheus_client import Counter, Gauge, Histogram

    import acvm.core.metrics as m

    names: set[str] = set()
    for obj in vars(m).values():
        if isinstance(obj, Counter):
            names.add(obj._name + "_total")
        elif isinstance(obj, Histogram):
            names |= {obj._name + s for s in ("_bucket", "_count", "_sum")}
        elif isinstance(obj, Gauge):
            names.add(obj._name)
    return names


# Series produced outside src/acvm/core/metrics.py (pushed by deploy/backup/*.sh) or explicitly requested
# from the application (acvm_budget_limit_krw: the rules fall back to the 20,000 KRW default until it exists).
EXTERNAL_ACVM_SERIES = {
    "acvm_backup_last_success_timestamp_seconds",
    "acvm_backup_duration_seconds",
    "acvm_restore_drill_rto_seconds",
    "acvm_restore_drill_rpo_seconds",
    "acvm_budget_limit_krw",
}


def test_every_acvm_metric_referenced_exists_in_metrics_module() -> None:
    exported = _exported_metric_names()
    referenced: set[str] = set()
    for expr in promql_strings():
        referenced |= set(re.findall(r"\bacvm_[a-z0-9_]+\b", expr))
    unknown = referenced - exported - EXTERNAL_ACVM_SERIES
    assert not unknown, f"metrics used in rules/dashboards but not defined in acvm.core.metrics: {sorted(unknown)}"


def test_every_metric_is_used_by_rules_or_dashboards() -> None:
    text = "\n".join(promql_strings())
    bases = {re.sub(r"_(bucket|count|sum)$", "", n) for n in _exported_metric_names()}
    unused = {b for b in bases if b not in text}
    assert not unused, f"metrics exported but never alerted on or charted: {sorted(unused)}"


def test_recording_rules_used_are_defined() -> None:
    defined = {r["record"] for r in all_rules() if "record" in r}
    used: set[str] = set()
    for expr in promql_strings():
        used |= set(re.findall(r"\bacvm:[a-z0-9_:]+\b", expr))
    assert used - defined == set()
    for name in defined:
        assert re.fullmatch(r"acvm:[a-z0-9_]+:[a-z0-9_]+", name), f"recording rule naming: {name}"


def test_slo_thresholds_match_design_section_9_2() -> None:
    by_name = {r["alert"]: r for r in all_rules() if "alert" in r}
    assert "> 20" in by_name["ACVMPlanLatencySLOBreach"]["expr"]
    assert "> 180" in by_name["ACVMTextCommandToPublishSLOBreach"]["expr"]
    assert "> 600" in by_name["ACVMShortCommandToPublishSLOBreach"]["expr"]
    assert "< 0.99" in by_name["ACVMPublishSuccessBelowSLO"]["expr"]
    assert "> 3600" in by_name["ACVMPlatformAttributionStale"]["expr"]
    assert "> 129600" in by_name["ACVMCoupangAttributionStale"]["expr"]
    regression = by_name["ACVMGateRuleRegression"]
    assert "acvm_gate_rule_regressions_total" in regression["expr"] and "> 0" in regression["expr"]
    assert regression["labels"]["severity"] == "page" and "for" not in regression
    assert ">= 0.9" in by_name["ACVMBudgetNearlyExhausted"]["expr"]
    for alert in ("ACVMCircuitBreakerOpen", "ACVMCoupangForbidden", "ACVMAccountRestricted",
                  "ACVMAccountCooldownSpike", "ACVMInvalidClickDetected", "ACVMHoldQueueBacklog"):
        assert alert in by_name
    # 1-hour success ratio excludes policy rejects
    ratio = next(r for r in all_rules() if r.get("record") == "acvm:publish_success:ratio_rate1h")["expr"]
    assert "[1h]" in ratio and "policy_reject" not in ratio.split("/")[-1]


def test_label_values_match_application_vocabulary() -> None:
    from acvm.core.enums import ContentKind

    exprs = "\n".join(promql_strings())
    for kind in re.findall(r'command_to_publish[a-z0-9_:]*\{[^}]*kind="([a-z_]+)"', exprs):
        assert kind in {k.value for k in ContentKind}
    dist = ROOT / "src" / "acvm" / "modules" / "m8_distribution" / "service.py"
    if dist.exists():
        source = dist.read_text(encoding="utf-8")
        outcomes = set()
        for regex in re.findall(r'acvm_publish_attempts_total\{outcome=~"([^"]+)"\}', exprs):
            outcomes |= set(regex.split("|"))
        missing = {o for o in outcomes - {"manual_published", "policy_reject"} if f'"{o}"' not in source}
        assert not missing, f"publish outcomes not emitted by m8_distribution: {missing}"
    loops = ROOT / "src" / "acvm" / "orchestration" / "loops.py"
    if loops.exists():
        assert '"hold_review"' in loops.read_text(encoding="utf-8")


def test_alerts_have_severity_and_resolvable_runbook() -> None:
    runbook = (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
    anchors = set(re.findall(r'<a id="([a-z0-9-]+)"', runbook))
    for rule in all_rules():
        if "alert" not in rule:
            continue
        assert rule["labels"]["severity"] in {"page", "ticket"}, rule["alert"]
        assert rule["annotations"]["summary"], rule["alert"]
        url = rule["annotations"].get("runbook_url")
        if url:
            assert url.startswith("docs/RUNBOOK.md#") and url.split("#", 1)[1] in anchors, (rule["alert"], url)


def test_prometheus_scrapes_api_and_worker() -> None:
    cfg = load_yaml(DEPLOY / "prometheus" / "prometheus.yml")
    jobs = {j["job_name"]: j for j in cfg["scrape_configs"]}
    assert jobs["acvm-api"]["dns_sd_configs"][0]["port"] == 8000
    assert jobs["acvm-api"]["metrics_path"] == "/metrics"
    for job in ("acvm-worker", "acvm-render", "acvm-publisher", "acvm-loops"):
        assert jobs[job]["dns_sd_configs"][0]["port"] == 9464
    assert cfg["rule_files"] == ["/etc/prometheus/rules/*.yml"]
    assert "pushgateway" in jobs and jobs["pushgateway"]["honor_labels"] is True


def test_alertmanager_routes_point_at_defined_receivers() -> None:
    cfg = load_yaml(DEPLOY / "alertmanager" / "alertmanager.yml")
    receivers = {r["name"] for r in cfg["receivers"]}
    assert {"page", "ticket"} <= receivers
    assert cfg["route"]["receiver"] in receivers
    assert all(r["receiver"] in receivers for r in cfg["route"]["routes"])
    for node in walk(cfg):  # webhook URLs are files, never inline secrets
        if isinstance(node, dict):
            assert "api_url" not in node and "url" not in node


def test_otel_collector_pipelines_reference_defined_components() -> None:
    cfg = load_yaml(DEPLOY / "otel" / "collector.yaml")
    for name, pipe in cfg["service"]["pipelines"].items():
        for kind in ("receivers", "processors", "exporters"):
            for comp in pipe.get(kind, []):
                assert comp in cfg[kind], f"{name}: {kind} {comp} undefined"
    assert cfg["exporters"]["prometheus"]["endpoint"].endswith(":8889")


# ─────────────────────────── Grafana ───────────────────────────


def test_dashboards_are_generated_and_cover_section_9_3() -> None:
    mod = runpy.run_path(str(DEPLOY / "grafana" / "build_dashboards.py"), run_name="acvm_dashboards")
    rendered = mod["render"]()
    on_disk = {p.name: p.read_text(encoding="utf-8") for p in (DEPLOY / "grafana" / "dashboards").glob("*.json")}
    assert rendered == on_disk, "run: uv run --no-sync python deploy/grafana/build_dashboards.py"
    uids = {b["uid"] for b in dashboards().values()}
    assert uids == {"acvm-slo", "acvm-money", "acvm-pipeline", "acvm-risk", "acvm-learning"}


def test_dashboard_panels_use_provisioned_datasources() -> None:
    provisioned = {d["uid"] for d in load_yaml(DEPLOY / "grafana" / "provisioning" / "datasources"
                                               / "datasources.yml")["datasources"]}
    allowed = provisioned | {"__expr__"}
    for name, board in dashboards().items():
        ids = [p["id"] for p in board["panels"]]
        assert len(ids) == len(set(ids)), name
        for panel in board["panels"]:
            pos = panel["gridPos"]
            assert pos["x"] + pos["w"] <= 24, (name, panel["title"])
        for node in walk(board):
            if isinstance(node, dict) and isinstance(node.get("datasource"), dict):
                assert node["datasource"]["uid"] in allowed, (name, node["datasource"])


def test_money_view_separates_gmv_commission_and_net() -> None:
    money = dashboards()["acvm-money.json"]
    titles = " ".join(p["title"] for p in money["panels"])
    for word in ("GMV", "확정 수수료", "취소", "순이익", "I*"):
        assert word in titles


# ─────────────────────────── compose ───────────────────────────

APP_SERVICES = ("acvm-api", "acvm-worker", "acvm-publisher", "acvm-render", "acvm-loops", "acvm-migrate")


def test_compose_has_every_required_service(compose: dict[str, Any]) -> None:
    required = {"postgres", "redis", "clickhouse", "temporal", "temporal-ui", "vault", "otel-collector",
                "prometheus", "grafana", "loki", "promtail", "acvm-migrate", "acvm-api", "acvm-worker",
                "acvm-render", "acvm-loops", "console", "cloudflared", "alertmanager"}
    assert required <= set(compose["services"])


def test_compose_entry_points_match_application(compose: dict[str, Any]) -> None:
    s = compose["services"]
    assert s["acvm-migrate"]["command"] == ["acvm", "db", "upgrade"]
    assert s["acvm-migrate"]["restart"] == "no"
    assert s["acvm-worker"]["command"] == ["python", "-m", "acvm.orchestration.worker"]
    assert s["acvm-render"]["command"] == ["python", "-m", "acvm.orchestration.worker", "--queue", "acvm-render"]
    assert s["acvm-loops"]["command"] == ["python", "-m", "acvm.orchestration.loops"]
    assert "/acvm-api:" in s["acvm-api"]["image"]  # CMD uvicorn acvm.api.app:app lives in the api image
    assert s["console"]["build"] == "../../console"
    dockerfile = (DEPLOY / "docker" / "Dockerfile").read_text(encoding="utf-8")
    assert 'CMD ["uvicorn", "acvm.api.app:app", "--host", "0.0.0.0", "--port", "8000"]' in dockerfile


def test_compose_migration_gates_app_start(compose: dict[str, Any]) -> None:
    for name in ("acvm-api", "acvm-worker", "acvm-publisher", "acvm-render", "acvm-loops"):
        dep = compose["services"][name]["depends_on"]["acvm-migrate"]
        assert dep["condition"] == "service_completed_successfully", name
    assert compose["services"]["acvm-migrate"]["depends_on"]["postgres"]["condition"] == "service_healthy"


def test_compose_app_containers_are_hardened(compose: dict[str, Any]) -> None:
    for name in APP_SERVICES:
        svc = compose["services"][name]
        assert svc["read_only"] is True, name
        assert svc["cap_drop"] == ["ALL"], name
        assert "no-new-privileges:true" in svc["security_opt"], name
        assert svc["logging"]["options"]["max-size"], name
        env = svc["environment"]
        assert env["ACVM_SECURITY__SECRET_BACKEND"].startswith("${ACVM_SECRET_BACKEND:-vault}"), name
        assert "image" in svc and "build" not in svc, f"{name}: prod must run released images"


def test_compose_every_long_running_service_restarts_and_rotates_logs(compose: dict[str, Any]) -> None:
    for name, svc in compose["services"].items():
        assert "logging" in svc, name
        if name != "acvm-migrate":
            assert svc.get("restart") == "unless-stopped", name
        assert svc.get("deploy", {}).get("resources", {}).get("limits"), f"{name}: resource limits"


def test_compose_network_exposure(compose: dict[str, Any]) -> None:
    nets = compose["networks"]
    assert nets["backend"]["internal"] is True
    for name, svc in compose["services"].items():
        for port in svc.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), f"{name} publishes {port} beyond loopback"
        if name in {"redis", "vault", "temporal", "loki", "prometheus"}:
            assert svc["networks"] == ["backend"], f"{name} must stay on the private network"
        if name in {"postgres", "clickhouse"}:  # + outbound-only egress for R2 backups
            assert svc["networks"] == ["backend", "egress"], name
    assert compose["services"]["cloudflared"]["networks"] == ["edge"]


def test_cloudflare_tunnel_only_exposes_hub_publicly(compose: dict[str, Any]) -> None:
    cfg = yaml.safe_load(compose["configs"]["cloudflared-config"]["content"]) if yaml else None
    assert cfg is not None
    rules = cfg["ingress"]
    assert rules[-1] == {"service": "http_status:404"}
    for rule in rules[:-1]:
        protected = rule.get("originRequest", {}).get("access", {}).get("required") is True
        if protected or rule["service"].startswith("http_status:"):
            continue
        assert rule["hostname"] == "${ACVM_HUB_HOST}", rule
        assert rule["path"] in {"^/(hub|r)/", "^/media/"}, rule
    console_rules = [r for r in rules if "CONSOLE_HOST" in r.get("hostname", "")]
    assert console_rules and all(r["originRequest"]["access"]["required"] for r in console_rules)


def test_compose_vault_and_secrets(compose: dict[str, Any]) -> None:
    vault = compose["services"]["vault"]
    assert "IPC_LOCK" in vault["cap_add"]
    assert any("vault.hcl" in v for v in vault["volumes"])
    hcl = (DEPLOY / "vault" / "config" / "vault.hcl").read_text(encoding="utf-8")
    assert re.search(r'storage\s+"file"', hcl)
    roles = {"acvm-api": "api", "acvm-worker": "worker", "acvm-publisher": "publisher", "acvm-render": "render",
             "acvm-loops": "loops"}
    for svc, role in roles.items():
        files = [e["path"] for e in compose["services"][svc]["env_file"]]
        assert f"./env/approle-{role}.env" in files, svc
    raw = (DEPLOY / "compose" / "docker-compose.yml").read_text(encoding="utf-8")
    assert not re.search(r"(PASSWORD|SECRET|TOKEN)[A-Z_]*:\s*['\"]?[A-Za-z0-9]{8,}", raw), "inline secret in compose"


def test_compose_datastore_settings(compose: dict[str, Any]) -> None:
    s = compose["services"]
    assert s["postgres"]["image"].endswith("acvm-postgres:${ACVM_POSTGRES_TAG:-16-walg}")
    assert "pgvector/pgvector" in (DEPLOY / "postgres" / "Dockerfile").read_text(encoding="utf-8")
    pgconf = (DEPLOY / "postgres" / "postgresql.conf").read_text(encoding="utf-8")
    assert "archive_mode = on" in pgconf and "acvm-wal-archive" in pgconf
    redis = (DEPLOY / "redis" / "redis.conf").read_text(encoding="utf-8")
    assert "appendonly yes" in redis and "maxmemory-policy noeviction" in redis
    assert "../clickhouse/schema.sql:/docker-entrypoint-initdb.d/10-schema.sql:ro" in s["clickhouse"]["volumes"]
    assert s["temporal"]["image"].startswith("temporalio/auto-setup:")
    assert s["temporal"]["environment"]["DB"] == "postgres12"
    assert "127.0.0.1:8088:8080" in s["temporal-ui"]["ports"]


def test_compose_dev_override_is_safe(compose_dev: dict[str, Any]) -> None:
    for name in APP_SERVICES:
        env = compose_dev["services"][name]["environment"]
        assert env["ACVM_PLATFORMS__DRY_RUN"] == "true", name
        assert env["ACVM_COUPANG__SANDBOX"] == "true", name
        assert env["ACVM_SECURITY__SECRET_BACKEND"] == "env", name
    assert compose_dev["services"]["vault"]["command"] == ["server", "-dev"]
    assert compose_dev["services"]["cloudflared"]["profiles"] == ["tunnel"]


# ─────────────────────────── images ───────────────────────────


def test_dockerfile_targets_and_hardening() -> None:
    df = (DEPLOY / "docker" / "Dockerfile").read_text(encoding="utf-8")
    for target in ("api", "worker", "render"):
        assert re.search(rf"^FROM .+ AS {target}$", df, re.M), target
    assert "ARG PYTHON_DIGEST=" in df and "${PYTHON_DIGEST:+@}${PYTHON_DIGEST}" in df
    assert "uv sync --frozen --no-dev" in df
    assert df.count("USER 10001:10001") == 3
    assert 'ENTRYPOINT ["/usr/bin/tini", "--"]' in df
    assert df.count("HEALTHCHECK") == 3
    assert "ffmpeg fonts-noto-cjk" in df
    assert "org.opencontainers.image.revision" in df
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    for entry in (".env", ".venv", ".git", "var"):
        assert entry in ignore


# ─────────────────────────── Vault ───────────────────────────


def _policy_paths(name: str) -> dict[str, set[str]]:
    hcl = (DEPLOY / "vault" / "policies" / f"{name}.hcl").read_text(encoding="utf-8")
    hcl = "\n".join(line.split("#")[0] for line in hcl.splitlines())
    out: dict[str, set[str]] = {}
    for path, caps in re.findall(r'path\s+"([^"]+)"\s*\{\s*capabilities\s*=\s*\[([^\]]*)\]\s*\}', hcl):
        out[path] = {c.strip().strip('"') for c in caps.split(",") if c.strip()}
    assert out, name
    return out


def test_vault_policies_least_privilege() -> None:
    api, worker, publisher = (_policy_paths(n) for n in ("acvm-api", "acvm-worker", "acvm-publisher"))
    token_paths = lambda pol: {p: c for p, c in pol.items() if "tokens" in p}  # noqa: E731
    assert all(caps == {"deny"} for caps in token_paths(api).values()) and token_paths(api), "api must deny tokens"
    assert token_paths(worker) == {}, "worker must not mention tokens (implicit deny; combinable with publisher)"
    assert "read" in publisher["acvm/data/tokens/*"]
    assert not any("delete" in c or "sudo" in c for c in publisher.values())
    for pol in (api, worker, publisher):
        assert not any(p in {"*", "acvm/*", "acvm/data/*", "sys/*"} for p in pol), "no wildcard grants"
    assert "acvm/data/coupang" not in api or api["acvm/data/coupang"] == {"deny"}


def test_vault_bootstrap_contract() -> None:
    sh = (DEPLOY / "vault" / "bootstrap.sh").read_text(encoding="utf-8")
    assert "vault secrets enable -path=acvm -version=2" in sh
    assert "vault auth enable approle" in sh
    assert 'TOKEN_TTL="${ACVM_VAULT_TOKEN_TTL:-5m}"' in sh and 'token_ttl="$TOKEN_TTL"' in sh
    for role in ("acvm-api", "acvm-worker", "acvm-render", "acvm-publisher", "acvm-loops"):
        assert role in sh
    from acvm.config import SecurityConfig

    assert SecurityConfig().vault_mount == "acvm"
    assert SecurityConfig().secret_cache_ttl_seconds <= 300


# ─────────────────────────── scripts ───────────────────────────


@pytest.mark.parametrize("path", sorted([*DEPLOY.rglob("*.sh")]), ids=lambda p: str(p.relative_to(DEPLOY)))
def test_shell_scripts_are_strict(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("#!/"), path
    assert re.search(r"^set -[a-z]*e", text, re.M), f"{path}: missing set -e"


def test_backup_contract() -> None:
    backup = (DEPLOY / "backup" / "backup.sh").read_text(encoding="utf-8")
    restore = (DEPLOY / "backup" / "restore.sh").read_text(encoding="utf-8")
    assert "wal-g backup-push" in backup and "wal-g delete retain FULL" in backup
    assert "BACKUP DATABASE acvm TO S3" in backup
    assert "acvm_backup_last_success_timestamp_seconds" in backup
    assert "archive_mode=off" in restore and "WALG_ENABLED=false" in restore  # drills never fork the WAL timeline
    assert "7200" in restore  # RTO 2 h
    walg = (DEPLOY / "backup" / "walg.env.example").read_text(encoding="utf-8")
    assert "WALG_S3_PREFIX=s3://" in walg and "r2.cloudflarestorage.com" in walg and "WALG_LIBSODIUM_KEY" in walg


# ─────────────────────────── .env.example ↔ Settings ───────────────────────────


def _settings_env_names() -> set[str]:
    from pydantic import BaseModel

    from acvm.config import Settings

    names: set[str] = set()

    def visit(model: type[BaseModel], prefix: str) -> None:
        for field, info in model.model_fields.items():
            ann = info.annotation
            if isinstance(ann, type) and issubclass(ann, BaseModel):
                visit(ann, f"{prefix}{field.upper()}__")
            else:
                names.add(f"{prefix}{field.upper()}")

    visit(Settings, "ACVM_")
    return names


def test_env_example_documents_every_setting() -> None:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    documented = set(re.findall(r"^#?\s*(ACVM_[A-Z0-9_]+)=", text, re.M))
    settings = _settings_env_names()
    missing = settings - documented
    assert not missing, f".env.example is missing: {sorted(missing)}"
    special = {n for n in documented if n.startswith("ACVM_SECRET__")} | {"ACVM_VAULT_ROLE_ID", "ACVM_VAULT_SECRET_ID"}
    stale = documented - settings - special
    assert not stale, f".env.example documents unknown settings: {sorted(stale)}"
    for line in text.splitlines():  # no real-looking secrets
        if re.match(r"^ACVM_SECRET__", line):
            assert "dev" in line.split("=", 1)[1], line


# ─────────────────────────── Kubernetes ───────────────────────────


def _k8s_docs() -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    for f in sorted((DEPLOY / "k8s").rglob("*.yaml")):
        if f.name != "kustomization.yaml":
            docs += load_yaml_docs(f)
    return docs


def test_k8s_workloads_are_pod_security_restricted() -> None:
    for doc in _k8s_docs():
        if doc.get("kind") not in {"Deployment", "Job"}:
            continue
        pod = doc["spec"]["template"]["spec"]
        name = doc["metadata"]["name"]
        assert pod["securityContext"]["runAsNonRoot"] is True, name
        assert pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault", name
        assert pod.get("automountServiceAccountToken") is False, name
        for c in [*pod.get("initContainers", []), *pod["containers"]]:
            sc = c["securityContext"]
            assert sc["allowPrivilegeEscalation"] is False, (name, c["name"])
            assert sc["readOnlyRootFilesystem"] is True, (name, c["name"])
            assert sc["capabilities"]["drop"] == ["ALL"], (name, c["name"])
            assert c["resources"]["limits"], (name, c["name"])


def test_k8s_topology() -> None:
    docs = _k8s_docs()
    by = {(d["kind"], d["metadata"]["name"]): d for d in docs}
    ns = by[("Namespace", "acvm")]
    assert ns["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"
    deny = by[("NetworkPolicy", "default-deny-all")]
    assert deny["spec"]["podSelector"] == {} and set(deny["spec"]["policyTypes"]) == {"Ingress", "Egress"}
    loops = by[("Deployment", "acvm-loops")]
    assert loops["spec"]["replicas"] == 1 and loops["spec"]["strategy"]["type"] == "Recreate"
    assert by[("Job", "acvm-migrate")]["spec"]["template"]["spec"]["containers"][0]["command"] == \
        ["acvm", "db", "upgrade"]
    assert ("HorizontalPodAutoscaler", "acvm-api") in by
    for scaled, queue in (("acvm-worker", "acvm-main"), ("acvm-render", "acvm-render"),
                          ("acvm-publisher", "acvm-publish")):
        trig = by[("ScaledObject", scaled)]["spec"]["triggers"][0]
        assert trig["type"] == "prometheus" and f'taskqueue="{queue}"' in trig["metadata"]["query"], scaled
    kust = load_yaml(DEPLOY / "k8s" / "overlays" / "prod" / "kustomization.yaml")
    assert "../../base" in kust["resources"]


# ─────────────────────────── CI/CD ───────────────────────────


def test_ci_workflow_gates() -> None:
    ci = load_yaml(ROOT / ".github" / "workflows" / "ci.yml")
    steps = json.dumps(ci["jobs"])
    for needle in ("uv sync --frozen", "ruff check", "mypy", "--cov=acvm", "npm ci", "npm run lint", "npm run build"):
        assert needle in steps, needle
    mypy = next(s for s in ci["jobs"]["python"]["steps"] if s.get("name", "").startswith("mypy"))
    assert mypy.get("continue-on-error") is True


def test_release_workflow_supply_chain() -> None:
    rel = load_yaml(ROOT / ".github" / "workflows" / "release.yml")
    job = rel["jobs"]["images"]
    assert job["permissions"]["id-token"] == "write" and job["permissions"]["packages"] == "write"
    steps = job["steps"]
    uses = [s.get("uses", "") for s in steps]
    assert any(u.startswith("docker/setup-buildx-action") for u in uses)
    assert any(u.startswith("anchore/sbom-action") for u in uses)
    assert any(u.startswith("sigstore/cosign-installer") for u in uses)
    gate = next(s for s in steps if s.get("name", "").startswith("Trivy gate"))
    assert gate["with"]["exit-code"] == "1" and gate["with"]["severity"] == "HIGH,CRITICAL"
    names = [s.get("name", "") for s in steps]
    assert names.index(gate["name"]) < names.index("build & push"), "scan must gate the push"
    assert "cosign sign --yes" in json.dumps(steps)
    assert rel["env"]["REGISTRY"] == "ghcr.io"
