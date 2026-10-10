"""Prometheus metrics — names are a contract with deploy/prometheus rules and Grafana dashboards.

SLO mapping (§9.2):
  plan p95 ≤ 20 s            ← acvm_command_plan_seconds
  text cmd→publish p95 ≤ 3 m ← acvm_command_to_publish_seconds{kind="text"}
  short cmd→publish p95 ≤ 10 m (HOLD excluded) ← acvm_command_to_publish_seconds{kind="short"}
  publish success ≥ 99 %     ← acvm_publish_attempts_total{outcome}
  attribution freshness      ← acvm_attribution_lag_seconds{source}
  gate rule regressions = 0  ← acvm_gate_rule_regressions_total
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

_LAT = (0.5, 1, 2, 5, 10, 20, 30, 60, 120, 180, 300, 600, 900, 1800)

COMMAND_PLAN_SECONDS = Histogram("acvm_command_plan_seconds", "NL command → execution plan latency",
                                 buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 40))
COMMAND_TO_PUBLISH_SECONDS = Histogram("acvm_command_to_publish_seconds",
                                       "Command accepted → first publication PUBLISHED", ["kind"], buckets=_LAT)
STAGE_DURATION_SECONDS = Histogram("acvm_stage_duration_seconds", "Pipeline stage duration", ["stage"],
                                   buckets=_LAT)
RENDER_SECONDS = Histogram("acvm_render_seconds", "Short render wall time", buckets=(5, 10, 20, 30, 45, 60, 90, 120,
                                                                                    180, 300))
PUBLISH_ATTEMPTS = Counter("acvm_publish_attempts_total", "Publish attempts", ["platform", "outcome"])
GATE_VERDICTS = Counter("acvm_gate_verdicts_total", "M7 verdicts", ["verdict", "stage"])
GATE_RULE_FINDINGS = Counter("acvm_gate_rule_findings_total", "Findings per rule", ["rule_id", "verdict"])
GATE_RULE_REGRESSIONS = Counter("acvm_gate_rule_regressions_total",
                                "Content reached distribution without a PASS check (must stay 0)")
QUEUE_DEPTH = Gauge("acvm_queue_depth", "Items waiting", ["queue"])
ATTRIBUTION_LAG_SECONDS = Gauge("acvm_attribution_lag_seconds", "Age of newest attributed data", ["source"])
LLM_COST_KRW = Counter("acvm_llm_cost_krw_total", "LLM spend (KRW)", ["model", "purpose"])
LLM_TOKENS = Counter("acvm_llm_tokens_total", "LLM tokens", ["model", "kind"])
ACCOUNT_STATUS = Gauge("acvm_account_status", "Accounts by status", ["platform", "status"])
INVALID_CLICK_ALERTS = Counter("acvm_invalid_click_alerts_total", "Invalid-click detections", ["reason"])
BUDGET_SPENT_KRW = Gauge("acvm_budget_spent_krw", "Spend today (KRW)", ["workspace"])
COUPANG_CALLS = Counter("acvm_coupang_calls_total", "Coupang Partners API calls", ["endpoint", "outcome"])
CIRCUIT_STATE = Gauge("acvm_circuit_open", "1 when a circuit breaker is open", ["name"])
BUDGET_LIMIT_KRW = Gauge("acvm_budget_limit_krw", "Daily budget (KRW) as set in the console", ["workspace"])
