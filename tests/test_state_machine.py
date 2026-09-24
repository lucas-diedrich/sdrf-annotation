"""The dataset state machine and rollup derivation."""

from __future__ import annotations

import pytest

from annotate import pipeline
from annotate.models import (
    TERMINAL_STATES,
    TRANSITIONS,
    DatasetPaths,
    Event,
    State,
    Step,
    TransitionError,
    transition,
)
from annotate.utils import read_json, write_json


class TestTransitions:
    @pytest.mark.parametrize(
        "state,event,expected",
        [
            (State.PENDING, Event.START_CREATOR, State.CREATING),
            (State.CREATING, Event.CREATOR_COMPLETED, State.CREATED),
            (State.CREATING, Event.CREATOR_BLOCKED, State.BLOCKED),
            (State.CREATING, Event.CONTRACT_FAILURE, State.FAILED_CONTRACT),
            (State.CREATING, Event.INFRA_FAILURE, State.FAILED_INFRA),
            (State.CREATED, Event.START_REVIEWER, State.REVIEWING),
            (State.REVIEWING, Event.REVIEW_PASS, State.REVIEWED_PASS),
            (State.REVIEWING, Event.REVIEW_FAIL, State.REVIEWED_FAIL),
            (State.REVIEWING, Event.REVIEW_BLOCKED, State.BLOCKED),
            (State.REVIEWED_FAIL, Event.START_CREATOR, State.CREATING),
            (State.REVIEWED_FAIL, Event.REPAIR_EXHAUSTED, State.BLOCKED),
            (State.FAILED_INFRA, Event.START_CREATOR, State.CREATING),
            (State.FAILED_CONTRACT, Event.START_REVIEWER, State.REVIEWING),
            (State.CREATED, Event.VALIDATION_FAIL, State.REVIEWED_FAIL),
            (State.REVIEWED_PASS, Event.VALIDATION_FAIL, State.REVIEWED_FAIL),
        ],
    )
    def test_legal_transitions(self, state, event, expected):
        assert transition(state, event) is expected

    @pytest.mark.parametrize(
        "state,event",
        [
            (State.REVIEWED_PASS, Event.START_CREATOR),
            (State.BLOCKED, Event.START_CREATOR),
            (State.BLOCKED, Event.START_REVIEWER),
            (State.PENDING, Event.REVIEW_PASS),
            (State.CREATING, Event.REVIEW_PASS),
        ],
    )
    def test_illegal_transitions_raise(self, state, event):
        with pytest.raises(TransitionError):
            transition(state, event)

    def test_terminal_states_accept_only_a_validation_reopen(self):
        exits = [key for key in TRANSITIONS if key[0] in TERMINAL_STATES]

        assert exits == [(State.REVIEWED_PASS, Event.VALIDATION_FAIL)]

    def test_every_transition_uses_declared_enum_members(self):
        assert {state for state, _ in TRANSITIONS} <= set(State)
        assert {event for _, event in TRANSITIONS} <= set(Event)
        assert set(TRANSITIONS.values()) <= set(State)

    @pytest.mark.parametrize(
        "step,expected",
        [(Step.CREATOR, Event.START_CREATOR), (Step.REVIEWER, Event.START_REVIEWER)],
    )
    def test_event_start_maps_from_step(self, step, expected):
        assert Event.start(step) is expected


class TestApplyEvent:
    def test_records_history_and_persists(self, work):
        paths = DatasetPaths(work, "PXD000001")
        paths.scaffold()
        rollup = pipeline.load_rollup(paths)

        pipeline.apply_event(paths, rollup, Event.START_CREATOR, step=Step.CREATOR)
        pipeline.apply_event(paths, rollup, Event.CREATOR_COMPLETED, step=Step.CREATOR)

        reloaded = read_json(paths.rollup)
        assert reloaded["state"] == State.CREATED
        assert [entry["event"] for entry in reloaded["history"]] == [
            "start_creator",
            "creator_completed",
        ]

    def test_history_round_trips_through_disk(self, work):
        paths = DatasetPaths(work, "PXD000001")
        paths.scaffold()
        rollup = pipeline.load_rollup(paths)
        pipeline.apply_event(paths, rollup, Event.START_CREATOR, step=Step.CREATOR)

        reloaded = pipeline.load_rollup(paths)

        assert reloaded.history[0].event is Event.START_CREATOR
        assert reloaded.history[0].from_state is State.PENDING
        assert reloaded.history[0].step is Step.CREATOR

    def test_failed_step_is_recorded_then_cleared(self, work):
        paths = DatasetPaths(work, "PXD000001")
        paths.scaffold()
        rollup = pipeline.load_rollup(paths)

        pipeline.apply_event(paths, rollup, Event.START_CREATOR, step=Step.CREATOR)
        pipeline.apply_event(
            paths, rollup, Event.INFRA_FAILURE, step=Step.CREATOR, detail="exit 1"
        )
        assert rollup.failed_step is Step.CREATOR

        pipeline.apply_event(paths, rollup, Event.START_CREATOR, step=Step.CREATOR)
        assert rollup.failed_step is None


class TestDeriveRollup:
    """`annotate rollup` must rebuild everything from the per-run statuses."""

    def _write_run(self, paths, step, **fields):
        status = {
            "schema_version": "1.0.0",
            "accession": paths.accession,
            "step": str(step),
            "attempt": 1,
            "exit_code": 0,
            "contract": {"valid": True, "error": None},
        }
        status.update(fields)
        write_json(paths.status(step), status)

    def test_no_creator_run_is_pending(self, work):
        paths = DatasetPaths(work, "PXD000001")
        paths.scaffold()

        assert pipeline.derive_rollup(paths, max_repair=2).state is State.PENDING

    def test_creator_only_is_created(self, work):
        paths = DatasetPaths(work, "PXD000001")
        paths.scaffold()
        self._write_run(paths, Step.CREATOR, outcome="completed")

        assert pipeline.derive_rollup(paths, max_repair=2).state is State.CREATED

    def test_pass_is_terminal(self, work):
        paths = DatasetPaths(work, "PXD000001")
        paths.scaffold()
        self._write_run(paths, Step.CREATOR, outcome="completed")
        self._write_run(paths, Step.REVIEWER, verdict="pass")

        assert pipeline.derive_rollup(paths, max_repair=2).state is State.REVIEWED_PASS

    def test_blocked_carries_its_reason(self, work):
        paths = DatasetPaths(work, "PXD000001")
        paths.scaffold()
        self._write_run(
            paths,
            Step.CREATOR,
            outcome="blocked",
            blocked_reason="sampling-time units lack second",
        )

        derived = pipeline.derive_rollup(paths, max_repair=2)

        assert derived.state is State.BLOCKED
        assert "second" in derived.blocked_reason

    def test_contract_failure_separates_from_infra_failure(self, work):
        paths = DatasetPaths(work, "PXD000001")
        paths.scaffold()
        broken = {"valid": False, "error": "no fenced JSON block"}
        self._write_run(paths, Step.CREATOR, outcome=None, exit_code=0, contract=broken)

        assert pipeline.derive_rollup(paths, max_repair=2).state is State.FAILED_CONTRACT

        self._write_run(paths, Step.CREATOR, outcome=None, exit_code=137, contract=broken)

        assert pipeline.derive_rollup(paths, max_repair=2).state is State.FAILED_INFRA

    def test_review_fail_under_the_cap_is_retryable(self, work):
        paths = DatasetPaths(work, "PXD000001")
        paths.scaffold()
        self._write_run(paths, Step.CREATOR, outcome="completed", attempt=2)
        self._write_run(paths, Step.REVIEWER, verdict="fail", attempt=2)

        assert pipeline.derive_rollup(paths, max_repair=2).state is State.REVIEWED_FAIL

    def test_review_fail_past_the_cap_becomes_blocked(self, work):
        paths = DatasetPaths(work, "PXD000001")
        paths.scaffold()
        self._write_run(paths, Step.CREATOR, outcome="completed", attempt=3)
        self._write_run(paths, Step.REVIEWER, verdict="fail", attempt=3)

        derived = pipeline.derive_rollup(paths, max_repair=2)

        assert derived.state is State.BLOCKED
        assert "repair cap" in derived.blocked_reason

    def test_a_review_of_a_superseded_artifact_is_ignored(self, work):
        """A repair bumps the attempt; the old verdict describes the old file."""
        paths = DatasetPaths(work, "PXD000001")
        paths.scaffold()
        self._write_run(paths, Step.REVIEWER, verdict="fail", attempt=1)
        self._write_run(paths, Step.CREATOR, outcome="completed", attempt=2)

        assert pipeline.derive_rollup(paths, max_repair=2).state is State.CREATED

    @pytest.mark.parametrize(
        ("validation_attempt", "reviewer_verdict", "expected"),
        [
            pytest.param(2, None, State.REVIEWED_FAIL, id="gate-rejected"),
            pytest.param(2, "pass", State.REVIEWED_FAIL, id="repair-overrides-pass"),
            pytest.param(1, "pass", State.REVIEWED_PASS, id="stale-record-ignored"),
        ],
    )
    def test_a_validation_rejection_of_the_current_attempt_is_a_failed_review(
        self, work, validation_attempt, reviewer_verdict, expected
    ):
        paths = DatasetPaths(work, "PXD000001")
        paths.scaffold()
        self._write_run(paths, Step.CREATOR, outcome="completed", attempt=2)
        if reviewer_verdict:
            self._write_run(paths, Step.REVIEWER, verdict=reviewer_verdict, attempt=2)
        write_json(
            paths.validation,
            {"attempt": validation_attempt, "failed": [{"path": "sdrf/x.sdrf.tsv"}]},
        )

        assert pipeline.derive_rollup(paths, max_repair=2).state is expected


class TestWorkflowRollup:
    def test_counts_by_state(self, work):
        for accession, state in [
            ("PXD000001", State.REVIEWED_PASS),
            ("PXD000002", State.REVIEWED_PASS),
            ("PXD000003", State.BLOCKED),
        ]:
            paths = DatasetPaths(work, accession)
            paths.scaffold()
            write_json(
                paths.rollup,
                {"accession": accession, "state": str(state), "attempts": 1},
            )

        summary = pipeline.workflow_rollup(work)

        assert summary["total"] == 3
        assert summary["counts"] == {State.BLOCKED: 1, State.REVIEWED_PASS: 2}
