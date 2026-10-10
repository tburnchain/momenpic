"""Publication state machine (§4 M8 "상태 머신", §5.2 saga compensation, §7 manual channels).

    QUEUED ─► UPLOADING ─► PROCESSING ─► PUBLISHED ─► TAKEN_DOWN
      │           │            ├───────► EXPIRED      (Instagram/Threads container > 24 h)
      │           └──► FAILED ◄┘
      ├──► FAILED
      ├──► MANUAL_PENDING ─► PUBLISHED                 (Kakao one-click queue)
      └──► CANCELLED                                   (sanction: account schedule cancelled, §9.5 #3)

Everything else is illegal and raises :class:`IllegalTransition`. A "transition" to the current state is a
field update (e.g. rescheduling a QUEUED item) and is always allowed.
"""

from __future__ import annotations

from collections.abc import Mapping

from acvm.core.enums import PublicationState as S
from acvm.core.errors import ACVMError

ALLOWED_TRANSITIONS: Mapping[S, frozenset[S]] = {
    S.QUEUED: frozenset({S.UPLOADING, S.FAILED, S.MANUAL_PENDING, S.CANCELLED}),
    S.UPLOADING: frozenset({S.PROCESSING, S.FAILED}),
    S.PROCESSING: frozenset({S.PUBLISHED, S.FAILED, S.EXPIRED}),
    S.MANUAL_PENDING: frozenset({S.PUBLISHED}),
    S.PUBLISHED: frozenset({S.TAKEN_DOWN}),
    S.FAILED: frozenset(),
    S.EXPIRED: frozenset(),
    S.CANCELLED: frozenset(),
    S.TAKEN_DOWN: frozenset(),
}

ACTIVE_STATES = frozenset({S.QUEUED, S.UPLOADING, S.PROCESSING, S.MANUAL_PENDING})
DEAD_STATES = frozenset({S.FAILED, S.EXPIRED, S.CANCELLED})


class IllegalTransition(ACVMError):
    code = "illegal_publication_transition"

    def __init__(self, frm: S, to: S) -> None:
        super().__init__(f"illegal publication transition {frm.value} → {to.value}", frm=frm.value, to=to.value)
        self.frm = frm
        self.to = to


def can_transition(frm: S, to: S) -> bool:
    return frm is to or to in ALLOWED_TRANSITIONS[frm]


def ensure_transition(frm: S, to: S) -> None:
    if not can_transition(frm, to):
        raise IllegalTransition(frm, to)


# Platforms may jump ahead of our bookkeeping (e.g. a single-phase upload that is already live, or a container
# that expired before it was ever polled). The engine still records — and emits an event for — every
# intermediate state, so consumers (M9, console) see the canonical sequence.
_VIA: Mapping[tuple[S, S], tuple[S, ...]] = {
    (S.QUEUED, S.PROCESSING): (S.UPLOADING,),
    (S.UPLOADING, S.PUBLISHED): (S.PROCESSING,),
    (S.UPLOADING, S.EXPIRED): (S.PROCESSING,),
}


def route(frm: S, to: S) -> list[S]:
    """Legal hop sequence ``frm → … → to`` (excluding ``frm``); raises on an illegal target."""
    if frm is to:
        return []
    hops = [*_VIA.get((frm, to), ()), to]
    prev = frm
    for hop in hops:
        ensure_transition(prev, hop)
        prev = hop
    return hops
