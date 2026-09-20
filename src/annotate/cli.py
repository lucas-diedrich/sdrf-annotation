"""Command line interface.

annotate run    --seed data/datasets.csv --work work/ --concurrency 2
annotate status --work work/
annotate retry  --work work/ --state failed_infra
annotate rollup --work work/
annotate purge  --work work/ --raw
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from annotate import pipeline
from annotate.models import (
    DEFAULT_CONCURRENCY,
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

EXIT_OK = 0
EXIT_INCOMPLETE = 1
EXIT_NOTHING_SELECTED = 2
EXIT_INTERRUPTED = 130


def _run_options(
    work: Path,
    seed: Path,
    accession: list[str] | None,
    state: list[State] | None,
    limit: int,
    include_annotated: bool,
    concurrency: int,
    image: str,
    permission_mode: str,
    timeout_s: int,
    max_repair: int,
    raw_budget_gb: float,
    scratch_gb: float,
    purge_raw_after: str,
    keep_raw: bool,
    dry_run: bool,
    prompts_dir: Path | None,
) -> RunConfig:
    return RunConfig(
        work=work,
        seed=seed,
        accessions=tuple(accession or ()),
        states=tuple(state or ()),
        limit=limit,
        include_annotated=include_annotated,
        concurrency=concurrency,
        image=image,
        permission_mode=permission_mode,
        timeout_s=timeout_s,
        max_repair=max_repair,
        raw_budget_gb=raw_budget_gb,
        scratch_gb=scratch_gb,
        purge_raw_after=purge_raw_after,
        keep_raw=keep_raw,
        dry_run=dry_run,
        prompts_dir=prompts_dir,
    )


def _print_summary(work: Path) -> dict:
    summary = read_json(work / "status.json") or pipeline.workflow_rollup(work)
    typer.echo(f"\n{summary['total']} dataset(s) in {work}")
    for state, count in summary["counts"].items():
        typer.echo(f"  {state:<16} {count}")
    return summary


def _execute(config: RunConfig) -> int:
    targets = pipeline.select_datasets(config)
    if not targets:
        typer.echo("nothing to do: no dataset matched the selection")
        return EXIT_OK

    typer.echo(
        f"{len(targets)} dataset(s), concurrency {config.concurrency}, "
        f"image {config.image}"
    )
    results, interrupted = pipeline.run_batch(
        config, on_result=lambda accession, state: typer.echo(f"  {accession}: {state}")
    )

    if config.dry_run:
        typer.echo(
            "\ndry run: prompts rendered and docker commands written, nothing executed"
        )
        return EXIT_OK

    _print_summary(config.work)
    if interrupted:
        return EXIT_INTERRUPTED
    incomplete = any(state != State.REVIEWED_PASS for state in results.values())
    return EXIT_INCOMPLETE if incomplete else EXIT_OK


@app.command()
def run(
    work: WorkOpt = DEFAULT_WORK,
    seed: Annotated[Path, typer.Option(help="Seed CSV of accessions.")] = DEFAULT_SEED,
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
        typer.Option(
            help="Host-enforced cap on raw/; a breach kills the run and blocks it."
        ),
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
) -> None:
    """Run the pipeline over the selected datasets."""
    config = _run_options(
        work, seed, accession, state, limit, include_annotated, concurrency, image,
        permission_mode, timeout_s, max_repair, raw_budget_gb, scratch_gb,
        purge_raw_after, keep_raw, dry_run, prompts_dir,
    )  # fmt: skip
    raise typer.Exit(_execute(config))


@app.command()
def retry(
    work: WorkOpt = DEFAULT_WORK,
    seed: Annotated[Path, typer.Option()] = DEFAULT_SEED,
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
) -> None:
    """Re-run datasets that failed. Defaults to every failure state."""
    config = _run_options(
        work, seed, accession,
        state or [State.FAILED_INFRA, State.FAILED_CONTRACT],
        limit, include_annotated, concurrency, image, permission_mode, timeout_s,
        max_repair, raw_budget_gb, scratch_gb, purge_raw_after, keep_raw, dry_run,
        prompts_dir,
    )  # fmt: skip
    raise typer.Exit(_execute(config))


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
            reason = (
                f"  — {entry['blocked_reason']}" if entry.get("blocked_reason") else ""
            )
            typer.echo(
                f"  {accession:<14} {entry['state']:<16} "
                f"attempts={entry['attempts']}{reason}"
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
