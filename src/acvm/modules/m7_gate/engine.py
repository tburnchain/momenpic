"""M7 Compliance Gate (§4 M7, §8) — the single path from production to distribution.

    evaluate(content)   — "gate" stage after render: all content rules, in-place FIX loop (≤ 3 passes)
    preflight(pub)      — "preflight" stage seconds before publishing: caption, price freshness, hub page,
                          link integrity, account state, AI label, and the bypass check
    decide_review(item) — human HOLD decision (owner/reviewer; enforced at the API layer)

Verdict semantics:
    PASS  → content.status = approved (only approved content can be published — preflight G-00)
    FIX   → returned with ``actions`` ⊆ {regenerate, rerender, reschedule}; the workflow performs them and
            calls the gate again. In-place fixes (disclosure, wording, price) are applied by the gate
            itself and re-checked before the verdict is final.
    HOLD  → review_item queued; content.status = gated
    BLOCK → content.status = blocked; never published

No learning signal can relax a rule: the optimizer has no write path here and GatePolicy is
configuration under owner/reviewer change control (§8 preamble).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from acvm.context import AppContext
from acvm.core.enums import ContentStatus, Verdict
from acvm.core.errors import NotFound, PolicyViolation
from acvm.core.events import Event, GateDecided
from acvm.core.logging import get_logger
from acvm.core.metrics import GATE_RULE_FINDINGS, GATE_VERDICTS, STAGE_DURATION_SECONDS
from acvm.db.tables import PublicationRow
from acvm.domain.models import ComplianceCheck, Content, Finding, Publication
from acvm.modules.m7_gate.rules import RULE_FUNCS
from acvm.modules.m7_gate.ruleset import fingerprint
from acvm.modules.m7_gate.subject import GateSubject, RuleResult
from acvm.policy.facts import build_facts
from acvm.security.audit import audit

log = get_logger(__name__)


@dataclass
class GateOutcome:
    verdict: Verdict
    check: ComplianceCheck
    actions: set[str] = field(default_factory=set)
    content: Content | None = None
    publication: Publication | None = None

    @property
    def reasons(self) -> list[str]:
        return [f"{f.rule_id}: {f.message}" for f in self.check.findings if f.verdict is not Verdict.PASS and not f.fixed]


class GateEngine:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        if ctx.settings.gate.rule_set_version != "v1":
            raise PolicyViolation(f"unknown rule set {ctx.settings.gate.rule_set_version}")

    async def _run(self, subject: GateSubject) -> tuple[Verdict, list[Finding], set[str], int]:
        """Run the stage's rules; apply in-place fixes; repeat until no patch or max iterations."""
        max_it = self.ctx.settings.gate.max_fix_iterations
        fixed_log: list[Finding] = []
        actions: set[str] = set()
        last: list[Finding] = []
        iteration = 0
        for iteration in range(1, max_it + 1):  # noqa: B007 — iteration is reported
            last = []
            patched = False
            for rule in RULE_FUNCS:
                if subject.stage not in rule.stages:
                    continue
                res: RuleResult = await rule.fn(subject, self.ctx)
                for f in res.findings:
                    GATE_RULE_FINDINGS.labels(rule_id=f.rule_id, verdict=f.verdict.value).inc()
                last.extend(res.findings)
                actions |= res.actions
                patched |= res.patched
            if any(f.verdict is Verdict.BLOCK for f in last):
                break
            fixed_log.extend(f for f in last if f.fixed)
            if not patched:
                break
        else:
            # still patching after max iterations → cannot converge automatically
            last.append(Finding(rule_id="GATE", verdict=Verdict.HOLD, message="자동 수정이 수렴하지 않음 — 사람 검토"))
        open_findings = [f for f in last if not f.fixed and f.verdict is not Verdict.PASS]
        verdict = Verdict.worst([f.verdict for f in open_findings])
        if verdict is Verdict.FIX and not (actions & {"regenerate", "rerender", "reschedule"}):
            verdict = Verdict.PASS
        if verdict is Verdict.PASS and "rerender" in actions and subject.stage == "gate":
            verdict = Verdict.FIX  # narration/on-screen text changed after render — video must be re-rendered
        info = [f for f in last if f.verdict is Verdict.PASS]
        findings = _dedupe(fixed_log + open_findings + info)
        return verdict, findings, actions if verdict is Verdict.FIX else set(), iteration

    async def evaluate(self, content_id: uuid.UUID, *, run_id: uuid.UUID | None = None) -> GateOutcome:
        started = time.perf_counter()
        now = self.ctx.clock.now()
        async with self.ctx.uow() as repos:
            content = await repos.contents.get(content_id)
            if content is None:
                raise NotFound(f"content {content_id}")
            product = await repos.products.get(content.product_id)
            if product is None:
                raise NotFound(f"product {content.product_id}")
            accounts = {}
            if content.target_account_id:
                acc = await repos.accounts.get(content.target_account_id)
                if acc:
                    accounts[acc.id] = acc
            assets = await repos.assets.get_many(content.render.asset_ids if content.render else [])
        facts = build_facts(product, now, self.ctx.settings.gate.price_fresh_minutes)
        subject = GateSubject.for_content(content, product, facts, accounts, assets, now)
        verdict, findings, actions, iteration = await self._run(subject)
        patched_content, _ = subject.apply()
        patched_content = patched_content.model_copy(update={"claims": subject.content.claims})

        status = {Verdict.PASS: ContentStatus.APPROVED, Verdict.HOLD: ContentStatus.GATED,
                  Verdict.BLOCK: ContentStatus.BLOCKED}.get(verdict, patched_content.status)
        if verdict is Verdict.FIX and actions and actions <= {"reschedule"}:
            status = ContentStatus.APPROVED  # compliant content; only the publish time moves (S-02)
        patched_content = patched_content.model_copy(update={"status": status})
        check = ComplianceCheck(content_id=content.id, rule_set_version=f"v1#{fingerprint()}", verdict=verdict,
                                findings=findings, iteration=iteration, decided_at=now, stage="gate")
        async with self.ctx.uow() as repos:
            await repos.contents.save(patched_content)
            await repos.compliance.add(check)
            if verdict is Verdict.HOLD:
                await repos.compliance.enqueue_review(content.workspace_id, content.id, check.id,
                                                      [f"{f.rule_id}: {f.message}" for f in findings
                                                       if f.verdict is Verdict.HOLD], now, run_id=run_id)
            if verdict in (Verdict.HOLD, Verdict.BLOCK):
                await audit(repos, self.ctx.clock, "gate", f"gate.{verdict.value.lower()}", str(content.id),
                            findings=[f.model_dump(mode="json") for f in findings if not f.fixed])
            await self.ctx.emit(repos, Event.of(
                GateDecided(content_id=content.id, verdict=verdict, check_id=check.id,
                            findings=[f.model_dump(mode="json") for f in findings]),
                workspace_id=content.workspace_id, run_id=run_id or content.created_by_run,
                idempotency_key=f"{content.id}:gate:{check.id}"))
        GATE_VERDICTS.labels(verdict=verdict.value, stage="gate").inc()
        STAGE_DURATION_SECONDS.labels(stage="gate").observe(time.perf_counter() - started)
        log.info("gate_decided", content=str(content.id), verdict=verdict.value, actions=sorted(actions),
                 iteration=iteration)
        return GateOutcome(verdict, check, actions, patched_content)

    async def preflight(self, publication_id: uuid.UUID) -> GateOutcome:
        """Seconds before publishing. Run M1 price refresh first so C-03 sees a fresh price."""
        now = self.ctx.clock.now()
        async with self.ctx.uow() as repos:
            pub = await repos.publications.get(publication_id)
            if pub is None:
                raise NotFound(f"publication {publication_id}")
            content = await repos.contents.get(pub.content_id)
            product = await repos.products.get(content.product_id) if content else None
            account = await repos.accounts.get(pub.channel_account_id)
            link = await repos.links.get(pub.link_id)
        if content is None or product is None or account is None:
            raise NotFound("publication dependencies missing")
        facts = build_facts(product, now, self.ctx.settings.gate.price_fresh_minutes)
        subject = GateSubject.for_publication(content, product, facts, account, pub, link, now)
        verdict, findings, actions, iteration = await self._run(subject)
        _, patched_pub = subject.apply()
        check = ComplianceCheck(content_id=content.id, rule_set_version=f"v1#{fingerprint()}", verdict=verdict,
                                findings=findings, iteration=iteration, decided_at=now, stage="preflight")
        async with self.ctx.uow() as repos:
            if patched_pub is not None and (patched_pub.caption != pub.caption or patched_pub.ai_label != pub.ai_label):
                row = await repos.session.get(PublicationRow, pub.id)
                if row is not None:
                    row.caption, row.ai_label = patched_pub.caption, patched_pub.ai_label
            await repos.compliance.add(check)
            if verdict is Verdict.BLOCK:
                await audit(repos, self.ctx.clock, "gate", "preflight.block", str(pub.id),
                            findings=[f.model_dump(mode="json") for f in findings if not f.fixed])
        GATE_VERDICTS.labels(verdict=verdict.value, stage="preflight").inc()
        return GateOutcome(verdict, check, actions, content, patched_pub)

    async def decide_review(self, item_id: uuid.UUID, *, approve: bool, by: str, note: str = "") -> Content:
        """Human decision on a HOLD item (§8.4). Approval makes the content publishable; rejection blocks it."""
        now = self.ctx.clock.now()
        async with self.ctx.uow() as repos:
            item = await repos.compliance.decide_review(item_id, approve, by, now, note)
            if item is None:
                raise NotFound(f"review item {item_id}")
            content = await repos.contents.get(item.content_id)
            if content is None:
                raise NotFound(f"content {item.content_id}")
            if item.decided_by != by:  # already decided earlier — idempotent replay
                return content
            verdict = Verdict.PASS if approve else Verdict.BLOCK
            content = content.model_copy(update={"status": ContentStatus.APPROVED if approve else ContentStatus.BLOCKED})
            await repos.contents.save(content)
            check = ComplianceCheck(content_id=content.id, rule_set_version=f"v1#{fingerprint()}", verdict=verdict,
                                    findings=[Finding(rule_id="HUMAN", verdict=verdict,
                                                      message=f"{'승인' if approve else '반려'}: {note}".strip(": "))],
                                    decided_at=now, stage="human")
            await repos.compliance.add(check)
            await audit(repos, self.ctx.clock, by, "review.approve" if approve else "review.reject", str(content.id),
                        note=note, item=str(item_id))
            await self.ctx.emit(repos, Event.of(
                GateDecided(content_id=content.id, verdict=verdict, check_id=check.id,
                            findings=[f.model_dump(mode="json") for f in check.findings]),
                workspace_id=content.workspace_id, run_id=item.run_id, idempotency_key=f"{content.id}:human:{check.id}"))
        return content


def _dedupe(findings: list[Finding]) -> list[Finding]:
    seen: set[tuple[str, str, bool]] = set()
    out = []
    for f in findings:
        key = (f.rule_id, f.message, f.fixed)
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out
