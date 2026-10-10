"""Text-similarity primitives for R-02 (reference copying) and S-01 / M4 diversity (self-repetition).

* Korean is agglutinative — particles glue onto stems, so whitespace tokens under-match. All
  shingling here is on *characters* after NFKC normalisation with whitespace/punctuation removed.
* ``ngram_overlap`` — R-02's "5-gram 중복률": share of the candidate's character 5-grams that also
  occur in the reference.
* ``MinHasher`` — k-permutation MinHash; E[agreement] = Jaccard. Std error ≈ √(J(1−J)/k).
* ``LSHIndex`` — banded LSH. A pair with Jaccard s becomes a candidate with probability
  1 − (1 − s^r)^b; ``optimal_bands`` picks (b, r) minimising FP+FN mass around the threshold.
* ``HashingEmbedder`` — dependency-free 1 024-d signed feature-hashing embedding of character
  1–3-grams (offline fallback for pgvector cosine search; production uses Voyage embeddings).
"""

from __future__ import annotations

import hashlib
import math
import re
import struct
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache

_STRIP = re.compile(r"[\s\W_]+", re.UNICODE)
_MERSENNE = (1 << 61) - 1
_MAX_HASH = (1 << 32) - 1


def normalize(text: str) -> str:
    return _STRIP.sub("", unicodedata.normalize("NFKC", text).lower())


def char_ngrams(text: str, n: int) -> list[str]:
    s = normalize(text)
    if len(s) < n:
        return [s] if s else []
    return [s[i : i + n] for i in range(len(s) - n + 1)]


def ngram_overlap(candidate: str, reference: str, n: int = 5) -> float:
    """|grams(candidate) ∩ grams(reference)| / |grams(candidate)| on unique grams."""
    cand = set(char_ngrams(candidate, n))
    if not cand:
        return 0.0
    ref = set(char_ngrams(reference, n))
    return len(cand & ref) / len(cand)


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / len(sa | sb)


def cosine(u: Sequence[float], v: Sequence[float]) -> float:
    if len(u) != len(v):
        raise ValueError("dimension mismatch")
    dot = sum(x * y for x, y in zip(u, v, strict=True))
    nu = math.sqrt(sum(x * x for x in u))
    nv = math.sqrt(sum(y * y for y in v))
    if nu == 0 or nv == 0:
        return 0.0
    return dot / (nu * nv)


def _h32(token: str) -> int:
    return int(struct.unpack("<I", hashlib.blake2b(token.encode(), digest_size=4).digest())[0])


class MinHasher:
    """Universal-hash MinHash: h_i(x) = (a_i·x + b_i) mod p, p = 2⁶¹−1, seeds fixed per instance."""

    def __init__(self, num_perm: int = 128, shingle: int = 4, seed: int = 1) -> None:
        self.num_perm = num_perm
        self.shingle = shingle
        rng_bytes = hashlib.blake2b(f"minhash-{seed}".encode(), digest_size=64).digest()
        state = int.from_bytes(rng_bytes, "big")
        self._a: list[int] = []
        self._b: list[int] = []
        for _ in range(num_perm):
            state = (state * 6364136223846793005 + 1442695040888963407) % (1 << 64)
            self._a.append((state % (_MERSENNE - 1)) + 1)
            state = (state * 6364136223846793005 + 1442695040888963407) % (1 << 64)
            self._b.append(state % _MERSENNE)

    def signature(self, text: str) -> tuple[int, ...]:
        grams = set(char_ngrams(text, self.shingle))
        if not grams:
            return tuple([_MAX_HASH] * self.num_perm)
        hv = [_h32(g) for g in grams]
        sig = []
        for a, b in zip(self._a, self._b, strict=True):
            sig.append(min(((a * x + b) % _MERSENNE) & _MAX_HASH for x in hv))
        return tuple(sig)

    @staticmethod
    def estimate(sig_a: Sequence[int], sig_b: Sequence[int]) -> float:
        if len(sig_a) != len(sig_b):
            raise ValueError("signature lengths differ")
        return sum(1 for x, y in zip(sig_a, sig_b, strict=True) if x == y) / len(sig_a)

    def similarity(self, a: str, b: str) -> float:
        return self.estimate(self.signature(a), self.signature(b))


def _integrate(fn: Callable[[float], float], lo: float, hi: float, steps: int = 200) -> float:
    """Midpoint rule — the integrands are smooth and monotone, 60 panels give < 1e-4 error."""
    h = (hi - lo) / steps
    return sum(fn(lo + (i + 0.5) * h) for i in range(steps)) * h


@lru_cache(maxsize=64)
def optimal_bands(threshold: float, num_perm: int, fp_weight: float = 0.5) -> tuple[int, int]:
    """Choose (bands b, rows r), b·r ≤ num_perm, minimising
    w_fp·∫₀ᵗ P(s) ds + w_fn·∫ₜ¹ (1 − P(s)) ds  with  P(s) = 1 − (1 − s^r)^b."""
    best, best_err = (1, num_perm), math.inf
    for b in range(1, num_perm + 1):
        r_max = num_perm // b
        for r in range(1, r_max + 1):
            def p(s: float, b: int = b, r: int = r) -> float:
                return 1 - (1 - s**r) ** b

            def miss(s: float, b: int = b, r: int = r) -> float:
                return (1 - s**r) ** b

            fp = _integrate(p, 0.0, threshold, 60)
            fn = _integrate(miss, threshold, 1.0, 60)
            err = fp_weight * fp + (1 - fp_weight) * fn
            if err < best_err:
                best, best_err = (b, r), err
    return best


@dataclass
class LSHIndex:
    """Banded LSH over MinHash signatures with exact re-ranking of candidates."""

    threshold: float
    hasher: MinHasher = field(default_factory=MinHasher)
    bands: int = 0
    rows: int = 0
    _tables: list[dict[tuple[int, ...], set[str]]] = field(default_factory=list)
    _sigs: dict[str, tuple[int, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.bands:
            # weight false negatives 3:1 — a missed near-duplicate costs an account, a false
            # candidate only costs one exact comparison
            self.bands, self.rows = optimal_bands(self.threshold, self.hasher.num_perm, fp_weight=0.25)
        self._tables = [defaultdict(set) for _ in range(self.bands)]

    def add(self, key: str, text: str) -> None:
        sig = self.hasher.signature(text)
        self._sigs[key] = sig
        for i, table in enumerate(self._tables):
            table[sig[i * self.rows : (i + 1) * self.rows]].add(key)

    def query(self, text: str) -> list[tuple[str, float]]:
        """Keys whose estimated Jaccard ≥ threshold, best first."""
        sig = self.hasher.signature(text)
        cands: set[str] = set()
        for i, table in enumerate(self._tables):
            cands |= table.get(sig[i * self.rows : (i + 1) * self.rows], set())
        scored = [(k, MinHasher.estimate(sig, self._sigs[k])) for k in cands]
        return sorted([x for x in scored if x[1] >= self.threshold], key=lambda x: -x[1])

    def max_similarity(self, text: str) -> tuple[str | None, float]:
        """Exact max over *all* stored signatures (used when the corpus is small enough)."""
        sig = self.hasher.signature(text)
        best_k, best = None, 0.0
        for k, s in self._sigs.items():
            est = MinHasher.estimate(sig, s)
            if est > best:
                best_k, best = k, est
        return best_k, best

    def __len__(self) -> int:
        return len(self._sigs)


class HashingEmbedder:
    """Signed feature hashing of char 1–3-grams, sublinear TF, L2-normalised."""

    def __init__(self, dim: int = 1024) -> None:
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        s = normalize(text)
        counts: Counter[str] = Counter()
        for n in (1, 2, 3):
            counts.update(s[i : i + n] for i in range(max(0, len(s) - n + 1)))
        vec = [0.0] * self.dim
        for gram, c in counts.items():
            h = int.from_bytes(hashlib.blake2b(gram.encode(), digest_size=8).digest(), "big")
            idx = h % self.dim
            sign = 1.0 if (h >> 63) & 1 else -1.0
            vec[idx] += sign * (1.0 + math.log(c))
        norm = math.sqrt(sum(x * x for x in vec))
        return [x / norm for x in vec] if norm else vec

    def embed_many(self, texts: Sequence[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]
