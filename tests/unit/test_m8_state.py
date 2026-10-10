"""Publication state machine (§4 M8)."""

from __future__ import annotations

import pytest

from acvm.core.enums import PublicationState as S
from acvm.modules.m8_distribution.state import (
    ALLOWED_TRANSITIONS,
    IllegalTransition,
    can_transition,
    ensure_transition,
    route,
)

LEGAL = {
    (S.QUEUED, S.UPLOADING), (S.UPLOADING, S.PROCESSING), (S.PROCESSING, S.PUBLISHED),
    (S.QUEUED, S.FAILED), (S.UPLOADING, S.FAILED), (S.PROCESSING, S.FAILED), (S.PROCESSING, S.EXPIRED),
    (S.QUEUED, S.MANUAL_PENDING), (S.MANUAL_PENDING, S.PUBLISHED), (S.QUEUED, S.CANCELLED),
    (S.PUBLISHED, S.TAKEN_DOWN),
}


def test_exactly_the_designed_transitions_are_legal() -> None:
    actual = {(frm, to) for frm, tos in ALLOWED_TRANSITIONS.items() for to in tos}
    assert actual == LEGAL
    for frm in S:
        for to in S:
            assert can_transition(frm, to) == (frm is to or (frm, to) in LEGAL)


@pytest.mark.parametrize(("frm", "to"), [
    (S.QUEUED, S.PUBLISHED), (S.PUBLISHED, S.QUEUED), (S.FAILED, S.QUEUED), (S.EXPIRED, S.PUBLISHED),
    (S.CANCELLED, S.QUEUED), (S.TAKEN_DOWN, S.PUBLISHED), (S.MANUAL_PENDING, S.CANCELLED),
    (S.UPLOADING, S.QUEUED), (S.PROCESSING, S.CANCELLED),
])
def test_illegal_transitions_raise(frm: S, to: S) -> None:
    with pytest.raises(IllegalTransition) as info:
        ensure_transition(frm, to)
    assert info.value.frm is frm and info.value.to is to


def test_route_records_canonical_intermediate_states() -> None:
    assert route(S.UPLOADING, S.PUBLISHED) == [S.PROCESSING, S.PUBLISHED]
    assert route(S.UPLOADING, S.EXPIRED) == [S.PROCESSING, S.EXPIRED]
    assert route(S.QUEUED, S.MANUAL_PENDING) == [S.MANUAL_PENDING]
    assert route(S.QUEUED, S.QUEUED) == []
    with pytest.raises(IllegalTransition):
        route(S.QUEUED, S.PUBLISHED)
