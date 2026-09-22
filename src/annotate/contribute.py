"""Publishing reviewed SDRFs to the dataset repository as pull requests.

One accession, one branch, one pull request. Every step is idempotent: a
dataset whose branch or PR already exists is skipped rather than force-pushed,
so the script can be re-run over a partially submitted batch without producing
duplicates or rewriting published history.

Work happens in a detached `git worktree`, so the checkout the operator has
open is never touched -- no branch switch, no stash, nothing to restore if the
batch is interrupted.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from annotate.models import DatasetPaths, RunConfig, State, Step
from annotate.utils import read_json

BRANCH_PREFIX = "annotation/mannlabs"
LABELS = {
    "sdrf:new": ("0e8a16", "New SDRF annotation for a dataset"),
    "automated": ("ededed", "Opened by an automated workflow"),
}
WORKFLOW_URL = "github.com/lucas-diedrich/sdrf-annotation"

# Backticked rather than @-mentioned: `claude-opus-5` is not a GitHub account,
# and an @ that resolves to nothing today would mention a stranger the day
# somebody registers the handle.
MODEL = "claude-opus-5"
COAUTHOR = "Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
GIT_TIMEOUT_S = 300

# How many `used_for` entries a cited source shows before it is summarised.
USED_FOR_SHOWN = 3

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class Candidate:
    """One dataset that passed review and is ready to be contributed."""

    accession: str
    title: str
    sdrf_files: tuple[Path, ...]
    templates: tuple[str, ...]
    spec_gaps: tuple[tuple[str, str], ...]
    sources: tuple[tuple[str, str, str], ...] = ()

    @property
    def branch(self) -> str:
        return f"{BRANCH_PREFIX}/{self.accession}"


@dataclass
class Submission:
    """What happened to one candidate."""

    accession: str
    branch: str
    status: str
    detail: str = ""
    url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "accession": self.accession,
            "branch": self.branch,
            "status": self.status,
            "detail": self.detail,
            "url": self.url,
        }


@dataclass
class Report:
    """The outcome of one `contribute` invocation."""

    submissions: list[Submission] = field(default_factory=list)

    def count(self, status: str) -> int:
        return sum(1 for item in self.submissions if item.status == status)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": len(self.submissions),
            "counts": {
                status: self.count(status)
                for status in sorted({item.status for item in self.submissions})
            },
            "submissions": [item.to_dict() for item in self.submissions],
        }


def run_command(
    command: list[str], cwd: Path | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run one git or gh command and capture its output."""
    return subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_S,
        check=check,
    )


# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------


def _spec_gaps(paths: DatasetPaths) -> tuple[tuple[str, str], ...]:
    """Read the creator's specification gaps as (column, detail) pairs.

    Entries without a column are dropped: the column is the part a maintainer
    can act on, and a gap that names none is a sentence with no address.
    """
    status = read_json(paths.status(Step.CREATOR)) or {}
    gaps = (status.get("agent_output") or {}).get("spec_gaps") or []
    return tuple(
        (entry["column"], entry.get("detail", ""))
        for entry in gaps
        if entry.get("column")
    )


def _used_for(entry: dict[str, Any]) -> str:
    """Summarise what one source was used for, bounded for a PR body.

    `used_for` is a column list on most entries and runs to a dozen names, which
    would bury the citation it belongs to. The first few name the kind of
    evidence; the count carries the rest.
    """
    used = entry.get("used_for") or []
    names = [used] if isinstance(used, str) else list(used)
    if len(names) <= USED_FOR_SHOWN:
        return ", ".join(names)
    shown = ", ".join(names[:USED_FOR_SHOWN])
    return f"{shown} (+{len(names) - USED_FOR_SHOWN} more)"


def _sources(paths: DatasetPaths) -> tuple[tuple[str, str, str], ...]:
    """Read the creator's provenance record as (id, location, used_for) rows.

    The dataset repository asks agentic contributions to cite their sources so a
    reviewer can verify organism, condition and file mapping independently. An
    entry with no id or no location cannot be checked by anyone, so it is
    dropped rather than published.
    """
    record = read_json(paths.files / "sources.json") or {}
    rows: list[tuple[str, str, str]] = []
    for entry in record.get("sources") or []:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        urls = entry.get("urls") or []
        location = entry.get("url") or (urls[0] if urls else "")
        if not location:
            continue
        rows.append((entry["id"], location, _used_for(entry)))
    return tuple(rows)


def _templates(paths: DatasetPaths) -> tuple[str, ...]:
    """The templates the SDRF declares, as the host recorded them."""
    status = read_json(paths.status(Step.REVIEWER)) or {}
    names: list[str] = []
    for summary in status.get("artifact_summary") or []:
        for name in summary.get("templates") or []:
            if name not in names:
                names.append(name)
    return tuple(names)


def select_candidates(
    work: Path,
    titles: dict[str, str],
    accessions: list[str] | None = None,
    limit: int = 0,
) -> list[Candidate]:
    """Collect every dataset whose review passed, in accession order.

    Args:
        work: Workflow root holding the per-dataset run directories.
        titles: Accession to publication title, from the seed.
        accessions: Restrict to these accessions. None selects all.
        limit: Stop after this many candidates. 0 means no limit.

    Returns:
        Candidates sorted by accession, so a limited run is a deterministic
        prefix of the full batch rather than an arbitrary subset.
    """
    wanted = set(accessions or [])
    candidates: list[Candidate] = []
    for rollup_path in sorted(work.glob("*/logs/status.json")):
        rollup = read_json(rollup_path) or {}
        accession = rollup.get("accession", "")
        if rollup.get("state") != State.REVIEWED_PASS:
            continue
        if wanted and accession not in wanted:
            continue
        paths = DatasetPaths(work, accession)
        sdrf_files = tuple(sorted(paths.sdrf.glob("*.sdrf.tsv")))
        if not sdrf_files:
            continue
        candidates.append(
            Candidate(
                accession=accession,
                title=titles.get(accession, ""),
                sdrf_files=sdrf_files,
                templates=_templates(paths),
                spec_gaps=_spec_gaps(paths),
                sources=_sources(paths),
            )
        )
        if limit and len(candidates) >= limit:
            break
    return candidates


# --------------------------------------------------------------------------
# pull request content
# --------------------------------------------------------------------------


def commit_message(accession: str) -> str:
    """The commit message, carrying the provenance of the workflow.

    The co-author trailer is the last paragraph, which is where git's own
    trailer convention requires it -- `git interpret-trailers` and GitHub both
    read only that block, so provenance stays machine-readable across the batch
    rather than living in prose alone.
    """
    return (
        f"[contribution] Dataset {accession}\n"
        "\n"
        f"Generated by @MannLabs with `{MODEL}` with the "
        f"`{WORKFLOW_URL}` workflow and `sdrf-skills`\n"
        "\n"
        f"{COAUTHOR}\n"
    )


def pr_title(accession: str, update: bool = False) -> str:
    """The pull request title.

    An accession the repository already carries is an update, not an addition,
    and saying "Add" of a file that replaces published curation misrepresents
    the change to whoever reviews it.
    """
    verb = "Update" if update else "Add"
    return f"{verb} SDRF annotation for {accession}"


def pr_body(candidate: Candidate, live_ols: bool) -> str:
    """Render the pull request body.

    Two things are carried over from the agents: the specification gaps, as a
    column and one sentence, and the provenance record, which the dataset
    repository asks every agentic contribution to cite. The reviewer's
    findings, evidence and claims stay out: they quote sources the host never
    verified, and a public contribution is the wrong place for an unverified
    citation.

    Args:
        candidate: The dataset being contributed.
        live_ols: Whether this run validated the file against live OLS. The
            checkbox is only ticked for a check that actually ran.

    Returns:
        Markdown for `gh pr create --body`.
    """
    templates = ", ".join(candidate.templates) or "declared templates"
    title = candidate.title or "(no title in seed)"
    lines = [
        f"> This is an automatic PR, generated with the workflow `{WORKFLOW_URL}` "
        "and `sdrf-skills`",
        "",
        f"Dataset {candidate.accession} | Title: {title}",
        "",
        "## Checks",
        "",
        f"- [x] `parse_sdrf validate-sdrf` passes ({templates})",
        f"- [{'x' if live_ols else ' '}] All accessions verified against live OLS4",
        "- [x] Independent adversarial review passed",
    ]
    if candidate.spec_gaps:
        lines += [
            "",
            "## Specification gaps encountered",
            "",
            "Values the specification could not express, recorded for the "
            "maintainers. Each names the column it affects.",
            "",
        ]
        lines += [f"- `{column}` — {detail}" for column, detail in candidate.spec_gaps]
    if candidate.sources:
        lines += [
            "",
            "## Sources",
            "",
            "Every record this annotation was read from, so the organism, "
            "condition and file mapping can be verified independently.",
            "",
        ]
        lines += [
            f"- `{name}` — {location}" + (f" — {used}" if used else "")
            for name, location, used in candidate.sources
        ]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# git and gh
# --------------------------------------------------------------------------


def ensure_labels(base_repo: str, runner: CommandRunner) -> None:
    """Create the PR labels that do not exist yet.

    `gh pr create --label` fails outright on an unknown label, which would
    abort the batch on its first dataset.
    """
    existing = runner(
        ["gh", "label", "list", "--repo", base_repo, "--json", "name", "-q", ".[].name"],
        check=False,
    )
    present = set(existing.stdout.split())
    for name, (color, description) in LABELS.items():
        if name in present:
            continue
        runner(
            ["gh", "label", "create", name, "--repo", base_repo,
             "--color", color, "--description", description],
            check=False,
        )  # fmt: skip


def existing_annotation(
    repo: Path, base_branch: str, accession: str, runner: CommandRunner
) -> list[str]:
    """List the SDRFs the base branch already carries for one accession.

    Read from the branch rather than the working tree, so a stale or dirty
    local checkout cannot hide published curation.

    Returns:
        Repository-relative paths, empty when the accession is new.
    """
    result = runner(
        ["git", "ls-tree", "-r", "--name-only", f"origin/{base_branch}",
         "--", f"datasets/{accession}/"],
        cwd=repo,
        check=False,
    )  # fmt: skip
    return [line for line in result.stdout.split() if line.endswith(".sdrf.tsv")]


def remote_branch_exists(repo: Path, branch: str, runner: CommandRunner) -> bool:
    result = runner(
        ["git", "ls-remote", "--heads", "origin", branch], cwd=repo, check=False
    )
    return bool(result.stdout.strip())


def _stage_files(worktree: Path, candidate: Candidate) -> list[str]:
    """Copy the dataset's SDRFs into the repository layout.

    Returns:
        The repository-relative paths written.
    """
    target_dir = worktree / "datasets" / candidate.accession
    target_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for source in candidate.sdrf_files:
        shutil.copyfile(source, target_dir / source.name)
        written.append(f"datasets/{candidate.accession}/{source.name}")
    return written


def submit(
    candidate: Candidate,
    worktree: Path,
    repo: Path,
    base_repo: str,
    base_branch: str,
    live_ols: bool,
    runner: CommandRunner,
    update: bool = False,
) -> Submission:
    """Push one branch and open its pull request.

    Args:
        candidate: The dataset to contribute.
        worktree: Detached worktree the branch is built in.
        repo: The fork's main checkout, used for remote queries.
        base_repo: `owner/name` the PR is opened against.
        base_branch: Branch the PR targets.
        live_ols: Whether live OLS validation ran, for the checklist.
        runner: Command runner, injected so the batch can be tested offline.
        update: The accession is already in the repository, so the pull request
            is titled as an update.

    Returns:
        The submission record. Never raises for an expected failure -- a
        dataset that cannot be pushed is recorded and the batch continues.
    """
    branch = candidate.branch
    if remote_branch_exists(repo, branch, runner):
        return Submission(
            candidate.accession, branch, "skipped", "branch already on origin"
        )

    try:
        runner(["git", "checkout", "-B", branch, f"origin/{base_branch}"], cwd=worktree)
        paths = _stage_files(worktree, candidate)
        runner(["git", "add", *paths], cwd=worktree)
        runner(["git", "commit", "-m", commit_message(candidate.accession)], cwd=worktree)
        runner(["git", "push", "origin", branch], cwd=worktree)
        created = runner(
            ["gh", "pr", "create",
             "--repo", base_repo,
             "--base", base_branch,
             "--head", branch,
             "--title", pr_title(candidate.accession, update),
             "--body", pr_body(candidate, live_ols),
             *(arg for label in LABELS for arg in ("--label", label))],
            cwd=worktree,
        )  # fmt: skip
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or str(error)).strip()
        return Submission(candidate.accession, branch, "failed", detail[:300])
    except subprocess.TimeoutExpired as error:
        # One hung push must not take the rest of the batch with it.
        return Submission(
            candidate.accession, branch, "failed", f"timed out: {error.cmd[:3]}"
        )

    printed = created.stdout.strip().splitlines()
    url = printed[-1] if printed else None
    return Submission(candidate.accession, branch, "submitted", url=url)


# --------------------------------------------------------------------------
# batch
# --------------------------------------------------------------------------


def _validate_live(candidate: Candidate, config: RunConfig) -> str | None:
    """Validate every SDRF against live OLS before it is published.

    The host's own run during the pipeline is `--use_ols_cache_only`, so no
    recorded check supports the "verified against live OLS4" claim. This runs
    it for real, against the network, immediately before the contribution.

    Returns:
        None when every file passes, otherwise the first failure's detail.
    """
    from annotate import runner as agent_runner

    for path in candidate.sdrf_files:
        result = agent_runner.validate_sdrf(
            path, list(candidate.templates), config, use_ols_cache_only=False
        )
        if not result.get("ran"):
            return f"{path.name}: validator could not be run: {result.get('detail', '')}"
        if not result.get("passed"):
            return f"{path.name}: {result.get('detail', 'validation failed')}"
    return None


def contribute(
    work: Path,
    titles: dict[str, str],
    repo: Path,
    base_repo: str,
    base_branch: str = "main",
    accessions: list[str] | None = None,
    limit: int = 0,
    dry_run: bool = True,
    validate: bool = True,
    allow_update: bool = False,
    config: RunConfig | None = None,
    runner: CommandRunner = run_command,
) -> Report:
    """Contribute every reviewed dataset as its own pull request.

    Args:
        work: Workflow root holding the run directories.
        titles: Accession to publication title, from the seed.
        repo: Local checkout of the fork the branches are pushed to.
        base_repo: `owner/name` the pull requests are opened against.
        base_branch: The branch the pull requests target.
        accessions: Restrict to these accessions. None selects all.
        limit: Stop after this many datasets. 0 means no limit.
        dry_run: Render and validate, but push nothing and open no PR.
        validate: Run live-OLS validation before contributing.
        allow_update: Contribute accessions the repository already annotates.
            Off by default: replacing published curation is a decision, not a
            batch default, and the two cases differ -- a same-named file is
            overwritten outright, a differently-named one lands beside the
            existing annotation for the same runs.
        config: Run configuration, for the validator image.
        runner: Command runner, injected so the batch can be tested offline.

    Returns:
        One submission record per selected dataset.

    Raises:
        FileNotFoundError: `repo` is not a git checkout.
    """
    if not (repo / ".git").exists():
        raise FileNotFoundError(f"{repo} is not a git checkout of the dataset repo")

    report = Report()
    candidates = select_candidates(work, titles, accessions, limit)
    if not candidates:
        return report

    runner(["git", "fetch", "origin", base_branch], cwd=repo)
    if not dry_run:
        ensure_labels(base_repo, runner)

    with tempfile.TemporaryDirectory(prefix="sdrf-contribute-") as scratch:
        worktree = Path(scratch) / "repo"
        if not dry_run:
            # An interrupted earlier batch can leave worktree entries behind,
            # and `worktree add` refuses to run while they are registered.
            runner(["git", "worktree", "prune"], cwd=repo, check=False)
            runner(
                ["git", "worktree", "add", "--detach", str(worktree),
                 f"origin/{base_branch}"],
                cwd=repo,
            )  # fmt: skip
        try:
            for candidate in candidates:
                existing = existing_annotation(
                    repo, base_branch, candidate.accession, runner
                )
                if existing and not allow_update:
                    report.submissions.append(
                        Submission(
                            candidate.accession,
                            candidate.branch,
                            "conflict",
                            "already annotated upstream: "
                            + ", ".join(existing)
                            + "; pass --allow-update to contribute anyway",
                        )
                    )
                    continue
                failure = (
                    _validate_live(candidate, config or RunConfig()) if validate else None
                )
                if failure:
                    # A file that does not validate belongs in the repository's
                    # sandbox/, not datasets/, so it is never contributed here.
                    report.submissions.append(
                        Submission(
                            candidate.accession,
                            candidate.branch,
                            "failed",
                            f"live validation failed: {failure}"[:300],
                        )
                    )
                    continue
                if dry_run:
                    ready = f"{len(candidate.sdrf_files)} file(s) ready"
                    report.submissions.append(
                        Submission(
                            candidate.accession,
                            candidate.branch,
                            "dry_run",
                            f"{ready} (replaces {', '.join(existing)})"
                            if existing
                            else ready,
                        )
                    )
                    continue
                report.submissions.append(
                    submit(
                        candidate,
                        worktree,
                        repo,
                        base_repo,
                        base_branch,
                        live_ols=validate,
                        runner=runner,
                        update=bool(existing),
                    )
                )
        finally:
            if not dry_run:
                runner(
                    ["git", "worktree", "remove", "--force", str(worktree)],
                    cwd=repo,
                    check=False,
                )
    return report
