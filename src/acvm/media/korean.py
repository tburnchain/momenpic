"""Korean reading helpers for speech timing (§4 M6: narration budgeted at ≈ 6.5 syllables/s).

Used by the offline TTS, the SAPI word aligner and the timeline to estimate how long a 어절 takes to say:
Hangul blocks count one syllable each, numbers are read in Sino-Korean (12,900 → 만이천구백), Latin letters
≈ one syllable per two letters, "%" → 퍼센트.
"""

from __future__ import annotations

import math
import re

_DIGITS = "영일이삼사오육칠팔구"
_SMALL = ("", "십", "백", "천")
_BIG = ("", "만", "억", "조")
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
_LATIN = re.compile(r"[A-Za-z]+")


def sino_korean(n: int) -> str:
    """Sino-Korean reading of a non-negative integer (12,900 → 만이천구백, 1억 → 일억)."""
    if n <= 0:
        return "영"
    groups: list[int] = []
    while n:
        groups.append(n % 10_000)
        n //= 10_000
    out = []
    for gi in range(len(groups) - 1, -1, -1):
        g = groups[gi]
        if g == 0 or gi >= len(_BIG):
            continue
        if g == 1 and gi == 1:  # 10,000 is read "만", not "일만" (억·조 keep "일")
            out.append(_BIG[gi])
            continue
        s = ""
        for pos in range(3, -1, -1):
            d = (g // (10 ** pos)) % 10
            if d:
                s += ("" if (d == 1 and pos > 0) else _DIGITS[d]) + _SMALL[pos]
        out.append(s + _BIG[gi])
    return "".join(out)


def estimate_syllables(word: str) -> int:
    """Spoken syllables of one 어절 (minimum 1)."""
    count = sum(1 for ch in word if "가" <= ch <= "힣")
    for m in _NUM.finditer(word):
        raw = m.group(0).replace(",", "")
        whole, _, frac = raw.partition(".")
        try:
            count += len(sino_korean(int(whole[:15] or "0")))
        except ValueError:
            count += len(whole)
        if frac:
            count += 1 + len(frac)
    latin = sum(len(m.group(0)) for m in _LATIN.finditer(word))
    count += math.ceil(latin / 2)
    count += 3 * word.count("%")
    return max(1, count)
