"""Korean particle (조사) agreement for generated copy.

The correct particle depends on whether the *pronounced* final syllable has a batchim. Hangul is
decided from the jamo; digits, Latin letters and common units are decided from their Korean reading
("11cm" → 센티미터 → no batchim → "예요"; "3M" → 엠 → batchim → "이에요").
"""

from __future__ import annotations

import re

_DIGIT_BATCHIM = {"0": True, "1": True, "2": False, "3": True, "4": False, "5": False, "6": True, "7": True,
                  "8": True, "9": False}
_UNIT_BATCHIM = {"cm": False, "mm": False, "m": False, "km": False, "kg": True, "g": True, "mg": True, "ml": False,
                 "l": False, "w": False, "mah": False, "v": False, "인치": False, "%": False}
_LATIN_BATCHIM = set("lmnr")  # 엘 엠 엔 알
_RIEUL = 8  # jongseong index of ㄹ


def _final(word: str) -> tuple[bool | None, bool]:
    """(has_batchim, is_rieul) of the pronounced last syllable; None when undecidable."""
    w = re.sub(r"[\s\"'”’)\]】.,!?]+$", "", word)
    if not w:
        return None, False
    lower = w.lower()
    for unit, b in sorted(_UNIT_BATCHIM.items(), key=lambda x: -len(x[0])):
        if lower.endswith(unit) and len(lower) > len(unit) and lower[-len(unit) - 1].isdigit():
            if w[-len(unit):] == "M":  # "3M" is the brand (엠), not three metres
                break
            return b, False
    ch = w[-1]
    code = ord(ch)
    if 0xAC00 <= code <= 0xD7A3:
        jong = (code - 0xAC00) % 28
        return jong != 0, jong == _RIEUL
    if ch.isdigit():
        return _DIGIT_BATCHIM[ch], ch in "178"
    if ch.isalpha() and ch.isascii():
        return ch.lower() in _LATIN_BATCHIM, ch.lower() in "lr"
    return None, False


def ida(word: str) -> str:
    """이에요 / 예요."""
    b, _ = _final(word)
    return "이에요" if b or b is None else "예요"


def irago(word: str) -> str:
    """이라 / 라 (connective)."""
    b, _ = _final(word)
    return "이라" if b or b is None else "라"


def euro(word: str) -> str:
    """으로 / 로 — ㄹ-final takes 로."""
    b, rieul = _final(word)
    return "으로" if b and not rieul else "로"


def eun(word: str) -> str:
    b, _ = _final(word)
    return "은" if b or b is None else "는"


def iga(word: str) -> str:
    b, _ = _final(word)
    return "이" if b or b is None else "가"


def eul(word: str) -> str:
    b, _ = _final(word)
    return "을" if b or b is None else "를"
