from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from acvm.agents.commander import parse_rule_based
from acvm.context import AppContext
from acvm.core.clock import KST
from acvm.core.enums import ContentKind, ContentStatus, Intent, Platform
from acvm.core.ids import uuid7
from acvm.orchestration.cron import Cron
from acvm.orchestration.planner import build_plan

SRC = Path(__file__).resolve().parents[2] / "src" / "acvm"


# ───────────────────────── Commander (§5.3 slot grammar) ─────────────────────────


def test_appendix_a_command() -> None:
    s = parse_rule_based("주방 카테고리로 쿠팡 파트너스 5개, 30대 여자 페르소나로 쇼츠 만들어서 스레드랑 인스타에 올려줘")
    assert s.intent is Intent.CREATE_AND_PUBLISH
    assert s.category == "1013" and s.count == 5 and s.kind is ContentKind.SHORT
    assert s.persona and s.persona.startswith("30대 여자")
    assert s.platforms == [Platform.THREADS, Platform.INSTAGRAM]
    assert s.schedule == "now"


def test_original_script_command() -> None:
    s = parse_rule_based("분석기 이용해서 쿠팡 파트너스 다섯 개 올려줘")
    assert s.count == 5 and s.intent is Intent.CREATE_AND_PUBLISH


@pytest.mark.parametrize(("text", "field", "expected"), [
    ("매일 오전 9시에 차량용품 10개", "schedule", "daily@09:00"),
    ("매일 오후 8시 30분에 주방 3개", "schedule", "daily@20:30"),
    ("오늘 밤에 정리수납 3개 올려줘", "schedule", "tonight"),
    ("2천 원 이내로 주방 5개", "budget_krw", 2000.0),
    ("예산 3만원으로 차량 5개", "budget_krw", 30000.0),
    ("차량 3개 글로 만들어서 스레드에 올려줘", "kind", ContentKind.TEXT),
    ("주방 5개 만들기만 해줘", "intent", Intent.CREATE_ONLY),
    ("지난주 성과 분석해줘", "intent", Intent.ANALYZE),
    ("열다섯 개 만들어서 올려줘", "count", 15),
    ("차량 쇼츠 3개 유튜브 쇼츠랑 틱톡에", "platforms", [Platform.YOUTUBE, Platform.TIKTOK]),
    ("'밀봉 클립' 관련 주방 3개", "keyword", "밀봉 클립"),
])
def test_slot_grammar(text: str, field: str, expected: object) -> None:
    assert getattr(parse_rule_based(text), field) == expected


# ───────────────────────── Planner ─────────────────────────


async def test_plan_matches_appendix_a(ctx: AppContext, workspace, accounts) -> None:  # type: ignore[no-untyped-def]
    slots = parse_rule_based("주방 카테고리로 쿠팡 파트너스 5개, 30대 여자 페르소나로 쇼츠 만들어서 스레드랑 인스타에 올려줘")
    plan = await build_plan(ctx, workspace_id=workspace.id, run_id=uuid7(), slots=slots)
    assert plan.count == 5 and plan.kind is ContentKind.SHORT and plan.persona == "30대 여성 직장인"
    assert plan.template_id == "problem-solution-v3" and plan.voice_id == "ko-F-30s-brisk"
    assert [(t.platform, t.link_placement, t.format) for t in plan.targets] == [
        (Platform.THREADS, "body", "text+video"), (Platform.INSTAGRAM, "hub", "reels")]
    assert plan.est_cost_krw == 750 and plan.budget_cap_krw == 2000  # "est. 750원, cap 2,000원"
    assert plan.schedule_window == (9, 23) and plan.jitter_minutes == 7
    assert [s.module for s in plan.steps] == ["M1", "M3", "M4", "M6", "M7", "M8"]
    # fresh workspace: template unverified + new category → console approval (§5.2 자동 승인 조건)
    assert plan.approval.mode == "console"
    assert any("템플릿" in r for r in plan.approval.reasons) and any("신규 카테고리" in r for r in plan.approval.reasons)


async def test_plan_auto_approves_when_conditions_hold(ctx: AppContext, workspace, accounts, product_factory) -> None:  # type: ignore[no-untyped-def]
    from acvm.core.enums import HookType
    from acvm.domain.models import Content, Script

    async with ctx.uow() as r:
        await r.products.upsert(product_factory())
        for _ in range(10):
            await r.contents.add(Content(
                workspace_id=workspace.id, product_id=1001, kind=ContentKind.SHORT, persona="p",
                template_id="problem-solution-v3", status=ContentStatus.APPROVED, created_at=ctx.clock.now(),
                script=Script(hooks=["h"], hook_type=HookType.QUESTION, persona="p", template_id="problem-solution-v3")))
    plan = await build_plan(ctx, workspace_id=workspace.id, run_id=uuid7(),
                            slots=parse_rule_based("주방 5개 쇼츠로 스레드랑 인스타에 올려줘"))
    assert plan.approval.mode == "auto", plan.approval.reasons


async def test_plan_text_kind_drops_video_platforms_and_flags_tiktok(ctx: AppContext, workspace, accounts) -> None:  # type: ignore[no-untyped-def]
    plan = await build_plan(ctx, workspace_id=workspace.id, run_id=uuid7(),
                            slots=parse_rule_based("주방 3개 글로 스레드랑 인스타에 올려줘"))
    assert [t.platform for t in plan.targets] == [Platform.THREADS]
    assert "제외" in plan.explanation
    plan2 = await build_plan(ctx, workspace_id=workspace.id, run_id=uuid7(),
                             slots=parse_rule_based("주방 쇼츠 2개 틱톡에 올려줘"))
    assert "SELF_ONLY" in plan2.explanation


async def test_plan_budget_and_recurring(ctx: AppContext, workspace, accounts) -> None:  # type: ignore[no-untyped-def]
    plan = await build_plan(ctx, workspace_id=workspace.id, run_id=uuid7(),
                            slots=parse_rule_based("매일 오전 9시에 주방 쇼츠 20개 500원 이내로 인스타에"))
    assert plan.recurring_cron == "0 9 * * *"
    assert plan.est_cost_krw == 3000 and plan.budget_cap_krw == 500
    assert plan.approval.mode == "console" and any("추정 비용" in r for r in plan.approval.reasons)


# ───────────────────────── cron ─────────────────────────


def test_cron_kst() -> None:
    c = Cron("0 9 * * *")
    nine_kst = datetime(2026, 10, 9, 0, 0, tzinfo=UTC)  # 09:00 KST
    assert c.matches(nine_kst) and not c.matches(nine_kst + timedelta(minutes=1))
    nxt = c.next_after(datetime(2026, 10, 8, 1, 0, tzinfo=UTC))
    assert nxt.astimezone(KST).hour == 9 and nxt.astimezone(KST).day == 9
    weekday = Cron("30 20 * * 1-5")
    sat = datetime(2026, 10, 10, 11, 30, tzinfo=UTC)  # Saturday 20:30 KST
    assert not weekday.matches(sat) and weekday.matches(sat - timedelta(days=1))
    with pytest.raises(ValueError):
        Cron("61 * * * *")


# ───────────────────────── architecture (§4: modules connect only through events) ─────────────────────────


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
        elif isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
    return out


def test_modules_never_import_each_other() -> None:
    violations = []
    for mod_dir in sorted((SRC / "modules").glob("m*_*")):
        me = f"acvm.modules.{mod_dir.name}"
        for py in mod_dir.rglob("*.py"):
            for imp in _imports(py):
                if imp.startswith("acvm.modules.") and not imp.startswith(me):
                    violations.append(f"{py.relative_to(SRC)} → {imp}")
    assert not violations, "cross-module imports:\n" + "\n".join(violations)


def test_modules_do_not_reach_control_plane() -> None:
    bad = []
    for py in (SRC / "modules").rglob("*.py"):
        for imp in _imports(py):
            if imp.startswith(("acvm.orchestration", "acvm.api", "acvm.agents", "acvm.cli")):
                bad.append(f"{py.relative_to(SRC)} → {imp}")
    assert not bad, "\n".join(bad)


def test_optimizer_has_no_write_path_to_gate_rules() -> None:
    """§8: no learning result may relax the gate — M10 must not import gate code or mutate GatePolicy."""
    m10 = SRC / "modules" / "m10_optimizer"
    if not m10.exists():
        pytest.skip("m10 not built yet")
    for py in m10.rglob("*.py"):
        src = py.read_text(encoding="utf-8")
        assert "m7_gate" not in src and "settings.gate." not in src.replace("settings.gate.price", "")
