"""Generate the provisioned Grafana dashboards (deploy/grafana/dashboards/*.json).

    uv run --no-sync python deploy/grafana/build_dashboards.py          # rewrite the JSON files
    uv run --no-sync python deploy/grafana/build_dashboards.py --check  # exit 1 if the files are stale

§9.3 views: money (머니), pipeline (파이프라인), risk (리스크), learning (학습) + an SLO overview (§9.2).
Prometheus metric names are the contract in src/acvm/core/metrics.py (tests/unit/test_ops_configs.py checks it);
ClickHouse tables come from deploy/clickhouse/schema.sql, PostgreSQL tables from src/acvm/db/tables.py.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

OUT_DIR = Path(__file__).resolve().parent / "dashboards"

PROM = {"type": "prometheus", "uid": "prometheus"}
LOKI = {"type": "loki", "uid": "loki"}
PG = {"type": "grafana-postgresql-datasource", "uid": "acvm-postgres"}
CH = {"type": "grafana-clickhouse-datasource", "uid": "acvm-clickhouse"}
EXPR = {"type": "__expr__", "uid": "__expr__"}

ISTAR_DEFAULT = "700"  # §2.3 break-even views I* (example assumptions; replace with measured value)

Json = dict[str, Any]


class Board:
    def __init__(self, uid: str, title: str, tags: list[str], description: str, *, time_from: str = "now-24h",
                 refresh: str = "1m") -> None:
        self.uid, self.title, self.tags, self.description = uid, title, tags, description
        self.time_from, self.refresh = time_from, refresh
        self.panels: list[Json] = []
        self.variables: list[Json] = []
        self._y = 0
        self._x = 0
        self._row_h = 0

    # ── layout ──
    def row(self, title: str) -> None:
        self._newline()
        self.panels.append({"id": len(self.panels) + 1, "type": "row", "title": title, "collapsed": False,
                            "gridPos": {"h": 1, "w": 24, "x": 0, "y": self._y}, "panels": []})
        self._y += 1

    def _newline(self) -> None:
        if self._x:
            self._y += self._row_h
        self._x, self._row_h = 0, 0

    def add(self, panel: Json, w: int, h: int) -> None:
        if self._x + w > 24:
            self._newline()
        panel["id"] = len(self.panels) + 1
        panel["gridPos"] = {"h": h, "w": w, "x": self._x, "y": self._y}
        self.panels.append(panel)
        self._x += w
        self._row_h = max(self._row_h, h)

    def build(self) -> Json:
        return {
            "uid": self.uid,
            "title": self.title,
            "description": self.description,
            "tags": ["acvm", *self.tags],
            "timezone": "Asia/Seoul",
            "editable": False,
            "graphTooltip": 1,
            "schemaVersion": 39,
            "version": 1,
            "refresh": self.refresh,
            "time": {"from": self.time_from, "to": "now"},
            "fiscalYearStartMonth": 0,
            "liveNow": False,
            "annotations": {"list": []},
            "links": [{"type": "dashboards", "tags": ["acvm"], "asDropdown": True, "title": "ACVM",
                       "includeVars": False, "keepTime": True}],
            "templating": {"list": self.variables},
            "panels": self.panels,
        }


# ─────────────────────────── targets ───────────────────────────


def prom(expr: str, legend: str = "", *, ref: str = "A", instant: bool = False, fmt: str | None = None) -> Json:
    t: Json = {"refId": ref, "datasource": PROM, "expr": expr, "legendFormat": legend or "__auto",
               "range": not instant, "instant": instant, "editorMode": "code"}
    if fmt:
        t["format"] = fmt
    return t


def pg(sql: str, *, ref: str = "A", fmt: str = "table") -> Json:
    return {"refId": ref, "datasource": PG, "editorMode": "code", "format": fmt, "rawQuery": True,
            "rawSql": sql.strip()}


def ch(sql: str, *, ref: str = "A", table: bool = True) -> Json:
    return {"refId": ref, "datasource": CH, "editorType": "sql", "format": 1 if table else 0,
            "queryType": "table" if table else "timeseries", "rawSql": sql.strip()}


def math(expression: str, *, ref: str) -> Json:
    return {"refId": ref, "datasource": EXPR, "type": "math", "expression": expression, "hide": False}


# ─────────────────────────── panels ───────────────────────────


def _thresholds(steps: list[tuple[str, float | None]]) -> Json:
    return {"mode": "absolute", "steps": [{"color": c, "value": v} for c, v in steps]}


def stat(title: str, targets: list[Json], *, unit: str = "short", steps: list[tuple[str, float | None]] | None = None,
         description: str = "", decimals: int | None = None, datasource: Json | None = None) -> Json:
    defaults: Json = {"unit": unit, "thresholds": _thresholds(steps or [("text", None)]),
                      "color": {"mode": "thresholds"}}
    if decimals is not None:
        defaults["decimals"] = decimals
    return {"type": "stat", "title": title, "description": description,
            "datasource": datasource or targets[0]["datasource"], "targets": targets,
            "fieldConfig": {"defaults": defaults, "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "colorMode": "background", "graphMode": "area", "justifyMode": "center",
                        "textMode": "value", "orientation": "auto", "wideLayout": True}}


def timeseries(title: str, targets: list[Json], *, unit: str = "short", description: str = "",
               steps: list[tuple[str, float | None]] | None = None, stack: bool = False, bars: bool = False,
               min_: float | None = None, max_: float | None = None, datasource: Json | None = None) -> Json:
    custom: Json = {"drawStyle": "bars" if bars else "line", "lineWidth": 1, "fillOpacity": 60 if bars else 12,
                    "showPoints": "never", "spanNulls": True, "axisSoftMin": 0,
                    "stacking": {"mode": "normal" if stack else "none", "group": "A"},
                    "thresholdsStyle": {"mode": "line+area" if steps else "off"}}
    defaults: Json = {"unit": unit, "custom": custom, "color": {"mode": "palette-classic"},
                      "thresholds": _thresholds(steps or [("green", None)])}
    if min_ is not None:
        defaults["min"] = min_
    if max_ is not None:
        defaults["max"] = max_
    return {"type": "timeseries", "title": title, "description": description,
            "datasource": datasource or targets[0]["datasource"], "targets": targets,
            "fieldConfig": {"defaults": defaults, "overrides": []},
            "options": {"legend": {"displayMode": "table", "placement": "bottom", "calcs": ["lastNotNull", "max"]},
                        "tooltip": {"mode": "multi", "sort": "desc"}}}


def table(title: str, targets: list[Json], *, description: str = "", unit: str = "short",
          datasource: Json | None = None) -> Json:
    return {"type": "table", "title": title, "description": description,
            "datasource": datasource or targets[0]["datasource"], "targets": targets,
            "fieldConfig": {"defaults": {"unit": unit, "custom": {"align": "auto", "filterable": True}},
                            "overrides": []},
            "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}}}


def pie(title: str, targets: list[Json], *, description: str = "") -> Json:
    return {"type": "piechart", "title": title, "description": description, "datasource": targets[0]["datasource"],
            "targets": targets, "fieldConfig": {"defaults": {"unit": "short"}, "overrides": []},
            "options": {"pieType": "donut", "legend": {"displayMode": "table", "placement": "right",
                                                         "values": ["value", "percent"]},
                        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}}}


def bargauge(title: str, targets: list[Json], *, unit: str = "short", description: str = "",
             steps: list[tuple[str, float | None]] | None = None, max_: float | None = None) -> Json:
    defaults: Json = {"unit": unit, "thresholds": _thresholds(steps or [("green", None)]),
                      "color": {"mode": "thresholds"}}
    if max_ is not None:
        defaults["max"] = max_
    return {"type": "bargauge", "title": title, "description": description, "datasource": targets[0]["datasource"],
            "targets": targets, "fieldConfig": {"defaults": defaults, "overrides": []},
            "options": {"orientation": "horizontal", "displayMode": "gradient", "showUnfilled": True,
                        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}}}


def heatmap(title: str, targets: list[Json], *, description: str = "") -> Json:
    return {"type": "heatmap", "title": title, "description": description, "datasource": targets[0]["datasource"],
            "targets": targets, "fieldConfig": {"defaults": {}, "overrides": []},
            "options": {"calculate": False, "yAxis": {"unit": "s"}, "cellGap": 1,
                        "color": {"mode": "scheme", "scheme": "Oranges", "steps": 64},
                        "tooltip": {"mode": "single", "yHistogram": True}}}


def state_timeline(title: str, targets: list[Json], *, description: str = "") -> Json:
    return {"type": "state-timeline", "title": title, "description": description,
            "datasource": targets[0]["datasource"], "targets": targets,
            "fieldConfig": {"defaults": {"color": {"mode": "thresholds"},
                                         "thresholds": _thresholds([("green", None), ("red", 1)]),
                                         "mappings": [{"type": "value", "options": {
                                             "0": {"text": "closed", "color": "green"},
                                             "1": {"text": "OPEN", "color": "red"}}}]},
                            "overrides": []},
            "options": {"showValue": "never", "mergeValues": True, "rowHeight": 0.8}}


def logs(title: str, expr: str, *, description: str = "") -> Json:
    return {"type": "logs", "title": title, "description": description, "datasource": LOKI,
            "targets": [{"refId": "A", "datasource": LOKI, "expr": expr, "queryType": "range"}],
            "options": {"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending",
                        "enableLogDetails": True, "dedupStrategy": "none"}}


def text(title: str, content: str) -> Json:
    return {"type": "text", "title": title, "options": {"mode": "markdown", "content": content.strip()}}


# ─────────────────────────── dashboards ───────────────────────────

GREEN_BELOW = lambda limit: [("green", None), ("red", limit)]  # noqa: E731 — latency/lag style
RED_BELOW = lambda limit: [("red", None), ("green", limit)]  # noqa: E731 — ratio style


def slo_overview() -> Json:
    b = Board("acvm-slo", "ACVM · SLO 개요", ["slo"], "§9.2 SLO 목표 대비 현재값 (Prometheus recording rules acvm:*)")
    b.row("§9.2 SLO — 현재값 (빨강 = 목표 위반)")
    b.add(stat("명령→계획 p95 (1h)", [prom("acvm:command_plan_seconds:p95_1h", instant=True)], unit="s",
               steps=GREEN_BELOW(20), description="목표 p95 ≤ 20 s"), 4, 4)
    b.add(stat("텍스트 명령→게시 p95 (1h)", [prom('acvm:command_to_publish_seconds:p95_1h{kind="text"}',
                                               instant=True)], unit="s", steps=GREEN_BELOW(180),
               description="목표 p95 ≤ 3 min"), 4, 4)
    b.add(stat("쇼츠 명령→게시 p95 (1h)", [prom('acvm:command_to_publish_seconds:p95_1h{kind="short"}',
                                              instant=True)], unit="s", steps=GREEN_BELOW(600),
               description="목표 p95 ≤ 10 min (HOLD 제외)"), 4, 4)
    b.add(stat("게시 성공률 (1h)", [prom("acvm:publish_success:ratio_rate1h", instant=True)], unit="percentunit",
               steps=RED_BELOW(0.99), decimals=2, description="목표 ≥ 99 % (플랫폼 정책 거절 제외)"), 4, 4)
    b.add(stat("플랫폼 지표 신선도", [prom('acvm:attribution_lag_seconds:max{source="platform"}', instant=True)],
               unit="s", steps=GREEN_BELOW(3600), description="목표 ≤ 1 h"), 4, 4)
    b.add(stat("쿠팡 리포트 신선도", [prom('acvm:attribution_lag_seconds:max{source="coupang"}', instant=True)],
               unit="s", steps=GREEN_BELOW(129600), description="목표 D+1 (≤ 36 h)"), 4, 4)
    b.add(stat("게이트 규칙 누락 (24h)", [prom("acvm:gate_rule_regressions:increase24h or vector(0)", instant=True)],
               steps=GREEN_BELOW(1), decimals=0, description="D·C-04·R-01 누락 0건 — 0이 아니면 즉시 게시 중지"), 6, 4)
    b.add(stat("발생 중 page 경보", [prom('count(ALERTS{alertstate="firing", severity="page"}) or vector(0)',
                                       instant=True)], steps=GREEN_BELOW(1), decimals=0), 6, 4)
    b.add(stat("발생 중 ticket 경보", [prom('count(ALERTS{alertstate="firing", severity="ticket"}) or vector(0)',
                                         instant=True)], steps=[("green", None), ("orange", 1)], decimals=0), 6, 4)
    b.add(stat("게시 시도 (1h, 정책 거절 제외)", [prom("acvm:publish_eligible_attempts:increase1h", instant=True)],
               decimals=0), 6, 4)
    b.row("추세")
    b.add(timeseries("명령→계획 p95", [prom("acvm:command_plan_seconds:p95_5m", "p95 5m"),
                                     prom("acvm:command_plan_seconds:p95_1h", "p95 1h", ref="B")],
                     unit="s", steps=GREEN_BELOW(20)), 12, 8)
    b.add(timeseries("명령→게시 p95 (kind)", [prom("acvm:command_to_publish_seconds:p95_1h", "{{kind}}")],
                     unit="s", steps=[("green", None), ("orange", 180), ("red", 600)],
                     description="text ≤ 180 s, short ≤ 600 s"), 12, 8)
    b.add(timeseries("게시 성공률 (플랫폼별, 1h)", [prom("acvm:publish_success:ratio_rate1h_by_platform",
                                                   "{{platform}}")], unit="percentunit", steps=RED_BELOW(0.99),
                     min_=0.9, max_=1), 12, 8)
    b.add(timeseries("오류 예산 소진 속도 (×, 1.0 = 정확히 소진)", [
        prom("(1 - acvm:publish_success:ratio_rate1h) / 0.01", "1h"),
        prom("(1 - acvm:publish_success:ratio_rate6h) / 0.01", "6h", ref="B")],
        steps=[("green", None), ("orange", 6), ("red", 14.4)],
        description="multi-window burn rate: 14.4× (1h) page, 6× (6h) ticket"), 12, 8)
    b.add(timeseries("수익 귀속 지연 (source)", [prom("acvm:attribution_lag_seconds:max", "{{source}}")], unit="s",
                     steps=[("green", None), ("orange", 3600), ("red", 129600)]), 12, 8)
    b.add(table("발생 중 경보", [prom('ALERTS{alertstate="firing"}', instant=True, fmt="table")]), 12, 8)
    return b.build()


def money() -> Json:
    b = Board("acvm-money", "ACVM · 머니 뷰", ["money"],
              "§9.3 머니 뷰 — GMV / 확정 수수료 / 취소 차감 / 순이익을 반드시 분리 표시 (§2.4). "
              "순이익 = 확정 수수료 − 취소 차감 − 변동비(cost_ledger), 인프라 고정비 제외.", time_from="now-30d",
              refresh="15m")
    b.variables.append({"type": "textbox", "name": "istar", "label": "I* 손익분기 조회수", "query": ISTAR_DEFAULT,
                        "current": {"text": ISTAR_DEFAULT, "value": ISTAR_DEFAULT}, "hide": 0})
    report = "SELECT {col} FROM acvm.affiliate_report FINAL WHERE $__dateFilter(date)"
    b.row("기간 합계 (KRW)")
    b.add(stat("GMV (상품 판매액)", [ch(report.format(col="sum(gmv_krw) AS gmv"))], unit="currencyKRW",
               description="GMV는 수익이 아니다 (§2.4)"), 5, 4)
    b.add(stat("확정 수수료", [ch(report.format(col="sum(commission_krw) AS commission"))], unit="currencyKRW"), 5, 4)
    b.add(stat("취소·반품 차감", [ch(report.format(col="sum(cancelled_krw) AS cancelled"))], unit="currencyKRW"), 4, 4)
    b.add(stat("변동비 (LLM·TTS·렌더)", [pg("SELECT coalesce(sum(amount_krw), 0) AS cost FROM cost_ledger "
                                         "WHERE $__timeFilter(at)")], unit="currencyKRW"), 5, 4)
    b.add(stat("순이익 (변동비 기준)", [
        {**ch(report.format(col="toFloat64(sum(commission_krw)) AS v"), ref="COMM"), "hide": True},
        {**ch(report.format(col="toFloat64(sum(cancelled_krw)) AS v"), ref="CANC"), "hide": True},
        {**pg("SELECT coalesce(sum(amount_krw), 0)::float8 AS v FROM cost_ledger WHERE $__timeFilter(at)",
              ref="COST"), "hide": True},
        math("$COMM - $CANC - $COST", ref="NET")],
        unit="currencyKRW", steps=[("red", None), ("green", 0)], datasource=EXPR), 5, 4)
    b.row("일별 추이")
    b.add(timeseries("일별 GMV / 확정 수수료 / 취소 차감", [ch(
        "SELECT toDateTime(date) AS time, sum(gmv_krw) AS GMV, sum(commission_krw) AS commission, "
        "sum(cancelled_krw) AS cancelled FROM acvm.affiliate_report FINAL WHERE $__dateFilter(date) "
        "GROUP BY date ORDER BY date", table=False)], unit="currencyKRW", bars=True), 12, 9)
    b.add(timeseries("일별 변동비 (stage)", [pg(
        "SELECT date_trunc('day', at AT TIME ZONE 'Asia/Seoul') AS time, stage AS metric, sum(amount_krw) AS value "
        "FROM cost_ledger WHERE $__timeFilter(at) GROUP BY 1, 2 ORDER BY 1", fmt="time_series")],
        unit="currencyKRW", bars=True, stack=True), 12, 9)
    b.row("채널 효율 (§2.3 KPI 1순위: 중앙값 조회수 ÷ I*)")
    b.add(table("플랫폼별 중앙값 조회수 ÷ I*", [ch(
        "SELECT platform, count() AS publications, quantileExact(0.5)(views) AS median_views, "
        "round(median_views / ${istar}, 2) AS median_over_istar FROM ("
        " SELECT platform, publication_id, argMax(value, (ts, version)) AS views FROM acvm.perf_event"
        " WHERE metric = 'views' AND $__timeFilter(ts) GROUP BY platform, publication_id)"
        " GROUP BY platform ORDER BY median_over_istar DESC")],
        description="≥ 1 이면 콘텐츠를 늘릴수록 이익이 선형 이상 증가 (§2.3). 계정 단위는 콘솔 리포트 참조."), 12, 8)
    b.add(table("플랫폼별 수수료 (기간)", [ch(
        "SELECT pl.platform AS platform, sum(r.gmv_krw) AS gmv_krw, sum(r.commission_krw) AS commission_krw, "
        "sum(r.cancelled_krw) AS cancelled_krw, sum(r.clicks) AS clicks, sum(r.orders) AS orders, "
        "round(sum(r.commission_krw) / greatest(sum(r.clicks), 1), 1) AS epc_krw "
        "FROM (SELECT * FROM acvm.affiliate_report FINAL WHERE $__dateFilter(date)) AS r "
        "INNER JOIN (SELECT sub_id, platform FROM acvm.publication_link FINAL) AS pl ON pl.sub_id = r.sub_id "
        "GROUP BY platform ORDER BY commission_krw DESC")], unit="short"), 12, 8)
    b.add(table("콘텐츠 ROI 상위 25 (누적, subId 1:1)", [ch(
        "SELECT platform, sub_id, content_id, views, report_clicks, orders, gmv_krw, commission_krw, "
        "cancelled_krw, net_commission_krw, round(epc_krw, 1) AS epc_krw FROM acvm.content_roi "
        "ORDER BY net_commission_krw DESC LIMIT 25")]), 24, 9)
    return b.build()


def pipeline() -> Json:
    b = Board("acvm-pipeline", "ACVM · 파이프라인 뷰", ["pipeline"],
              "§9.3 파이프라인 뷰 — 단계별 처리량·대기열 길이·실패율, 렌더 시간 분포", time_from="now-6h", refresh="30s")
    b.row("처리량 · 지연")
    b.add(timeseries("단계별 처리량 (건/분)", [prom("sum by (stage) (rate(acvm_stage_duration_seconds_count[5m])) * 60",
                                             "{{stage}}")]), 12, 8)
    b.add(timeseries("단계별 소요시간 p95", [prom("acvm:stage_duration_seconds:p95_15m", "{{stage}}")], unit="s"),
          12, 8)
    b.add(timeseries("대기열 길이", [prom("max by (queue) (acvm_queue_depth)", "{{queue}}")],
                     description="hold_review = HOLD 승인 대기, publication_<state> = 최근 30일 게시 상태별 건수"),
          12, 8)
    b.add(timeseries("게시 시도 결과 (outcome)", [prom(
        "sum by (outcome) (increase(acvm_publish_attempts_total[$__rate_interval]))", "{{outcome}}")],
        bars=True, stack=True), 12, 8)
    b.add(timeseries("플랫폼별 게시 실패율 (1h)", [prom("1 - acvm:publish_success:ratio_rate1h_by_platform",
                                                   "{{platform}}")], unit="percentunit", steps=GREEN_BELOW(0.01)),
          12, 8)
    b.add(timeseries("게이트 판정 (stage × verdict, /h)", [prom(
        "sum by (stage, verdict) (increase(acvm_gate_verdicts_total[1h]))", "{{stage}} {{verdict}}")]), 12, 8)
    b.row("렌더 (M6: 30초 영상 CPU ≤ 60 s, 동시 8건)")
    b.add(heatmap("렌더 시간 분포", [prom("sum by (le) (increase(acvm_render_seconds_bucket[$__rate_interval]))",
                                     "{{le}}", fmt="heatmap")]), 12, 8)
    b.add(timeseries("렌더 p50 / p95", [
        prom("histogram_quantile(0.5, sum by (le) (rate(acvm_render_seconds_bucket[15m])))", "p50"),
        prom("histogram_quantile(0.95, sum by (le) (rate(acvm_render_seconds_bucket[15m])))", "p95", ref="B")],
        unit="s", steps=GREEN_BELOW(60)), 12, 8)
    b.row("비용 · 외부 API")
    b.add(timeseries("LLM 비용 (KRW/h, purpose)", [prom("acvm:llm_cost_krw:increase1h", "{{purpose}}")],
                     unit="currencyKRW", stack=True), 8, 8)
    b.add(timeseries("LLM 토큰 (/s, model·kind)", [prom("sum by (model, kind) (rate(acvm_llm_tokens_total[5m]))",
                                                    "{{model}} {{kind}}")]), 8, 8)
    b.add(bargauge("일 예산 사용률 (workspace)", [prom("acvm:budget_used:ratio", "{{workspace}}", instant=True)],
                   unit="percentunit", steps=[("green", None), ("orange", 0.9), ("red", 1)], max_=1), 8, 8)
    b.add(timeseries("쿠팡 API 호출 (endpoint·outcome, /h)", [prom(
        "sum by (endpoint, outcome) (increase(acvm_coupang_calls_total[1h]))", "{{endpoint}} {{outcome}}")],
        description="검색 운영 상한 8회/시간 (§7). forbidden(403) 발생 시 즉시 중단 (§9.5-2)"), 24, 8)
    return b.build()


def risk() -> Json:
    b = Board("acvm-risk", "ACVM · 리스크 뷰", ["risk"],
              "§9.3 리스크 뷰 — 계정 상태(active/cooldown/restricted), 게이트 판정 분포, 무효 클릭 경보",
              time_from="now-7d", refresh="1m")
    b.row("계정 상태 (§7.1 쿨다운 자동 전환)")
    b.add(stat("active", [prom('sum(acvm:accounts:by_status{status="active"}) or vector(0)', instant=True)],
               steps=[("green", None)], decimals=0), 4, 4)
    b.add(stat("cooldown", [prom('sum(acvm:accounts:by_status{status="cooldown"}) or vector(0)', instant=True)],
               steps=[("green", None), ("orange", 1)], decimals=0), 4, 4)
    b.add(stat("restricted", [prom('sum(acvm:accounts:by_status{status="restricted"}) or vector(0)',
                                   instant=True)], steps=GREEN_BELOW(1), decimals=0), 4, 4)
    b.add(stat("게이트 누락 (24h)", [prom("acvm:gate_rule_regressions:increase24h or vector(0)", instant=True)],
               steps=GREEN_BELOW(1), decimals=0), 4, 4)
    b.add(stat("무효 클릭 경보 (24h)", [prom("sum(increase(acvm_invalid_click_alerts_total[24h])) or vector(0)",
                                         instant=True)], steps=[("green", None), ("orange", 1), ("red", 10)],
               decimals=0), 4, 4)
    b.add(stat("쿠팡 403 (24h)", [prom('sum(increase(acvm_coupang_calls_total{outcome="forbidden"}[24h])) '
                                       'or vector(0)', instant=True)], steps=GREEN_BELOW(1), decimals=0), 4, 4)
    b.add(timeseries("계정 상태 추이 (platform·status)", [prom("acvm:accounts:by_platform_status",
                                                         "{{platform}} {{status}}")], stack=True), 12, 8)
    b.add(table("비활성 계정 (cooldown / restricted)", [pg(
        "SELECT platform, handle, status, cooldown_until, niche FROM channel_account "
        "WHERE status <> 'active' ORDER BY status DESC, cooldown_until")]), 12, 8)
    b.row("M7 게이트")
    b.add(pie("게이트 판정 분포 (기간)", [prom(
        'sum by (verdict) (increase(acvm_gate_verdicts_total{stage="gate"}[$__range]))', "{{verdict}}",
        instant=True)]), 8, 8)
    b.add(table("규칙별 지적 (기간)", [prom(
        "sort_desc(sum by (rule_id, verdict) (increase(acvm_gate_rule_findings_total[$__range])))",
        instant=True, fmt="table")]), 8, 8)
    b.add(table("HOLD 승인 대기", [pg(
        "SELECT created_at, content_id, reasons::text AS reasons FROM review_item "
        "WHERE status = 'pending' ORDER BY created_at LIMIT 50")]), 8, 8)
    b.row("외부 리스크 · 무효 클릭")
    b.add(state_timeline("서킷 브레이커", [prom("max by (name) (acvm_circuit_open)", "{{name}}")]), 12, 8)
    b.add(timeseries("무효 클릭 경보 (reason, /h)", [prom(
        "sum by (reason) (increase(acvm_invalid_click_alerts_total[1h]))", "{{reason}}")], bars=True), 12, 8)
    b.add(table("계정 경고·실패 이벤트", [pg(
        "SELECT e.at AS time, a.platform, a.handle, e.kind, e.value, e.detail::text AS detail "
        "FROM account_health_event e JOIN channel_account a ON a.id = e.account_id "
        "WHERE e.kind IN ('warning', 'attempt_fail') AND $__timeFilter(e.at) ORDER BY e.at DESC LIMIT 100")]),
        12, 9)
    b.add(logs("경고·오류 로그 (acvm-*)", '{service=~"acvm-.*", level=~"warning|error|critical"}'), 12, 9)
    return b.build()


def learning() -> Json:
    b = Board("acvm-learning", "ACVM · 학습 뷰", ["learning"],
              "§9.3 학습 뷰 — arm별 사후분포, 탐색/활용 비율, 꼬리 콘텐츠 목록 (M10)", time_from="now-30d",
              refresh="15m")
    b.row("M10 밴딧 (탐색 예산 20 %, 30회 시도 후 하위 10 % 퇴출)")
    b.add(stat("활성 arm", [pg("SELECT count(*) AS arms FROM arm_posterior WHERE NOT retired")], decimals=0), 6, 4)
    b.add(stat("퇴출 arm", [pg("SELECT count(*) AS arms FROM arm_posterior WHERE retired")], decimals=0), 6, 4)
    b.add(stat("관측 (기간)", [pg("SELECT count(*) AS n FROM arm_observation WHERE $__timeFilter(observed_at)")],
               decimals=0), 6, 4)
    b.add(stat("활성 플레이북 규칙", [pg("SELECT count(*) AS n FROM playbook_rule WHERE active")], decimals=0), 6, 4)
    b.add(timeseries("탐색 비중 (추정) vs 목표 20 %", [pg(
        "SELECT date_trunc('day', o.observed_at) AS time, "
        "avg(CASE WHEN p.trials < 30 THEN 1.0 ELSE 0.0 END) AS explore_share, 0.20 AS target "
        "FROM arm_observation o JOIN arm_posterior p ON p.workspace_id = o.workspace_id AND p.arm_key = o.arm_key "
        "WHERE $__timeFilter(o.observed_at) GROUP BY 1 ORDER BY 1", fmt="time_series")],
        unit="percentunit", min_=0, max_=1,
        description="trials < 30인 arm(퇴출 판정 전)에 대한 관측 비중 — 현재 trials 기준 근사치"), 12, 8)
    b.add(table("arm 사후분포 (상위 50, 관측 EPC 순)", [pg(
        "SELECT arm_key, trials, "
        "round((ctr_alpha / NULLIF(ctr_alpha + ctr_beta, 0))::numeric, 5) AS ctr_post_mean, "
        "round((order_shape / NULLIF(order_rate, 0))::numeric, 4) AS order_post_mean, "
        "round((comm_shape / NULLIF(comm_rate, 0))::numeric, 1) AS commission_post_mean, "
        "round((commission_krw / NULLIF(clicks, 0))::numeric, 1) AS epc_observed_krw, "
        "impressions, clicks, orders, commission_krw, retired, updated_at "
        "FROM arm_posterior ORDER BY epc_observed_krw DESC NULLS LAST LIMIT 50")],
        description="CTR ~ Beta(α, β), 주문·수수료 ~ Gamma(shape, rate) — 평균 = shape / rate"), 12, 8)
    b.row("꼬리 콘텐츠 (게시 48h 내 상위 2 % → 증폭 후보)")
    b.add(table("꼬리 콘텐츠 (48h, 조회 상위 2 %)", [ch(
        "WITH latest AS (SELECT publication_id, any(content_id) AS content_id, any(platform) AS platform, "
        "argMax(value, (ts, version)) AS views, min(ts) AS first_seen FROM acvm.perf_event "
        "WHERE metric = 'views' AND ts >= now() - INTERVAL 7 DAY GROUP BY publication_id) "
        "SELECT platform, content_id, publication_id, views, first_seen FROM latest "
        "WHERE first_seen >= now() - INTERVAL 48 HOUR "
        "AND views >= (SELECT quantileExact(0.98)(views) FROM latest) ORDER BY views DESC LIMIT 30")]), 12, 9)
    b.add(table("최근 플레이북 규칙 (RAG)", [pg(
        "SELECT created_at, text, round(lift::numeric, 3) AS lift, round(confidence::numeric, 2) AS confidence "
        "FROM playbook_rule WHERE active ORDER BY created_at DESC LIMIT 20")]), 12, 9)
    return b.build()


DASHBOARDS = {
    "acvm-slo-overview.json": slo_overview,
    "acvm-money.json": money,
    "acvm-pipeline.json": pipeline,
    "acvm-risk.json": risk,
    "acvm-learning.json": learning,
}


def render() -> dict[str, str]:
    return {name: json.dumps(fn(), ensure_ascii=False, indent=2) + "\n" for name, fn in DASHBOARDS.items()}


def main(argv: list[str]) -> int:
    rendered = render()
    if "--check" in argv:
        stale = [n for n, body in rendered.items()
                 if not (OUT_DIR / n).exists() or (OUT_DIR / n).read_text(encoding="utf-8") != body]
        if stale:
            print("stale dashboards (run build_dashboards.py):", ", ".join(stale))
            return 1
        return 0
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, body in rendered.items():
        (OUT_DIR / name).write_text(body, encoding="utf-8", newline="\n")
        print("wrote", OUT_DIR / name)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
