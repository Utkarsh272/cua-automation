from __future__ import annotations

from cua.core.artifact import Detector
from cua.replay.engine import choose_detector


def det(i: str, then: dict[str, object]) -> Detector:
    return Detector.model_validate({"id": i, "when": {"text_visible": i}, "then": then})


DETS = [
    det("notice", {"recover": "relogin"}),
    det("not_found", {"outcome": "MEMBER_NOT_FOUND"}),
    det("prompt", {"escalate": "a person must decide"}),
    det("error_page", {"fail": "APP_ERROR"}),
]


def test_fail_beats_escalate_beats_outcome_beats_recover() -> None:
    order = ["error_page", "prompt", "not_found", "notice"]
    for i in range(len(order)):
        fired = frozenset(order[i:])
        chosen = choose_detector(fired, DETS)
        assert chosen is not None and chosen.id == order[i]
    assert choose_detector(frozenset(), DETS) is None
    assert choose_detector(frozenset({"unknown"}), DETS) is None
