"""The pipeline itself: rollups, the per-dataset loop, and batch execution."""

from __future__ import annotations

import csv
import shutil
import sys
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from annotate import prompts, runner
from annotate.contracts import (
    check_agent_artifacts,
    hash_artifacts,
    normalize,
    validate_contract,
)
from annotate.models import (
    RETRYABLE_STATES,
    SCHEMA_VERSION,
    TERMINAL_STATES,
    Contract,
    DatasetPaths,
    DatasetRollup,
    Event,
    HistoryEntry,
    Outcome,
    RunConfig,
    RunResult,
    RunStatus,
    SeedRow,
    State,
    Step,
    Verdict,
    transition,
)
from annotate.utils import (
    dir_size_bytes,
    empty_dir,
    extract_last_json_object,
    now_iso,
    read_json,
    write_json,
)

# --------------------------------------------------------------------------
# rollups
# --------------------------------------------------------------------------


def load_rollup(paths: DatasetPaths) -> DatasetRollup:
    existing = read_json(paths.rollup)
    if existing:
        return DatasetRollup.from_dict(existing)
    return DatasetRollup(accession=paths.accession, updated=now_iso())


def apply_event(
    paths: DatasetPaths,
    rollup: DatasetRollup,
    event: Event,
    *,
    step: Step | None = None,
    detail: str | None = None,
) -> DatasetRollup:
    """Advance the rollup by one event and persist it.

    Args:
        paths: Dataset paths.
        rollup: The rollup, mutated in place.
        event: The event to apply.
        step: The step the event came from, recorded so a retry can re-enter
            at the step that failed.
        detail: Free text for the history entry.

    Returns:
        The same rollup, advanced and written to disk.

    Raises:
        TransitionError: The event is illegal in the current state.
    """
    before = rollup.state
    rollup.state = transition(before, event)
    rollup.updated = now_iso()
    if event in (Event.INFRA_FAILURE, Event.CONTRACT_FAILURE):
        rollup.failed_step = step
    elif rollup.state not in RETRYABLE_STATES:
        rollup.failed_step = None
    rollup.history.append(
        HistoryEntry(rollup.updated, before, event, rollup.state, step, detail)
    )
    write_json(paths.rollup, rollup.to_dict())
    return rollup


def revalidate(run: RunStatus, paths: DatasetPaths) -> Contract:
    """Re-judge a stored run against the current contract logic.

    The run status keeps the agent's raw output, so the contract verdict is
    derivable rather than historical. That makes a fix to the contract layer
    retroactive: a run wrongly rejected by an older rule is recovered by
    `annotate rollup`, instead of costing a fresh -- and expensive -- agent run.
    """
    if not run.agent_output:
        return run.contract
    payload = normalize(run.step, run.agent_output)
    if error := validate_contract(payload, run.step):
        return Contract(valid=False, error=error)
    error, _ = check_agent_artifacts(run.step, payload, hash_artifacts(paths.sdrf))
    return Contract(valid=error is None, error=error)


def derive_rollup(paths: DatasetPaths, max_repair: int) -> DatasetRollup:
    """Rebuild a dataset rollup from the authoritative per-run status files.

    Args:
        paths: Dataset paths.
        max_repair: Repair cap, needed to decide whether a failing review is
            `reviewed_fail` (retryable) or `blocked` (cap reached).

    Returns:
        A freshly derived rollup. `history` is not reconstructible from run
        statuses and is left empty for the caller to preserve.
    """
    creator = read_json(paths.status(Step.CREATOR))
    reviewer = read_json(paths.status(Step.REVIEWER))
    attempts = max((run or {}).get("attempt", 0) for run in (creator, reviewer, {}))
    rollup = DatasetRollup(
        accession=paths.accession, attempts=attempts, updated=now_iso(), derived=True
    )

    if creator is None:
        return rollup
    creator_run = RunStatus.from_dict(creator)
    creator_run.contract = revalidate(creator_run, paths)
    if not creator_run.contract.valid:
        rollup.state = (
            State.FAILED_CONTRACT if creator_run.exit_code == 0 else State.FAILED_INFRA
        )
        rollup.failed_step = Step.CREATOR
        return rollup
    if creator_run.outcome == Outcome.BLOCKED:
        rollup.state = State.BLOCKED
        rollup.blocked_reason = creator_run.blocked_reason
        return rollup
    if creator_run.outcome != Outcome.COMPLETED:
        rollup.state = State.FAILED_INFRA
        rollup.failed_step = Step.CREATOR
        return rollup

    rollup.state = State.CREATED
    # A reviewer status from an earlier attempt describes a superseded artifact;
    # the review has to be redone, so the dataset stays at `created`.
    if reviewer is None:
        return rollup
    reviewer_run = RunStatus.from_dict(reviewer)
    if reviewer_run.attempt != creator_run.attempt:
        return rollup
    reviewer_run.contract = revalidate(reviewer_run, paths)
    if not reviewer_run.contract.valid:
        rollup.state = (
            State.FAILED_CONTRACT if reviewer_run.exit_code == 0 else State.FAILED_INFRA
        )
        rollup.failed_step = Step.REVIEWER
        return rollup

    match reviewer_run.verdict:
        case Verdict.PASS:
            rollup.state = State.REVIEWED_PASS
        case Verdict.BLOCKED:
            rollup.state = State.BLOCKED
            rollup.blocked_reason = reviewer_run.blocked_reason
        case Verdict.FAIL:
            if attempts > max_repair:
                rollup.state = State.BLOCKED
                rollup.blocked_reason = f"repair cap of {max_repair} attempts reached"
            else:
                rollup.state = State.REVIEWED_FAIL
        case _:
            rollup.state = State.FAILED_CONTRACT
            rollup.failed_step = Step.REVIEWER
    return rollup


def workflow_rollup(work: Path) -> dict[str, Any]:
    """Summarise every dataset rollup under `work` into one derived document."""
    datasets: dict[str, Any] = {}
    for rollup_path in sorted(work.glob("*/logs/status.json")):
        data = read_json(rollup_path)
        if not data:
            continue
        rollup = DatasetRollup.from_dict(data)
        datasets[rollup.accession] = {
            "state": str(rollup.state),
            "attempts": rollup.attempts,
            "updated": rollup.updated,
            "blocked_reason": rollup.blocked_reason,
            # Why the dataset is stuck. Without this an auth failure reads as a
            # bare `failed_infra` and the cause stays buried in the trace. Only
            # shown for states that need explaining: history outlives a
            # re-derivation, so a healthy dataset would otherwise carry the
            # detail of a failure it has since recovered from.
            "last_detail": next(
                (e.detail for e in reversed(rollup.history) if e.detail), None
            )
            if rollup.is_retryable or rollup.state is State.BLOCKED
            else None,
        }
    counts: dict[str, int] = {}
    for entry in datasets.values():
        counts[entry["state"]] = counts.get(entry["state"], 0) + 1
    return {
        "schema_version": SCHEMA_VERSION,
        "generated": now_iso(),
        "total": len(datasets),
        "counts": dict(sorted(counts.items())),
        "datasets": datasets,
    }


def _persist_revalidated_runs(paths: DatasetPaths) -> None:
    """Write back any run whose contract verdict changed under current logic."""
    for step in Step:
        stored = read_json(paths.status(step))
        if not stored:
            continue
        run = RunStatus.from_dict(stored)
        verdict = revalidate(run, paths)
        if verdict.to_dict() != run.contract.to_dict():
            run.contract = verdict
            write_json(paths.status(step), run.to_dict())


def rebuild_rollups(work: Path, max_repair: int) -> int:
    """Regenerate every rollup from the per-run statuses.

    Returns:
        The number of dataset rollups rebuilt.
    """
    rebuilt = 0
    for logs_dir in sorted(work.glob("*/logs")):
        paths = DatasetPaths(work, logs_dir.parent.name)
        _persist_revalidated_runs(paths)
        derived = derive_rollup(paths, max_repair)
        if existing := read_json(paths.rollup):
            previous = DatasetRollup.from_dict(existing)
            derived.history = previous.history
            derived.attempts = max(derived.attempts, previous.attempts)
        write_json(paths.rollup, derived.to_dict())
        rebuilt += 1
    write_json(work / "status.json", workflow_rollup(work))
    return rebuilt


# --------------------------------------------------------------------------
# raw lifecycle
# --------------------------------------------------------------------------


def check_raw_budget(paths: DatasetPaths, config: RunConfig) -> str | None:
    """Return a blocking reason when `raw/` exceeds the per-dataset budget.

    This is the backstop for a run that finished between two watchdog polls;
    the watchdog in `runner` is the primary enforcement.
    """
    used = dir_size_bytes(paths.raw)
    if used <= int(config.raw_budget_gb * 1024**3):
        return None
    return (
        f"raw download budget exceeded: {used / 1024**3:.1f} GB in raw/ "
        f"against a cap of {config.raw_budget_gb:g} GB"
    )


def purge_raw(paths: DatasetPaths, config: RunConfig, point: str) -> int:
    """Purge `raw/` if the configured purge point has been reached.

    Args:
        paths: Dataset paths.
        config: Run configuration.
        point: The point just reached, "creator" or "dataset".

    Returns:
        Bytes reclaimed.
    """
    if config.keep_raw or config.purge_raw_after != point:
        return 0
    return empty_dir(paths.raw)


def purge_workflow_raw(work: Path, include_in_flight: bool = False) -> int:
    """Reclaim `raw/` across the workflow.

    Args:
        work: Workflow root.
        include_in_flight: Also purge datasets that are not yet settled.

    Returns:
        Bytes reclaimed.
    """
    reclaimed = 0
    for raw_dir in sorted(work.glob("*/raw")):
        rollup = load_rollup(DatasetPaths(work, raw_dir.parent.name))
        if not include_in_flight and not (rollup.is_terminal or rollup.is_retryable):
            continue
        reclaimed += empty_dir(raw_dir)
    return reclaimed


def route_to_sandbox(work: Path, paths: DatasetPaths, reason: str) -> None:
    """Stage a blocked dataset for the CI-exempt `sandbox/` contribution path."""
    target = work / "sandbox" / paths.accession
    target.mkdir(parents=True, exist_ok=True)
    for artifact in sorted(paths.sdrf.glob("*.sdrf.tsv")):
        shutil.copy2(artifact, target / artifact.name)
    (target / "BLOCKED.md").write_text(
        f"# BLOCKED: {paths.accession}\n\n{reason.strip()}\n\n"
        f"Recorded {now_iso()} by the annotate pipeline. Not retried.\n"
    )


# --------------------------------------------------------------------------
# one agent run
# --------------------------------------------------------------------------


def write_run_status(
    paths: DatasetPaths,
    step: Step,
    attempt: int,
    run: RunResult,
    payload: dict[str, Any] | None,
    contract_error: str | None,
    artifacts: dict[str, str],
    started: str,
    notes: list[str] | None = None,
) -> RunStatus:
    """Persist the host's record of one run. This is the authoritative artifact."""
    status = RunStatus(
        accession=paths.accession,
        step=step,
        attempt=attempt,
        run_id=str(uuid.uuid4()),
        session_id=run.session_id,
        started=started,
        ended=now_iso(),
        duration_s=round(run.duration_s, 1),
        exit_code=run.exit_code,
        timed_out=run.timed_out,
        outcome=(payload or {}).get("outcome"),
        verdict=(payload or {}).get("verdict"),
        blocked_reason=(payload or {}).get("blocked_reason") or run.over_budget or None,
        artifacts=[{"path": p, "sha256": h} for p, h in sorted(artifacts.items())],
        usage=run.usage,
        contract=Contract(valid=contract_error is None, error=contract_error),
        notes=notes or [],
        agent_output=payload or {},
    )
    write_json(paths.status(step), status.to_dict())
    return status


def _classify_creator(
    paths: DatasetPaths,
    rollup: DatasetRollup,
    payload: dict[str, Any],
    config: RunConfig,
) -> DatasetRollup:
    match payload["outcome"]:
        case Outcome.COMPLETED:
            if over := check_raw_budget(paths, config):
                rollup.blocked_reason = over
                return apply_event(
                    paths, rollup, Event.CREATOR_BLOCKED, step=Step.CREATOR, detail=over
                )
            return apply_event(paths, rollup, Event.CREATOR_COMPLETED, step=Step.CREATOR)
        case Outcome.BLOCKED:
            rollup.blocked_reason = payload.get("blocked_reason")
            return apply_event(
                paths,
                rollup,
                Event.CREATOR_BLOCKED,
                step=Step.CREATOR,
                detail=rollup.blocked_reason,
            )
        case _:
            return apply_event(
                paths,
                rollup,
                Event.INFRA_FAILURE,
                step=Step.CREATOR,
                detail="creator reported outcome 'failed'",
            )


def _classify_reviewer(
    paths: DatasetPaths, rollup: DatasetRollup, payload: dict[str, Any]
) -> DatasetRollup:
    match payload["verdict"]:
        case Verdict.PASS:
            return apply_event(paths, rollup, Event.REVIEW_PASS, step=Step.REVIEWER)
        case Verdict.BLOCKED:
            rollup.blocked_reason = payload.get("blocked_reason")
            return apply_event(
                paths,
                rollup,
                Event.REVIEW_BLOCKED,
                step=Step.REVIEWER,
                detail=rollup.blocked_reason,
            )
        case _:
            errors = sum(
                1 for f in payload.get("findings", []) if f.get("severity") == "error"
            )
            return apply_event(
                paths,
                rollup,
                Event.REVIEW_FAIL,
                step=Step.REVIEWER,
                detail=f"{errors} error finding(s)",
            )


def run_step(
    step: Step,
    paths: DatasetPaths,
    rollup: DatasetRollup,
    config: RunConfig,
    title: str,
    review_for_repair: dict[str, Any] | None = None,
) -> tuple[DatasetRollup, dict[str, Any] | None]:
    """Run one agent and fold its result into the dataset rollup.

    Args:
        step: Which agent to run.
        paths: Dataset paths.
        rollup: The dataset rollup, mutated and persisted.
        config: Run configuration.
        title: Dataset title from the seed, injected into the prompt.
        review_for_repair: The previous reviewer payload on a repair run.

    Returns:
        (rollup, payload) where payload is the validated agent output, or None
        when the run failed before producing one.
    """
    attempt = rollup.attempts
    if not config.dry_run:
        runner.rotate_step_dir(paths, step, attempt)
        apply_event(paths, rollup, Event.start(step), step=step)

    prompt = prompts.build(
        step, paths.accession, title, config, review_for_repair, attempt
    )
    # Written by the host, not the runner, so the exact prompt a run received is
    # on disk even when the container never starts.
    paths.step_dir(step).mkdir(parents=True, exist_ok=True)
    (paths.step_dir(step) / "prompt.md").write_text(prompt)

    if config.dry_run:
        runner.run_agent(step, paths, prompt, config)  # writes command.txt only
        return rollup, None

    started = now_iso()
    run = runner.run_agent(step, paths, prompt, config)

    payload, parse_error = extract_last_json_object(run.final_text)
    if payload is not None:
        payload = normalize(step, payload)
    contract_error = parse_error or (
        validate_contract(payload, step) if payload else None
    )
    on_disk = hash_artifacts(paths.sdrf)
    notes: list[str] = []
    if contract_error is None and payload is not None:
        contract_error, notes = check_agent_artifacts(step, payload, on_disk)

    runner.prune_config_dir(paths, step)
    write_run_status(
        paths, step, attempt, run, payload, contract_error, on_disk, started, notes
    )

    # A disk breach is checked first: the run was killed, so it has no usable
    # output, and the cause is known precisely enough not to call it infra.
    if run.over_budget:
        rollup.blocked_reason = run.over_budget
        apply_event(paths, rollup, Event.blocked(step), step=step, detail=run.over_budget)
        return rollup, None
    if run.auth_failed:
        detail = f"authentication failed: {run.final_text.strip()[:120]}"
        apply_event(paths, rollup, Event.INFRA_FAILURE, step=step, detail=detail)
        # Recorded first so the dataset is not lost, then raised: every other
        # dataset would fail identically, so the batch stops rather than
        # burning 2 runs each on a credential the operator has to fix.
        raise runner.AuthenticationError(detail)
    if run.exit_code != 0 or run.timed_out:
        detail = "timed out" if run.timed_out else f"exit code {run.exit_code}"
        apply_event(paths, rollup, Event.INFRA_FAILURE, step=step, detail=detail)
        return rollup, None
    if contract_error is not None:
        apply_event(
            paths, rollup, Event.CONTRACT_FAILURE, step=step, detail=contract_error
        )
        return rollup, None

    assert payload is not None
    if step is Step.CREATOR:
        return _classify_creator(paths, rollup, payload, config), payload
    return _classify_reviewer(paths, rollup, payload), payload


# --------------------------------------------------------------------------
# one dataset
# --------------------------------------------------------------------------


def _next_step(rollup: DatasetRollup) -> Step | None:
    """Decide which agent, if any, should run next.

    `creating` and `reviewing` mean a previous run was killed before it wrote
    its status: resumable, not stuck, so that step restarts. A retryable
    failure re-enters at the step that failed, since re-annotating because the
    reviewer container crashed would discard a good artifact.
    """
    retry_step = rollup.failed_step if rollup.is_retryable else None
    if retry_step is not None:
        return retry_step
    match rollup.state:
        case State.PENDING | State.REVIEWED_FAIL | State.CREATING:
            return Step.CREATOR
        case State.CREATED | State.REVIEWING:
            return Step.REVIEWER
        case _:
            return None


def process_dataset(
    work: Path, accession: str, title: str, config: RunConfig
) -> DatasetRollup:
    """Drive one dataset from its current state to a terminal or failed state.

    Terminal datasets return untouched, which is what makes a batch resumable.

    Args:
        work: Workflow root.
        accession: ProteomeXchange accession.
        title: Seed title, injected into both prompts.
        config: Run configuration.

    Returns:
        The final dataset rollup.
    """
    paths = DatasetPaths(work, accession)
    paths.scaffold()
    rollup = load_rollup(paths)
    if rollup.is_terminal:
        return rollup

    while True:
        if rollup.state is State.REVIEWED_FAIL and rollup.attempts > config.max_repair:
            rollup.blocked_reason = f"repair cap of {config.max_repair} attempts reached"
            apply_event(
                paths, rollup, Event.REPAIR_EXHAUSTED, detail=rollup.blocked_reason
            )
            continue

        step = _next_step(rollup)
        if step is None:
            break

        if step is Step.CREATOR:
            repairing = rollup.state is State.REVIEWED_FAIL
            rollup.attempts += 1
            previous = read_json(paths.status(Step.REVIEWER)) if repairing else None
            review = (previous or {}).get("agent_output") if repairing else None
            rollup, _ = run_step(step, paths, rollup, config, title, review)
            if config.dry_run:
                return rollup
            purge_raw(paths, config, "creator")
        else:
            rollup, _ = run_step(step, paths, rollup, config, title)
            if config.dry_run:
                return rollup

        if rollup.is_terminal or rollup.is_retryable:
            break

    if rollup.state is State.BLOCKED:
        route_to_sandbox(work, paths, rollup.blocked_reason or "no reason recorded")
    if rollup.is_terminal or rollup.is_retryable:
        purge_raw(paths, config, "dataset")
    write_json(paths.rollup, rollup.to_dict())
    return rollup


# --------------------------------------------------------------------------
# seed and batch
# --------------------------------------------------------------------------


def read_seed(path: Path) -> list[SeedRow]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    return [
        SeedRow(
            accession=row["pride_accession"].strip(),
            title=(row.get("title") or "").strip(),
            annotated=(row.get("annotated") or "").strip().lower() == "true",
        )
        for row in rows
        if (row.get("pride_accession") or "").strip()
    ]


def datasets_in_progress(work: Path) -> set[str]:
    """Accessions under `work` that have started but not finished.

    Work already under way must stay reachable even when the seed marks the
    dataset as annotated, or a run interrupted mid-flight becomes unresumable
    without naming it explicitly.
    """
    started: set[str] = set()
    for rollup_path in sorted(work.glob("*/logs/status.json")):
        data = read_json(rollup_path)
        if not data:
            continue
        rollup = DatasetRollup.from_dict(data)
        if rollup.state is not State.PENDING and not rollup.is_terminal:
            started.add(rollup.accession)
    return started


def select_datasets(config: RunConfig) -> list[SeedRow]:
    """Resolve the config's filters to the datasets this run should touch.

    With no `--state` filter, terminal datasets drop out, which is what makes a
    re-run resume rather than redo.
    """
    seed_rows = read_seed(config.seed) if config.seed.exists() else []
    by_accession = {row.accession: row for row in seed_rows}

    if config.accessions:
        # An explicit request overrides the seed's `annotated` flag rather than
        # being filtered out and re-added bare, which silently dropped the
        # title and sent the agent "(no title in seed)". An accession absent
        # from the seed is still runnable: the seed is a convenience list, not
        # the set of legal inputs.
        candidates = [
            by_accession.get(accession, SeedRow(accession))
            for accession in dict.fromkeys(config.accessions)
        ]
    else:
        candidates = [
            row for row in seed_rows if config.include_annotated or not row.annotated
        ]
        known = {row.accession for row in candidates}
        candidates += [
            by_accession.get(accession, SeedRow(accession))
            for accession in sorted(datasets_in_progress(config.work) - known)
        ]

    wanted_states = set(config.states)

    def selected(row: SeedRow) -> bool:
        state = load_rollup(DatasetPaths(config.work, row.accession)).state
        if wanted_states:
            return state in wanted_states
        return state not in TERMINAL_STATES

    rows = [row for row in candidates if selected(row)]
    return rows[: config.limit] if config.limit else rows


@dataclass(slots=True)
class BatchOutcome:
    """How a batch ended, beyond the per-dataset states."""

    results: dict[str, str] = field(default_factory=dict)
    interrupted: bool = False
    auth_error: str = ""


def run_batch(
    config: RunConfig, on_result: Callable[[str, str], None] | None = None
) -> BatchOutcome:
    """Process the selected datasets, up to `concurrency` at a time.

    Args:
        config: Run configuration.
        on_result: Called with (accession, state) as each dataset settles.

    Returns:
        A BatchOutcome carrying the per-dataset states and how the batch ended.
    """
    config.work.mkdir(parents=True, exist_ok=True)
    targets = select_datasets(config)
    outcome = BatchOutcome()

    with ThreadPoolExecutor(max_workers=config.concurrency) as pool:
        futures = {
            pool.submit(
                process_dataset, config.work, row.accession, row.title, config
            ): row.accession
            for row in targets
        }
        try:
            for future in as_completed(futures):
                accession = futures[future]
                try:
                    outcome.results[accession] = str(future.result().state)
                except runner.AuthenticationError as error:
                    outcome.results[accession] = str(State.FAILED_INFRA)
                    outcome.auth_error = str(error)
                except Exception as error:  # noqa: BLE001 - one crash must not sink the batch
                    outcome.results[accession] = f"orchestrator_error: {error}"
                if on_result:
                    on_result(accession, outcome.results[accession])
                if outcome.auth_error:
                    # Nothing else can succeed until the credential is fixed.
                    pool.shutdown(wait=False, cancel_futures=True)
                    break
        except KeyboardInterrupt:
            outcome.interrupted = True
            # In-flight datasets stay at `creating`/`reviewing`, which the next
            # run treats as resumable and restarts from that step.
            print("\ninterrupted; letting in-flight containers finish", file=sys.stderr)
            pool.shutdown(wait=True, cancel_futures=True)

    if not config.dry_run:
        write_json(config.work / "status.json", workflow_rollup(config.work))
    return outcome
