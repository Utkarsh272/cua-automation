"""The control lease: one holder at a time, and stale automation tokens are refused."""

from __future__ import annotations

import pytest

from cua.core.lease import ControlLease, LeaseError


def test_full_handoff_cycle_bumps_the_epoch_twice() -> None:
    lease = ControlLease()
    old = lease.token()
    lease.check(old)

    lease.request_human("irreversible step")
    assert lease.holder == "pending_human"
    with pytest.raises(LeaseError, match="pending_human"):
        lease.check(old)
    with pytest.raises(LeaseError):
        lease.token()

    lease.claim("ops.alex")
    assert (lease.holder, lease.operator) == ("human", "ops.alex")
    lease.begin_resume()
    new = lease.resumed()
    assert lease.holder == "automation" and new.epoch == old.epoch + 2
    lease.check(new)
    with pytest.raises(LeaseError, match="stale"):
        lease.check(old)  # an automation thread that woke up late cannot act


def test_failed_resume_goes_back_to_waiting_for_a_person() -> None:
    lease = ControlLease()
    lease.request_human("dialog")
    lease.claim("ops.alex")
    lease.begin_resume()
    lease.resume_failed("dialog still open")
    assert lease.holder == "pending_human" and lease.operator is None
    lease.claim("ops.sam")
    assert lease.operator == "ops.sam"


def test_illegal_transitions_are_refused() -> None:
    lease = ControlLease()
    with pytest.raises(LeaseError):
        lease.claim("ops.alex")  # nobody asked for a human
    with pytest.raises(LeaseError):
        lease.resumed()
    lease.request_human("x")
    with pytest.raises(LeaseError):
        lease.begin_resume()  # not claimed yet
    lease.abort("timeout")
    with pytest.raises(LeaseError):
        lease.claim("ops.alex")  # aborted is final
    assert [h for _, h, _ in lease.history] == ["automation", "pending_human", "aborted"]


def test_released_claim_can_be_taken_by_someone_else() -> None:
    lease = ControlLease()
    lease.request_human("x")
    lease.claim("ops.alex")
    lease.release()
    lease.claim("ops.sam")
    assert lease.operator == "ops.sam"
