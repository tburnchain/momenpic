"""Deterministic script composer — the Writer's offline strategy (no LLM configured) and the safe
fallback when an LLM draft cannot be grounded.

Every factual line is emitted *together with* its evidence keys, so composer output is grounded by
construction. Variety comes from phrase banks × persona × hook type × an RNG seeded by
(product, variant, account), which is what the diversity check (MinHash < 0.6 vs the account's last
30 days) needs.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from acvm.core.enums import ContentKind, HookType
from acvm.modules.m4_script.facts import ProductFacts
from acvm.modules.m4_script.korean import euro, ida
from acvm.modules.m4_script.templates import (
    SHORT_MAX_SECONDS,
    SHORT_MIN_SECONDS,
    Persona,
    Template,
    estimate_seconds,
)

# keyword in product name → (problem statement, amplification)
PROBLEM_BANK: tuple[tuple[tuple[str, ...], tuple[tuple[str, str], ...]], ...] = (
    (("밀봉", "클립", "집게", "봉지"), (
        ("먹다 남은 과자 봉지, 하루만 지나도 눅눅해지죠", "고무줄로 묶어두면 금방 풀리고, 결국 반은 버리게 돼요"),
        ("과자 봉지 아직도 대충 접어서 두세요", "다음 날 열어보면 바삭함이 다 날아가 있어요"),
    )),
    (("정리함", "수납", "칸막이", "트레이", "오거나이저"), (
        ("서랍 열 때마다 뭐가 어디 있는지 모르겠죠", "필요한 거 하나 찾으려고 서랍을 다 뒤집게 돼요"),
        ("정리해도 일주일이면 다시 엉망이 되지 않나요", "칸이 없으니까 물건들이 계속 섞여버려요"),
    )),
    (("틈새", "사이드", "콘솔"), (
        ("운전석 옆 틈새로 물건 떨어뜨린 적 있죠", "신호 대기 중에 손 넣어서 더듬다 보면 진짜 위험해요"),
        ("차 안에 자잘한 물건들 둘 데가 없죠", "컵홀더에 다 몰아넣다 보니 정작 컵을 못 둬요"),
    )),
    (("거치대", "홀더"), (
        ("휴대폰 둘 데가 마땅치 않아서 불편하셨죠", "손에 들고 보다가 떨어뜨리는 게 한두 번이 아니에요"),
    )),
    (("밀폐", "용기", "보관"), (
        ("남은 반찬 보관할 때마다 뚜껑 찾느라 바쁘죠", "냉장고 안이 크기 제각각인 통으로 꽉 차 있어요"),
    )),
    (("수세미", "행주", "세척", "솔"), (
        ("설거지 끝나도 개운하지 않은 날 있죠", "구석구석 닦이지 않아서 결국 손으로 한 번 더 문질러요"),
    )),
    (("걸이", "후크", "행거"), (
        ("걸어둘 곳이 없어서 물건들이 바닥에 쌓이죠", "벽에 못을 박기엔 부담스럽고요"),
    )),
    (("방수", "물받이", "매트"), (
        ("싱크대 앞 바닥이 늘 젖어 있지 않나요", "닦아도 닦아도 물 자국이 남아요"),
    )),
)

CATEGORY_PROBLEMS: dict[str, tuple[tuple[str, str], ...]] = {
    "1013": (("주방에서 매일 쓰는 건데 은근히 불편한 거 있죠", "하루에도 몇 번씩 그 불편을 참고 넘어가게 돼요"),),
    "1018": (("차 안에서 은근히 거슬리는 불편 하나쯤 있죠", "운전하면서 신경 쓰이면 그게 제일 피곤해요"),),
    "1014": (("집에서 매일 마주치는 사소한 불편 있죠", "작은 불편이라도 매일 쌓이면 스트레스예요"),),
}
GENERIC_PROBLEM = (("매일 쓰는 물건인데 은근히 불편한 거 있죠", "참고 쓰다 보면 그게 더 스트레스가 돼요"),)

REVEAL = (
    "이럴 때 쓰기 좋은 게 바로 {name}{ida}",
    "그래서 찾은 게 {name}{ida}",
    "이 불편, {name}{euro} 정리해봤어요",
    "요즘 이런 분들이 많이 찾는 게 {name}{ida}",
)

CTA = {
    "body": ("제품 정보는 본문 링크에 남겨둘게요", "궁금하면 본문 링크에서 확인해보세요"),
    "first_comment": ("제품 링크는 첫 댓글에 남겨둘게요",),
    "hub": ("제품 정보는 프로필 링크에서 확인하세요", "자세한 건 프로필 링크에 정리해뒀어요"),
    "caption_first_line": ("링크는 게시물 본문에 있어요",),
    "manual": ("제품 정보는 설명을 확인해 주세요",),
}

# Non-factual fillers for a short that would otherwise fall under 20 s at the fast delivery rate. None of them
# states anything about the product (no digits, no fact cues — policy.claims.is_factual is False), no superlative,
# no first-person testimony: they only stretch the viewer's problem, lead into the demo, or ask for a save.
AMPLIFY_MORE = (
    "매번 이러면 은근히 스트레스예요",
    "작은 불편인데 매일 겪으면 꽤 신경 쓰여요",
    "알면서도 매번 그냥 넘어가게 되죠",
    "다들 한 번쯤 겪어보셨을 거예요",
)
BENEFITS_INTRO = (
    "핵심만 짚어볼게요",
    "포인트만 빠르게 볼게요",
)
REVEAL_MORE = (
    "어떻게 쓰는지 바로 보여드릴게요",
    "딱 보면 감이 오실 거예요",
    "바로 확인해볼게요",
)
CTA_MORE = (
    "필요한 분들은 저장해두세요",
    "나중에 찾기 쉽게 저장해두세요",
)
SHORT_FILL_TARGET = SHORT_MIN_SECONDS + 0.5  # aim just above the floor so the CTA hold stays short

TAGS = {
    "1013": ("주방꿀템", "살림템", "주방정리", "살림노하우"),
    "1018": ("차량용품", "차량꿀템", "드라이브", "차박"),
    "1014": ("생활꿀템", "정리수납", "자취템", "살림템"),
}


@dataclass
class ComposedClaim:
    text: str
    kind: str
    evidence: list[str] = field(default_factory=list)


@dataclass
class ComposedBeat:
    role: str
    narration: str
    on_screen_text: str
    visual: str
    claims: list[ComposedClaim] = field(default_factory=list)


@dataclass
class ComposedScript:
    hooks: list[str]
    hook_type: HookType
    beats: list[ComposedBeat]
    body: str
    caption: str
    hashtags: list[str]
    cta: str
    title: str
    claims: list[ComposedClaim] = field(default_factory=list)  # every claim used anywhere (beats or body)


def _problem(facts: ProductFacts, category_id: str, rng: random.Random) -> tuple[str, str]:
    """The bank entry with the most keyword hits in the product name ("차량용 틈새 수납함 콘솔 사이드" → the
    console-gap problem, not the drawer one); ties keep bank order; no hit → the category's problem."""
    name = str(facts.get("name", ""))
    hits = [(sum(k in name for k in keys), -i) for i, (keys, _) in enumerate(PROBLEM_BANK)]
    best, neg_i = max(hits, default=(0, 0))
    if best:
        return rng.choice(PROBLEM_BANK[-neg_i][1])
    return rng.choice(CATEGORY_PROBLEMS.get(category_id, GENERIC_PROBLEM))


def _benefit_candidates(facts: ProductFacts) -> list[ComposedClaim]:
    c: list[ComposedClaim] = []
    v = facts.values
    if "spec.count" in v:
        c.append(ComposedClaim(f"{v['spec.count']} 구성이라 넉넉하게 쓸 수 있어요", "spec", ["spec.count"]))
    if "spec.material" in v:
        m = str(v["spec.material"])
        c.append(ComposedClaim(f"소재는 {m}{ida(m)}", "spec", ["spec.material"]))
    if "spec.length_cm" in v:
        c.append(ComposedClaim(f"길이는 {v['spec.length_cm']}cm예요", "spec", ["spec.length_cm"]))
    if "spec.size" in v:
        sz = str(v["spec.size"])
        c.append(ComposedClaim(f"크기는 {sz}{ida(sz)}", "spec", ["spec.size"]))
    if "spec.capacity" in v:
        cap = str(v["spec.capacity"])
        c.append(ComposedClaim(f"용량은 {cap}{ida(cap)}", "spec", ["spec.capacity"]))
    rc = v.get("review_count")
    if isinstance(rc, int) and rc >= 100:
        c.append(ComposedClaim(f"리뷰가 {rc:,}개 넘게 달린 상품이에요", "review", ["review_count"]))
    rating = v.get("rating")
    if isinstance(rating, (int, float)) and rating >= 4.0:
        c.append(ComposedClaim(f"평점은 {rating:.1f}점이에요", "review", ["rating"]))
    if v.get("is_rocket"):
        c.append(ComposedClaim("로켓배송 상품이라 받기도 편해요", "shipping", ["is_rocket"]))
    elif v.get("is_free_shipping"):
        c.append(ComposedClaim("무료배송 상품이에요", "shipping", ["is_free_shipping"]))
    return c


def _hook(hook_type: HookType, problem: str, name: str, persona: Persona, facts: ProductFacts,
          rng: random.Random) -> tuple[str, list[ComposedClaim]]:
    if hook_type is HookType.QUESTION:
        return (problem if problem.endswith("?") else problem + "?"), []
    if hook_type is HookType.EMPATHY:
        opener = persona.openers[0] if persona.openers else ""
        return rng.choice(("이거 저만 불편했던 거 아니죠?", f"{opener} 이런 경험 있으시죠?".strip(),
                           "다들 한 번쯤 겪어봤을 거예요")), []
    if hook_type is HookType.SHOCK:
        return rng.choice((f"{name} 하나 바꿨을 뿐인데 달라졌어요", "이걸 왜 이제 알았을까요")), []
    if hook_type is HookType.BEFORE_AFTER:
        return rng.choice(("정리 전이랑 후, 차이 한번 보세요", "비포 애프터 보면 바로 이해돼요")), []
    if hook_type is HookType.PRICE_TWIST:
        price = facts.get("price")
        if isinstance(price, int):
            line = f"이 구성이 {price:,}원이에요"
            return line, [ComposedClaim(line, "price", ["price"])]
        return "생각보다 부담 없는 가격이라 놀랐어요", []
    return problem, []


def _screen_label(sentence: str) -> str:
    """Short on-screen label from a benefit sentence: cut at the first connective/copula ending."""
    for cut in ("이라 ", "라 ", "이에요", "예요", "이에요.", "예요."):
        if cut in sentence:
            return sentence.split(cut)[0].strip()
    return sentence.rstrip(".")


def compose(*, facts: ProductFacts, category_id: str, template: Template, persona: Persona,
            hook_type: HookType, link_placement: str, rng: random.Random) -> ComposedScript:
    name = str(facts.get("short_name") or facts.get("name"))
    problem, amplify = _problem(facts, category_id, rng)
    benefits = _benefit_candidates(facts)
    rng.shuffle(benefits)
    chosen = benefits[:3]
    spare = benefits[3:]

    hook_types = [hook_type, *[h for h in template.preferred_hooks if h is not hook_type]]
    hooks: list[str] = []
    hook_claims: list[ComposedClaim] = []
    for i, ht in enumerate(hook_types[:3]):
        line, claims = _hook(ht, problem, name, persona, facts, rng)
        hooks.append(line)
        if i == 0:
            hook_claims = claims
    while len(hooks) < 3:
        hooks.append(problem)

    if persona.openers and rng.random() < 0.6:
        amplify = f"{rng.choice(persona.openers)} {amplify}"
    reveal = rng.choice(REVEAL).format(name=name, ida=ida(name), euro=euro(name))
    cta = rng.choice(CTA.get(link_placement, CTA["hub"]))
    tags = list(TAGS.get(category_id, ("생활꿀템", "추천템")))
    rng.shuffle(tags)
    hashtags = ["광고", *tags[:3]]

    beats: list[ComposedBeat] = []
    for spec in template.beats:
        if spec.role == "hook":
            beats.append(ComposedBeat("hook", hooks[0], hooks[0], spec.visual, list(hook_claims)))
        elif spec.role in ("amplify", "problem"):
            text = amplify if spec.role == "amplify" else f"{problem}. {amplify}"
            beats.append(ComposedBeat(spec.role, text, problem if spec.role == "problem" else "", spec.visual))
        elif spec.role == "reveal":
            beats.append(ComposedBeat("reveal", reveal, name, spec.visual))
        elif spec.role == "benefits":
            narr = " ".join(c.text + "." for c in chosen) if chosen else f"{name}, 매일 쓰기 좋은 구성이에요."
            screen = " · ".join(_screen_label(c.text) for c in chosen[:3])
            beats.append(ComposedBeat("benefits", narr, screen, spec.visual, list(chosen)))
        elif spec.role == "cta":
            beats.append(ComposedBeat("cta", cta, cta, spec.visual,
                                      [ComposedClaim(cta, "cta", [])]))

    if template.kind is ContentKind.SHORT:
        _fill_short(beats, chosen, spare, amplify, rng)

    body_lines = [hooks[0], "", amplify + ".", reveal + ".", ""]
    body_lines += [f"✔ {c.text}" for c in chosen]
    body_lines += ["", cta]
    body = "\n".join(body_lines).strip()
    caption = f"{hooks[0]} {reveal}." if template.kind is ContentKind.SHORT else hooks[0]
    title = f"{name} | {hooks[0]}"[:90]
    all_claims = [*hook_claims, *chosen, ComposedClaim(cta, "cta", [])]
    return ComposedScript(hooks=hooks, hook_type=hook_type, beats=beats, body=body, caption=caption,
                          hashtags=hashtags, cta=cta, title=title, claims=all_claims)


def _fill_short(beats: list[ComposedBeat], chosen: list[ComposedClaim], spare: list[ComposedClaim],
                amplify: str, rng: random.Random) -> None:
    """Stretch a short whose spoken estimate is under 20 s (fast delivery) towards ``SHORT_FILL_TARGET``.

    In order: the unused grounded benefit candidates (each with its evidence); the problem amplification before
    the reveal when the template has no amplify/problem beat; then one non-factual line per beat (empathy →
    benefits intro → demo lead-in → save prompt → a second empathy line). A sentence that would push the
    estimate past ``SHORT_MAX_SECONDS − 0.5`` is skipped, so filling never creates a too-long script.
    Mutates ``beats`` and ``chosen`` in place.
    """
    def est() -> float:
        return estimate_seconds([b.narration for b in beats])

    def put(beat: ComposedBeat, sentence: str, *, front: bool = False) -> bool:
        before = beat.narration.strip()
        if not before:
            beat.narration = f"{sentence}."
        elif front:
            beat.narration = f"{sentence}. {before}"
        else:
            beat.narration = f"{before}{'' if before.endswith(('.', '!', '?')) else '.'} {sentence}."
        if est() > SHORT_MAX_SECONDS - 0.5:
            beat.narration = before
            return False
        return True

    by_role = {b.role: b for b in beats}
    benefits = by_role.get("benefits")
    if benefits is not None:
        for claim in spare:
            if est() >= SHORT_FILL_TARGET:
                return
            if put(benefits, claim.text):
                benefits.claims.append(claim)
                chosen.append(claim)
    reveal = by_role.get("reveal")
    if est() < SHORT_FILL_TARGET and reveal is not None and not ({"amplify", "problem"} & by_role.keys()):
        put(reveal, amplify, front=True)
    empathy = "amplify" if "amplify" in by_role else "problem"
    more = rng.sample(AMPLIFY_MORE, 2)
    steps = [(empathy, more[0], False), ("benefits", rng.choice(BENEFITS_INTRO), True),
             ("reveal", rng.choice(REVEAL_MORE), False), ("cta", rng.choice(CTA_MORE), False),
             (empathy, more[1], False)]
    for role, line, front in steps:  # one line per beat (+ a second empathy line last), never a repeat
        beat = by_role.get(role)
        if beat is None:
            continue
        if est() >= SHORT_FILL_TARGET:
            return
        if put(beat, line, front=front) and role == "cta":
            beat.claims.append(ComposedClaim(line, "cta", []))
