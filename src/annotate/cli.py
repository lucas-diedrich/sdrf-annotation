"""Command line interface.

    annotate run    --seed data/datasets.csv --work work/ --concurrency 2
    annotate status --work work/
    annotate retry  --work work/ --state failed_infra
    annotate rollup --work work/
    annotate purge  --work work/ --raw
    annotate contribute --work work/ --repo ../sdrf-annotated-datasets
    annotate doctor

Nothing here resolves paths against the package, so the workflow can be run
from any directory: `--work`, `--seed` and `--env-file` are all taken as given.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from annotate import analysis, pipeline, runner
from annotate import contribute as contribute_mod
from annotate.models import (
    DEFAULT_BASE_REPO,
    DEFAULT_CONCURRENCY,
    DEFAULT_DATASET_REPO,
    DEFAULT_ENV_FILE,
    DEFAULT_IMAGE,
    DEFAULT_MAX_REPAIR,
    DEFAULT_PERMISSION_MODE,
    DEFAULT_RAW_BUDGET_GB,
    DEFAULT_SCRATCH_GB,
    DEFAULT_SEED,
    DEFAULT_TIMEOUT_S,
    DEFAULT_WORK,
    RunConfig,
    State,
)
from annotate.utils import read_json, write_json

app = typer.Typer(
    name="annotate",
    help="Two-agent (creator -> reviewer) SDRF annotation pipeline over PRIDE.",
    no_args_is_help=True,
    add_completion=False,
)

WorkOpt = Annotated[Path, typer.Option("--work", help="Workflow root directory.")]
EnvFileOpt = Annotated[
    Path | None,
    typer.Option(
        "--env-file",
        envvar="ANNOTATE_ENV_FILE",
        help="File holding ANTHROPIC_API_KEY. Defaults to .env in the working "
        "directory; the environment always wins over it.",
    ),
]

EXIT_OK = 0
EXIT_INCOMPLETE = 1
EXIT_NOTHING_SELECTED = 2
EXIT_PREFLIGHT = 3
EXIT_INTERRUPTED = 130


def _config(params: dict[str, Any]) -> RunConfig:
    """Build a RunConfig from a command's parameters.

    Typer needs each option spelled out in the command signature, so the two
    run-shaped commands pass their `locals()` through here rather than
    repeating a nineteen-argument constructor call.
    """
    params = dict(params)
    accessions = params.pop("accession", None) or ()
    states = params.pop("state", None) or ()
    # An omitted --env-file must fall through to the dataclass default, not
    # disable env-file lookup entirely.
    if params.get("env_file") is None:
        params.pop("env_file", None)
    return RunConfig(
        accessions=tuple(accessions),
        states=tuple(states),
        **{key: value for key, value in params.items() if key in RunConfig.__slots__},
    )


def _print_summary(work: Path) -> dict[str, Any]:
    summary = read_json(work / "status.json") or pipeline.workflow_rollup(work)
    typer.echo(f"\n{summary['total']} dataset(s) in {work}")
    for state, count in summary["counts"].items():
        typer.echo(f"  {state:<16} {count}")
    return summary


def _report_preflight(problems: list[str]) -> None:
    for problem in problems:
        typer.secho(f"preflight: {problem}", fg=typer.colors.RED, err=True)


def _execute(config: RunConfig) -> int:
    targets = pipeline.select_datasets(config)
    if not targets:
        typer.echo("nothing to do: no dataset matched the selection")
        return EXIT_OK

    # Checked before anything starts: a bad credential or a missing image fails
    # every dataset identically, in milliseconds, and looks like 268 separate
    # infrastructure failures rather than one thing to fix.
    if config.preflight and not config.dry_run and (problems := runner.preflight(config)):
        _report_preflight(problems)
        return EXIT_PREFLIGHT

    typer.echo(
        f"{len(targets)} dataset(s), concurrency {config.concurrency}, "
        f"image {config.image}"
    )
    outcome = pipeline.run_batch(
        config, on_result=lambda accession, state: typer.echo(f"  {accession}: {state}")
    )

    if config.dry_run:
        typer.echo(
            "\ndry run: prompts rendered and docker commands written, nothing executed"
        )
        return EXIT_OK

    _print_summary(config.work)
    if outcome.auth_error:
        typer.secho(
            f"\nbatch stopped: {outcome.auth_error}\n"
            "Every remaining dataset would fail the same way. Fix the credential, "
            "then `annotate retry --state failed_infra`.",
            fg=typer.colors.RED,
            err=True,
        )
        return EXIT_PREFLIGHT
    if outcome.interrupted:
        return EXIT_INTERRUPTED
    incomplete = any(state != State.REVIEWED_PASS for state in outcome.results.values())
    return EXIT_INCOMPLETE if incomplete else EXIT_OK


@app.command()
def run(
    work: WorkOpt = DEFAULT_WORK,
    seed: Annotated[Path, typer.Option(help="Seed CSV of accessions.")] = DEFAULT_SEED,
    env_file: EnvFileOpt = None,
    accession: Annotated[
        list[str] | None, typer.Option(help="Restrict to this accession; repeatable.")
    ] = None,
    state: Annotated[
        list[State] | None, typer.Option(help="Only datasets in this state; repeatable.")
    ] = None,
    limit: Annotated[int, typer.Option(help="Stop after this many datasets.")] = 0,
    include_annotated: Annotated[
        bool, typer.Option(help="Also run datasets the seed marks as annotated.")
    ] = False,
    concurrency: Annotated[
        int, typer.Option(help="Datasets in flight at once.")
    ] = DEFAULT_CONCURRENCY,
    image: Annotated[str, typer.Option(help="Container image.")] = DEFAULT_IMAGE,
    permission_mode: Annotated[
        str, typer.Option(help="Claude Code permission mode.")
    ] = DEFAULT_PERMISSION_MODE,
    timeout_s: Annotated[
        int, typer.Option(help="Per-run wall clock limit.")
    ] = DEFAULT_TIMEOUT_S,
    max_repair: Annotated[
        int, typer.Option(help="Repair attempts before a dataset is blocked.")
    ] = DEFAULT_MAX_REPAIR,
    raw_budget_gb: Annotated[
        float,
        typer.Option(help="Host-enforced cap on raw/; a breach kills the run."),
    ] = DEFAULT_RAW_BUDGET_GB,
    scratch_gb: Annotated[
        float, typer.Option(help="Size of the container's tmpfs scratch space.")
    ] = DEFAULT_SCRATCH_GB,
    purge_raw_after: Annotated[
        str, typer.Option(help="Purge raw/ after 'dataset' or 'creator'.")
    ] = "dataset",
    keep_raw: Annotated[bool, typer.Option(help="Never purge raw/.")] = False,
    dry_run: Annotated[
        bool, typer.Option(help="Render prompts and commands, execute nothing.")
    ] = False,
    prompts_dir: Annotated[
        Path | None, typer.Option(help="Override the packaged prompts directory.")
    ] = None,
    preflight: Annotated[
        bool, typer.Option(help="Verify image and credential before starting.")
    ] = True,
    validate_artifacts: Annotated[
        bool,
        typer.Option(help="Record a host-side parse_sdrf verdict for every SDRF."),
    ] = True,
) -> None:
    """Run the pipeline over the selected datasets."""
    raise typer.Exit(_execute(_config(locals())))


@app.command()
def retry(
    work: WorkOpt = DEFAULT_WORK,
    seed: Annotated[Path, typer.Option()] = DEFAULT_SEED,
    env_file: EnvFileOpt = None,
    state: Annotated[
        list[State] | None,
        typer.Option(help="Defaults to both failure states when omitted."),
    ] = None,
    accession: Annotated[list[str] | None, typer.Option()] = None,
    limit: Annotated[int, typer.Option()] = 0,
    include_annotated: Annotated[bool, typer.Option()] = False,
    concurrency: Annotated[int, typer.Option()] = DEFAULT_CONCURRENCY,
    image: Annotated[str, typer.Option()] = DEFAULT_IMAGE,
    permission_mode: Annotated[str, typer.Option()] = DEFAULT_PERMISSION_MODE,
    timeout_s: Annotated[int, typer.Option()] = DEFAULT_TIMEOUT_S,
    max_repair: Annotated[int, typer.Option()] = DEFAULT_MAX_REPAIR,
    raw_budget_gb: Annotated[float, typer.Option()] = DEFAULT_RAW_BUDGET_GB,
    scratch_gb: Annotated[float, typer.Option()] = DEFAULT_SCRATCH_GB,
    purge_raw_after: Annotated[str, typer.Option()] = "dataset",
    keep_raw: Annotated[bool, typer.Option()] = False,
    dry_run: Annotated[bool, typer.Option()] = False,
    prompts_dir: Annotated[Path | None, typer.Option()] = None,
    preflight: Annotated[bool, typer.Option()] = True,
    validate_artifacts: Annotated[bool, typer.Option()] = True,
) -> None:
    """Re-run datasets that failed. Defaults to every failure state."""
    params = dict(locals())
    params["state"] = state or [State.FAILED_INFRA, State.FAILED_CONTRACT]
    raise typer.Exit(_execute(_config(params)))


@app.command()
def doctor(
    image: Annotated[str, typer.Option()] = DEFAULT_IMAGE,
    env_file: EnvFileOpt = None,
) -> None:
    """Check the container image and credential without running the pipeline."""
    config = RunConfig(image=image, env_file=env_file or DEFAULT_ENV_FILE)
    if problems := runner.preflight(config):
        _report_preflight(problems)
        raise typer.Exit(EXIT_PREFLIGHT)
    typer.secho(
        f"image {image} present; {runner.API_KEY_VAR} accepted by the container.",
        fg=typer.colors.GREEN,
    )


@app.command()
def status(
    work: WorkOpt = DEFAULT_WORK,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit the rollup as JSON.")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Print the workflow rollup, refreshing it from the dataset rollups."""
    summary = pipeline.workflow_rollup(work)
    write_json(work / "status.json", summary)
    if as_json:
        typer.echo(json.dumps(summary, indent=2))
        raise typer.Exit(EXIT_OK)
    _print_summary(work)
    if verbose:
        typer.echo("")
        for accession, entry in sorted(summary["datasets"].items()):
            note = entry.get("blocked_reason") or entry.get("last_detail") or ""
            typer.echo(
                f"  {accession:<14} {entry['state']:<16} "
                f"attempts={entry['attempts']}" + (f"  — {note}" if note else "")
            )


@app.command()
def rollup(
    work: WorkOpt = DEFAULT_WORK,
    max_repair: Annotated[int, typer.Option()] = DEFAULT_MAX_REPAIR,
) -> None:
    """Regenerate both rollups from the authoritative per-run status files."""
    rebuilt = pipeline.rebuild_rollups(work, max_repair)
    typer.echo(f"rebuilt {rebuilt} dataset rollup(s) and {work / 'status.json'}")
    _print_summary(work)


@app.command()
def costs(
    work: WorkOpt = DEFAULT_WORK,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit the attribution as JSON.")
    ] = False,
) -> None:
    """Attribute the batch's spend to the phases of work that caused it."""
    report = analysis.batch_phase_costs(work)
    if as_json:
        typer.echo(json.dumps(report, indent=2))
        raise typer.Exit(EXIT_OK)
    typer.echo(f"{report['traces']} trace(s), ${report['total_cost_usd']:.2f} total\n")
    for phase, bucket in report["phases"].items():
        typer.echo(
            f"  {phase:<12} ${bucket['cost_usd']:>8.2f}  "
            f"{bucket['cost_share'] * 100:>5.1f}%  {bucket['messages']:>4} msg"
        )


@app.command()
def report(
    work: WorkOpt = DEFAULT_WORK,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit the report as JSON.")
    ] = False,
) -> None:
    """Count what the batch recorded: failures, spend, gaps, validation."""
    summary = analysis.batch_report(work)
    if as_json:
        typer.echo(json.dumps(summary, indent=2))
        raise typer.Exit(EXIT_OK)

    typer.echo(f"{summary['runs']} run(s) in {work}")
    for step, bucket in summary["by_step"].items():
        uncosted = f", {bucket['uncosted']} uncosted" if bucket["uncosted"] else ""
        typer.echo(
            f"  {step:<10} {bucket['runs']:>3} run(s)  ${bucket['cost_usd']:>8.2f}  "
            f"{bucket['duration_s'] / 3600:>5.1f} h{uncosted}"
        )
    for title, counts in (
        ("failure kinds", summary["failure_kinds"]),
        ("specification gaps by column", summary["spec_gaps"]),
        ("unresolved by column", summary["unresolved"]),
    ):
        if counts:
            typer.echo(f"\n{title}")
            for name, count in counts.items():
                typer.echo(f"  {count:>4}  {name}")
    validation = summary["validation"]
    typer.echo(
        f"\nhost-side validation: {validation['passed']} passed, "
        f"{validation['failed']} failed, {validation['not_run']} not run"
    )


@app.command()
def contribute(
    work: WorkOpt = DEFAULT_WORK,
    seed: Annotated[Path, typer.Option()] = DEFAULT_SEED,
    repo: Annotated[
        Path, typer.Option(help="Local checkout of your dataset-repo fork.")
    ] = DEFAULT_DATASET_REPO,
    base_repo: Annotated[
        str, typer.Option(help="owner/name the pull requests are opened against.")
    ] = DEFAULT_BASE_REPO,
    base_branch: Annotated[str, typer.Option()] = "main",
    accession: Annotated[list[str] | None, typer.Option()] = None,
    limit: Annotated[
        int, typer.Option(help="Contribute at most this many datasets. 0 is all.")
    ] = 0,
    dry_run: Annotated[
        bool,
        typer.Option(
            help="Select and validate only. Pass --no-dry-run to push and open PRs."
        ),
    ] = True,
    validate: Annotated[
        bool, typer.Option(help="Validate against live OLS before contributing.")
    ] = True,
    allow_update: Annotated[
        bool,
        typer.Option(help="Contribute accessions the repository already annotates."),
    ] = False,
    image: Annotated[str, typer.Option()] = DEFAULT_IMAGE,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Open one pull request per reviewed dataset against the dataset repo."""
    titles = {row.accession: row.title for row in pipeline.read_seed(seed)}
    report = contribute_mod.contribute(
        work=work,
        titles=titles,
        repo=repo,
        base_repo=base_repo,
        base_branch=base_branch,
        accessions=accession or None,
        limit=limit,
        dry_run=dry_run,
        validate=validate,
        allow_update=allow_update,
        config=RunConfig(image=image),
    )
    if as_json:
        typer.echo(json.dumps(report.to_dict(), indent=2))
    else:
        if not report.submissions:
            typer.echo("no dataset at reviewed_pass matched the selection")
            raise typer.Exit(EXIT_NOTHING_SELECTED)
        for item in report.submissions:
            suffix = item.url or item.detail
            typer.echo(f"  {item.status:<10} {item.accession:<12} {suffix}")
        if dry_run:
            typer.echo("\ndry run: nothing pushed. Re-run with --no-dry-run to submit.")
    raise typer.Exit(EXIT_OK if not report.count("failed") else EXIT_INCOMPLETE)


@app.command()
def purge(
    work: WorkOpt = DEFAULT_WORK,
    raw: Annotated[bool, typer.Option("--raw", help="Purge raw/ directories.")] = False,
    all_datasets: Annotated[
        bool, typer.Option("--all", help="Purge datasets still in flight too.")
    ] = False,
) -> None:
    """Reclaim disk left behind by earlier runs."""
    if not raw:
        typer.secho("nothing selected; pass --raw", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_NOTHING_SELECTED)
    reclaimed = pipeline.purge_workflow_raw(work, include_in_flight=all_datasets)
    typer.echo(f"reclaimed {reclaimed / 1024**3:.2f} GB")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
