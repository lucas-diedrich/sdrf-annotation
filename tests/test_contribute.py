"""Contributing reviewed SDRFs to the dataset repo as pull requests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from annotate import contribute
from annotate.models import DatasetPaths, State, Step

ACC = "PXD000001"
SDRF = "source name\tassay name\nsample 1\trun 1\n"


@pytest.fixture
def work(tmp_path):
    return tmp_path / "work"


def make_dataset(
    work: Path,
    accession: str = ACC,
    state: str = State.REVIEWED_PASS,
    spec_gaps=None,
    templates=("ms-proteomics", "human"),
    files=(f"{ACC}.sdrf.tsv",),
    sources=None,
):
    """Write the on-disk shape of one finished dataset run."""
    paths = DatasetPaths(work, accession)
    paths.scaffold()
    for name in files:
        (paths.sdrf / name).write_text(SDRF)
    if sources is not None:
        (paths.files / "sources.json").write_text(
            json.dumps({"accession": accession, "sources": sources})
        )
    (paths.logs / "status.json").write_text(
        json.dumps({"accession": accession, "state": str(state)})
    )
    for step in Step:
        (paths.logs / str(step)).mkdir(parents=True, exist_ok=True)
        (paths.status(step)).write_text(
            json.dumps(
                {
                    "accession": accession,
                    "step": str(step),
                    "artifact_summary": [
                        {"path": f"sdrf/{name}", "templates": list(templates)}
                        for name in files
                    ],
                    "agent_output": {"spec_gaps": spec_gaps or []},
                }
            )
        )
    return paths


class FakeRunner:
    """Records commands instead of running git and gh."""

    def __init__(self, stdout: dict[str, str] | None = None, fail: str | None = None):
        self.calls: list[list[str]] = []
        self.stdout = stdout or {}
        self.fail = fail

    def __call__(self, command, cwd=None, check=True):
        self.calls.append(command)
        joined = " ".join(command)
        if self.fail and self.fail in joined:
            raise subprocess.CalledProcessError(1, command, "", "remote rejected")
        out = next((v for k, v in self.stdout.items() if k in joined), "")
        return subprocess.CompletedProcess(command, 0, out, "")

    def ran(self, fragment: str) -> bool:
        return any(fragment in " ".join(call) for call in self.calls)


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "sdrf-annotated-datasets"
    (path / ".git").mkdir(parents=True)
    return path


class TestSelection:
    def test_only_reviewed_pass_is_selected(self, work):
        make_dataset(work, "PXD000001", State.REVIEWED_PASS)
        make_dataset(work, "PXD000002", State.REVIEWED_FAIL)
        make_dataset(work, "PXD000003", State.BLOCKED)

        selected = contribute.select_candidates(work, {})

        assert [c.accession for c in selected] == ["PXD000001"]

    def test_limit_takes_a_deterministic_prefix(self, work):
        for accession in ("PXD000003", "PXD000001", "PXD000002"):
            make_dataset(work, accession)

        selected = contribute.select_candidates(work, {}, limit=2)

        assert [c.accession for c in selected] == ["PXD000001", "PXD000002"]

    def test_accession_filter_wins_over_state_scan(self, work):
        make_dataset(work, "PXD000001")
        make_dataset(work, "PXD000002")

        selected = contribute.select_candidates(work, {}, accessions=["PXD000002"])

        assert [c.accession for c in selected] == ["PXD000002"]

    def test_a_dataset_with_no_sdrf_on_disk_is_not_contributed(self, work):
        paths = make_dataset(work)
        (paths.sdrf / f"{ACC}.sdrf.tsv").unlink()

        assert contribute.select_candidates(work, {}) == []

    def test_every_sdrf_in_the_dataset_is_carried(self, work):
        make_dataset(work, files=(f"{ACC}.sdrf.tsv", f"{ACC}-cell-lines.sdrf.tsv"))

        [candidate] = contribute.select_candidates(work, {})

        assert len(candidate.sdrf_files) == 2

    def test_branch_is_namespaced_per_accession(self, work):
        make_dataset(work)

        [candidate] = contribute.select_candidates(work, {})

        assert candidate.branch == f"annotation/mannlabs/{ACC}"


class TestPullRequestContent:
    def test_commit_message_carries_the_provenance(self):
        message = contribute.commit_message(ACC)

        assert message.startswith(f"[contribution] Dataset {ACC}")
        assert "@MannLabs" in message and "@claude-opus-5" in message
        assert "sdrf-skills" in message

    def test_title_names_the_accession(self):
        assert contribute.pr_title(ACC) == f"Add SDRF annotation for {ACC}"

    def test_body_carries_title_and_checks(self, work):
        make_dataset(work)
        [candidate] = contribute.select_candidates(work, {ACC: "A deep proteome"})

        body = contribute.pr_body(candidate, live_ols=True)

        assert "This is an automatic PR" in body
        assert f"Dataset {ACC} | Title: A deep proteome" in body
        assert "- [x] `parse_sdrf validate-sdrf` passes (ms-proteomics, human)" in body
        assert "- [x] All accessions verified against live OLS4" in body
        assert "- [x] Independent adversarial review passed" in body

    def test_the_live_ols_box_is_unticked_when_the_check_was_skipped(self, work):
        make_dataset(work)
        [candidate] = contribute.select_candidates(work, {})

        body = contribute.pr_body(candidate, live_ols=False)

        assert "- [ ] All accessions verified against live OLS4" in body

    def test_spec_gaps_are_published_with_their_column(self, work):
        make_dataset(
            work,
            spec_gaps=[
                {"column": "comment[x]", "detail": "no PRIDE term for BoxCar"},
                {"column": None, "detail": "a gap with no column"},
            ],
        )
        [candidate] = contribute.select_candidates(work, {})

        body = contribute.pr_body(candidate, live_ols=True)

        assert "## Specification gaps encountered" in body
        assert "- `comment[x]` — no PRIDE term for BoxCar" in body
        assert "a gap with no column" not in body

    def test_no_gaps_means_no_section(self, work):
        make_dataset(work)
        [candidate] = contribute.select_candidates(work, {})

        assert "Specification gaps" not in contribute.pr_body(candidate, live_ols=True)

    def test_sources_are_cited_with_their_location(self, work):
        """The dataset repo asks agentic contributions to cite what they read."""
        make_dataset(
            work,
            sources=[
                {
                    "id": "pride-project",
                    "url": "https://example.org/p",
                    "used_for": ["organism", "organism part"],
                },
                {
                    "id": "publication",
                    "urls": ["https://pubmed.example/1", "https://doi.example/1"],
                    "used_for": "disease",
                },
            ],
        )
        [candidate] = contribute.select_candidates(work, {})

        body = contribute.pr_body(candidate, live_ols=True)

        assert "## Sources" in body
        assert (
            "- `pride-project` — https://example.org/p — organism, organism part" in body
        )
        assert "- `publication` — https://pubmed.example/1 — disease" in body

    def test_a_long_used_for_list_is_summarised(self, work):
        make_dataset(
            work,
            sources=[
                {
                    "id": "pride-files",
                    "url": "https://example.org/f",
                    "used_for": [f"col{n}" for n in range(7)],
                }
            ],
        )
        [candidate] = contribute.select_candidates(work, {})

        assert "col0, col1, col2 (+4 more)" in contribute.pr_body(candidate, True)

    def test_a_source_nobody_can_check_is_not_published(self, work):
        """An entry with no id or no location cites nothing."""
        make_dataset(
            work,
            sources=[
                {"id": "local-only", "local": "files/notes.txt", "used_for": "x"},
                {"url": "https://example.org/anonymous", "used_for": "y"},
                {"id": "good", "url": "https://example.org/g", "used_for": "z"},
            ],
        )
        [candidate] = contribute.select_candidates(work, {})

        body = contribute.pr_body(candidate, live_ols=True)

        assert candidate.sources == (("good", "https://example.org/g", "z"),)
        assert "local-only" not in body and "anonymous" not in body

    def test_a_missing_sources_record_omits_the_section(self, work):
        make_dataset(work)
        [candidate] = contribute.select_candidates(work, {})

        assert "## Sources" not in contribute.pr_body(candidate, live_ols=True)

    def test_reviewer_prose_never_reaches_the_body(self, work):
        """Findings quote sources the host never verified; they stay internal."""
        paths = make_dataset(work)
        status = json.loads(paths.status(Step.REVIEWER).read_text())
        status["agent_output"]["findings"] = [
            {"severity": "warning", "claim": "SECRET-CLAIM", "evidence": "SECRET-QUOTE"}
        ]
        paths.status(Step.REVIEWER).write_text(json.dumps(status))
        [candidate] = contribute.select_candidates(work, {})

        body = contribute.pr_body(candidate, live_ols=True)

        assert "SECRET-CLAIM" not in body and "SECRET-QUOTE" not in body


class TestSubmission:
    def test_a_dry_run_pushes_nothing(self, work, repo):
        make_dataset(work)
        runner = FakeRunner()

        report = contribute.contribute(
            work, {}, repo, "owner/name", dry_run=True, validate=False, runner=runner
        )

        assert report.count("dry_run") == 1
        assert not runner.ran("push") and not runner.ran("gh pr create")

    def test_a_submission_branches_commits_pushes_and_opens_the_pr(self, work, repo):
        make_dataset(work)
        runner = FakeRunner(stdout={"gh pr create": "https://github.com/x/y/pull/1"})

        report = contribute.contribute(
            work, {}, repo, "owner/name", dry_run=False, validate=False, runner=runner
        )

        [item] = report.submissions
        assert item.status == "submitted"
        assert item.url == "https://github.com/x/y/pull/1"
        assert runner.ran(f"checkout -B annotation/mannlabs/{ACC} origin/main")
        assert runner.ran("git push origin")
        assert runner.ran("--label sdrf:new")
        assert runner.ran("--label automated")

    def test_the_sdrf_lands_in_the_repository_layout(self, work, repo, tmp_path):
        make_dataset(work)
        written: list[Path] = []

        class Recorder(FakeRunner):
            def __call__(self, command, cwd=None, check=True):
                if command[:2] == ["git", "add"]:
                    written.extend(Path(arg) for arg in command[2:])
                return super().__call__(command, cwd, check)

        contribute.contribute(
            work, {}, repo, "owner/name", dry_run=False, validate=False,
            runner=Recorder(),
        )  # fmt: skip

        assert written == [Path(f"datasets/{ACC}/{ACC}.sdrf.tsv")]

    def test_an_existing_remote_branch_is_skipped_not_overwritten(self, work, repo):
        make_dataset(work)
        runner = FakeRunner(stdout={"ls-remote": "abc123\trefs/heads/branch"})

        report = contribute.contribute(
            work, {}, repo, "owner/name", dry_run=False, validate=False, runner=runner
        )

        assert report.submissions[0].status == "skipped"
        assert not runner.ran("git push")

    def test_a_push_failure_is_recorded_and_the_batch_continues(self, work, repo):
        make_dataset(work, "PXD000001")
        make_dataset(work, "PXD000002")
        runner = FakeRunner(fail="push")

        report = contribute.contribute(
            work, {}, repo, "owner/name", dry_run=False, validate=False, runner=runner
        )

        assert report.count("failed") == 2
        assert "remote rejected" in report.submissions[0].detail

    def test_labels_are_created_when_the_repo_lacks_them(self, work, repo):
        make_dataset(work)
        runner = FakeRunner()

        contribute.contribute(
            work, {}, repo, "owner/name", dry_run=False, validate=False, runner=runner
        )

        assert runner.ran("gh label create sdrf:new")
        assert runner.ran("gh label create automated")

    def test_existing_labels_are_not_recreated(self, work, repo):
        make_dataset(work)
        runner = FakeRunner(stdout={"gh label list": "sdrf:new\nautomated\n"})

        contribute.contribute(
            work, {}, repo, "owner/name", dry_run=False, validate=False, runner=runner
        )

        assert not runner.ran("gh label create")

    def test_the_operators_checkout_is_never_touched(self, work, repo):
        """Every write happens in a throwaway worktree, not the main checkout."""
        make_dataset(work)
        runner = FakeRunner()

        contribute.contribute(
            work, {}, repo, "owner/name", dry_run=False, validate=False, runner=runner
        )

        assert runner.ran("git worktree add --detach")
        assert runner.ran("git worktree remove --force")

    def test_a_missing_repo_checkout_is_fatal(self, work, tmp_path):
        make_dataset(work)

        with pytest.raises(FileNotFoundError):
            contribute.contribute(
                work, {}, tmp_path / "nope", "owner/name", runner=FakeRunner()
            )

    def test_nothing_selected_is_not_an_error(self, work, repo):
        work.mkdir(parents=True)

        report = contribute.contribute(work, {}, repo, "owner/name", runner=FakeRunner())

        assert report.submissions == []


class TestExistingAnnotations:
    """The repository already carries ~9k annotations; some are ours to update."""

    LS_TREE = {"ls-tree": f"datasets/{ACC}/{ACC}-DDA.sdrf.tsv\n"}

    def test_an_annotated_accession_is_not_contributed_by_default(self, work, repo):
        make_dataset(work)
        runner = FakeRunner(stdout=self.LS_TREE)

        report = contribute.contribute(
            work, {}, repo, "owner/name", dry_run=False, validate=False, runner=runner
        )

        assert report.submissions[0].status == "conflict"
        assert "already annotated upstream" in report.submissions[0].detail
        assert not runner.ran("git push")

    def test_allow_update_contributes_and_retitles_the_pr(self, work, repo):
        make_dataset(work)
        runner = FakeRunner(stdout=self.LS_TREE)

        report = contribute.contribute(
            work, {}, repo, "owner/name", dry_run=False, validate=False,
            allow_update=True, runner=runner,
        )  # fmt: skip

        assert report.submissions[0].status == "submitted"
        assert runner.ran(f"Update SDRF annotation for {ACC}")
        assert not runner.ran(f"Add SDRF annotation for {ACC}")

    def test_a_new_accession_is_still_titled_add(self, work, repo):
        make_dataset(work)
        runner = FakeRunner()

        contribute.contribute(
            work, {}, repo, "owner/name", dry_run=False, validate=False, runner=runner
        )

        assert runner.ran(f"Add SDRF annotation for {ACC}")

    def test_a_dry_run_names_what_would_be_replaced(self, work, repo):
        make_dataset(work)

        report = contribute.contribute(
            work, {}, repo, "owner/name", dry_run=True, validate=False,
            allow_update=True, runner=FakeRunner(stdout=self.LS_TREE),
        )  # fmt: skip

        assert "replaces" in report.submissions[0].detail
        assert f"{ACC}-DDA.sdrf.tsv" in report.submissions[0].detail

    def test_only_sdrf_files_count_as_an_existing_annotation(self, work, repo):
        make_dataset(work)
        runner = FakeRunner(stdout={"ls-tree": f"datasets/{ACC}/README.md\n"})

        report = contribute.contribute(
            work, {}, repo, "owner/name", dry_run=True, validate=False, runner=runner
        )

        assert report.submissions[0].status == "dry_run"


class TestLiveValidationGate:
    """A file that does not validate belongs in the repo's sandbox/, not datasets/."""

    def test_a_failing_file_is_never_contributed(self, work, repo, monkeypatch):
        make_dataset(work)
        monkeypatch.setattr(
            contribute,
            "_validate_live",
            lambda candidate, config: "term not found in ontology",
        )
        runner = FakeRunner()

        report = contribute.contribute(
            work, {}, repo, "owner/name", dry_run=False, validate=True, runner=runner
        )

        assert report.submissions[0].status == "failed"
        assert "live validation failed" in report.submissions[0].detail
        assert not runner.ran("git push")

    @pytest.mark.parametrize("cache_only", [True, False])
    def test_only_a_live_run_leaves_the_network_on(self, cache_only):
        """The pipeline's own check is cache-only, so it cannot support the claim."""
        from annotate.runner import validate_command

        command = validate_command(
            Path("x.sdrf.tsv"), ["human"], "img", use_ols_cache_only=cache_only
        )

        assert ("--use_ols_cache_only" in command) is cache_only
        assert ("--network" in command) is cache_only
