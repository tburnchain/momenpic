"""Self-repetition guard (§4 M4 다양성, §8.1 S-01): MinHash Jaccard vs the target account's last 30 days.

Repeating the same sentences is the #1 spam signal (§0 원본과 다르게 #2), so a draft whose estimated
Jaccard similarity to any recent post of the account exceeds 0.6 is regenerated with a different
hook/structure seed.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from acvm.context import AppContext
from acvm.quant.similarity import MinHasher

_HASHER = MinHasher(num_perm=128, shingle=4)


def script_text(hook: str, narration: str, body: str, caption: str) -> str:
    return " ".join(x for x in (hook, narration, body, caption) if x).strip()


async def max_similarity(ctx: AppContext, *, workspace_id: uuid.UUID, account_id: uuid.UUID | None,
                         text: str, exclude: uuid.UUID | None = None) -> tuple[float, uuid.UUID | None]:
    since = ctx.clock.now() - timedelta(days=ctx.settings.gate.account_lookback_days)
    async with ctx.uow() as repos:
        recent = await repos.contents.recent_texts(account_id=account_id, workspace_id=workspace_id, since=since,
                                                   exclude=exclude)
    sig = _HASHER.signature(text)
    best, best_id = 0.0, None
    for cid, other in recent:
        if not other:
            continue
        sim = MinHasher.estimate(sig, _HASHER.signature(other))
        if sim > best:
            best, best_id = sim, cid
    return best, best_id
