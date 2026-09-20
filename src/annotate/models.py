"""Data models and the dataset state machine.

Everything persisted to disk or passed between modules is defined here. The
state machine is the core of it: a dataset advances only through a declared
(state, event) pair, so an impossible transition raises instead of producing a
plausible-looking rollup.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Self

SCHEMA_VERSION = "1.0.0"

DEFAULT_IMAGE = "sdrf-annotation"
DEFAULT_SEED = Path("data/datasets.csv")
DEFAULT_ENV_FILE = Path(".env")
DEFAULT_WORK = Path("work")
DEFAULT_CONCURRENCY = 2  # parse_sdrf caps at 2; OLS and PRIDE rate-limit above it
DEFAULT_RAW_BUDGET_GB = 20.0
DEFAULT_SCRATCH_GB = 2.0
DEFAULT_MAX_REPAIR = 2
DEFAULT_TIMEOUT_S = 5400

# --permission-mode acceptEdits leaves Bash gated, and a gated Bash call in
# `claude -p` is denied outright -- there is nobody to answer the prompt, so the
# agent cannot run parse_sdrf at all. The container, not the permission system,
# is this pipeline's isolation boundary: the agent sees three bind mounts and
# has no path to logs/ or the host. Overridable for an attended debug run.
DEFAULT_PERMISSION_MODE = "bypassPermissions"


class Step(StrEnum):
    CREATOR = "creator"
    REVIEWER = "reviewer"


class State(StrEnum):
    PENDING = "pending"
    CREATING = "creating"
    CREATED = "created"
    REVIEWING = "reviewing"
    REVIEWED_PASS = "reviewed_pass"
    REVIEWED_FAIL = "reviewed_fail"
    BLOCKED = "blocked"
    FAILED_INFRA = "failed_infra"
    FAILED_CONTRACT = "failed_contract"


class Event(StrEnum):
    START_CREATOR = "start_creator"
    START_REVIEWER = "start_reviewer"
    CREATOR_COMPLETED = "creator_completed"
    CREATOR_BLOCKED = "creator_blocked"
    REVIEW_PASS = "review_pass"
    REVIEW_FAIL = "review_fail"
    REVIEW_BLOCKED = "review_blocked"
    INFRA_FAILURE = "infra_failure"
    CONTRACT_FAILURE = "contract_failure"
    REPAIR_EXHAUSTED = "repair_exhausted"

    @staticmethod
    def start(step: Step) -> Event:
        return Event.START_CREATOR if step is Step.CREATOR else Event.START_REVIEWER

    @staticmethod
    def blocked(step: Step) -> Event:
        return Event.CREATOR_BLOCKED if step is Step.CREATOR else Event.REVIEW_BLOCKED


class Outcome(StrEnum):
    """The creator's self-reported result."""

    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"


class Verdict(StrEnum):
    """The reviewer's judgement."""

    PASS = "pass"
    FAIL = "fail"
    BLOCKED = "blocked"


TERMINAL_STATES = frozenset({State.REVIEWED_PASS, State.BLOCKED})
RETRYABLE_STATES = frozenset({State.FAILED_INFRA, State.FAILED_CONTRACT})

# (state, event) -> state. A `failed_*` state carries `failed_step`, so a retry
# re-enters at the step that failed instead of redoing the creator.
TRANSITIONS: dict[tuple[State, Event], State] = {
    (State.PENDING, Event.START_CREATOR): State.CREATING,
    (State.REVIEWED_FAIL, Event.START_CREATOR): State.CREATING,
    (State.FAILED_INFRA, Event.START_CREATOR): State.CREATING,
    (State.FAILED_CONTRACT, Event.START_CREATOR): State.CREATING,
    (State.CREATING, Event.START_CREATOR): State.CREATING,
    (State.CREATING, Event.CREATOR_COMPLETED): State.CREATED,
    (State.CREATING, Event.CREATOR_BLOCKED): State.BLOCKED,
    (State.CREATING, Event.INFRA_FAILURE): State.FAILED_INFRA,
    (State.CREATING, Event.CONTRACT_FAILURE): State.FAILED_CONTRACT,
    (State.CREATED, Event.START_REVIEWER): State.REVIEWING,
    (State.REVIEWING, Event.START_REVIEWER): State.REVIEWING,
    (State.FAILED_INFRA, Event.START_REVIEWER): State.REVIEWING,
    (State.FAILED_CONTRACT, Event.START_REVIEWER): State.REVIEWING,
    (State.REVIEWING, Event.REVIEW_PASS): State.REVIEWED_PASS,
    (State.REVIEWING, Event.REVIEW_FAIL): State.REVIEWED_FAIL,
    (State.REVIEWING, Event.REVIEW_BLOCKED): State.BLOCKED,
    (State.REVIEWING, Event.INFRA_FAILURE): State.FAILED_INFRA,
    (State.REVIEWING, Event.CONTRACT_FAILURE): State.FAILED_CONTRACT,
    # Repair cap reached: a dataset the reviewer keeps rejecting is unresolvable
    # by this pipeline, which is the definition of blocked.
    (State.REVIEWED_FAIL, Event.REPAIR_EXHAUSTED): State.BLOCKED,
}


class TransitionError(RuntimeError):
    """An event was applied to a state that does not accept it."""


def transition(state: State, event: Event) -> State:
    """Apply `event` to `state`.

    Args:
        state: Current dataset state.
        event: Event to apply.

    Returns:
        The resulting state.

    Raises:
        TransitionError: The (state, event) pair is not a legal transition.
    """
    try:
        return TRANSITIONS[(State(state), Event(event))]
    except KeyError:
        raise TransitionError(f"no transition from {state!r} on {event!r}") from None


@dataclass(frozen=True, slots=True)
class RunConfig:
    """Everything a pipeline run needs, built once by the CLI.

    Frozen because it is shared across worker threads; nothing in the pipeline
    may mutate the configuration mid-batch.
    """

    work: Path = DEFAULT_WORK
    seed: Path = DEFAULT_SEED
    accessions: tuple[str, ...] = ()
    states: tuple[State, ...] = ()
    limit: int = 0
    include_annotated: bool = False
    concurrency: int = DEFAULT_CONCURRENCY
    image: str = DEFAULT_IMAGE
    permission_mode: str = DEFAULT_PERMISSION_MODE
    timeout_s: int = DEFAULT_TIMEOUT_S
    max_repair: int = DEFAULT_MAX_REPAIR
    raw_budget_gb: float = DEFAULT_RAW_BUDGET_GB
    scratch_gb: float = DEFAULT_SCRATCH_GB
    purge_raw_after: str = "dataset"
    keep_raw: bool = False
    dry_run: bool = False
    prompts_dir: Path | None = None
    env_file: Path | None = DEFAULT_ENV_FILE
    preflight: bool = True

    def replace(self, **changes: Any) -> RunConfig:
        return replace(self, **changes)


@dataclass(frozen=True, slots=True)
class DatasetPaths:
    """The on-disk layout of one dataset under the workflow root."""

    work: Path
    accession: str

    @property
    def root(self) -> Path:
        return self.work / self.accession

    @property
    def sdrf(self) -> Path:
        return self.root / "sdrf"

    @property
    def files(self) -> Path:
        return self.root / "files"

    @property
    def raw(self) -> Path:
        return self.root / "raw"

    @property
    def logs(self) -> Path:
        """Never mounted into a container, by construction."""
        return self.root / "logs"

    @property
    def rollup(self) -> Path:
        return self.logs / "status.json"

    def step_dir(self, step: Step) -> Path:
        return self.logs / str(step)

    def status(self, step: Step) -> Path:
        return self.step_dir(step) / "status.json"

    def config_dir(self, step: Step) -> Path:
        return self.step_dir(step) / "claude-config"

    def scaffold(self) -> None:
        for path in (self.sdrf, self.files, self.raw, self.logs):
            path.mkdir(parents=True, exist_ok=True)
        for step in Step:
            self.step_dir(step).mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True, slots=True)
class SeedRow:
    accession: str
    title: str = ""
    annotated: bool = False


@dataclass(slots=True)
class RunResult:
    """What one agent container produced, before any interpretation."""

    exit_code: int
    timed_out: bool = False
    duration_s: float = 0.0
    session_id: str = ""
    result_event: dict[str, Any] = field(default_factory=dict)
    final_text: str = ""
    over_budget: str = ""
    auth_failed: bool = False

    @property
    def usage(self) -> dict[str, Any]:
        event = self.result_event or {}
        usage = event.get("usage") or {}
        return {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
            "cost_usd": event.get("total_cost_usd", 0.0),
            "num_turns": event.get("num_turns", 0),
        }


@dataclass(slots=True)
class Contract:
    valid: bool
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"valid": self.valid, "error": self.error}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Contract:
        data = data or {}
        return cls(valid=bool(data.get("valid", False)), error=data.get("error"))


@dataclass(slots=True)
class RunStatus:
    """The host's record of one agent run. The only authoritative artifact.

    Both rollups are derived from these, so a corrupted rollup is always
    regenerable as long as these survive.
    """

    accession: str
    step: Step
    attempt: int
    run_id: str = ""
    session_id: str = ""
    started: str = ""
    ended: str = ""
    duration_s: float = 0.0
    exit_code: int = 0
    timed_out: bool = False
    outcome: str | None = None
    verdict: str | None = None
    blocked_reason: str | None = None
    artifacts: list[dict[str, str]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    contract: Contract = field(default_factory=lambda: Contract(valid=False))
    notes: list[str] = field(default_factory=list)
    agent_output: dict[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "accession": self.accession,
            "step": str(self.step),
            "attempt": self.attempt,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "started": self.started,
            "ended": self.ended,
            "duration_s": self.duration_s,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "outcome": self.outcome,
            "verdict": self.verdict,
            "blocked_reason": self.blocked_reason,
            "artifacts": self.artifacts,
            "usage": self.usage,
            "contract": self.contract.to_dict(),
            "notes": self.notes,
            "agent_output": self.agent_output,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            accession=data["accession"],
            step=Step(data["step"]),
            attempt=data.get("attempt", 0),
            run_id=data.get("run_id", ""),
            session_id=data.get("session_id", ""),
            started=data.get("started", ""),
            ended=data.get("ended", ""),
            duration_s=data.get("duration_s", 0.0),
            exit_code=data.get("exit_code", 0),
            timed_out=data.get("timed_out", False),
            outcome=data.get("outcome"),
            verdict=data.get("verdict"),
            blocked_reason=data.get("blocked_reason"),
            artifacts=data.get("artifacts", []),
            usage=data.get("usage", {}),
            contract=Contract.from_dict(data.get("contract")),
            notes=data.get("notes", []),
            agent_output=data.get("agent_output", {}),
            schema_version=data.get("schema_version", SCHEMA_VERSION),
        )


@dataclass(slots=True)
class HistoryEntry:
    at: str
    from_state: State
    event: Event
    to_state: State
    step: Step | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "from": str(self.from_state),
            "event": str(self.event),
            "to": str(self.to_state),
            "step": str(self.step) if self.step else None,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            at=data.get("at", ""),
            from_state=State(data["from"]),
            event=Event(data["event"]),
            to_state=State(data["to"]),
            step=Step(data["step"]) if data.get("step") else None,
            detail=data.get("detail"),
        )


@dataclass(slots=True)
class DatasetRollup:
    """Derived per-dataset state. Regenerable from the run statuses."""

    accession: str
    state: State = State.PENDING
    failed_step: Step | None = None
    attempts: int = 0
    updated: str = ""
    history: list[HistoryEntry] = field(default_factory=list)
    blocked_reason: str | None = None
    derived: bool = False
    schema_version: str = SCHEMA_VERSION

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def is_retryable(self) -> bool:
        return self.state in RETRYABLE_STATES

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "accession": self.accession,
            "state": str(self.state),
            "failed_step": str(self.failed_step) if self.failed_step else None,
            "attempts": self.attempts,
            "updated": self.updated,
            "history": [entry.to_dict() for entry in self.history],
            "blocked_reason": self.blocked_reason,
            "derived": self.derived,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            accession=data["accession"],
            state=State(data.get("state", State.PENDING)),
            failed_step=Step(data["failed_step"]) if data.get("failed_step") else None,
            attempts=data.get("attempts", 0),
            updated=data.get("updated", ""),
            history=[HistoryEntry.from_dict(e) for e in data.get("history", [])],
            blocked_reason=data.get("blocked_reason"),
            derived=data.get("derived", False),
            schema_version=data.get("schema_version", SCHEMA_VERSION),
        )
