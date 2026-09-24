"""Host-side parse_sdrf: the gate before review, `annotate repair`, live checks."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from annotate import contribute, pipeline
from annotate.models import DatasetPaths, RunConfig, State, Step
from annotate.runner import validate_sdrf as real_validate_sdrf
from annotate.utils import read_json
from test_pipeline import ACC, ERROR_FINDING, creator_ok, reviewer

INVALID_SDRF = "source name\tassay name\nsample 1\tBAD\n"
FIXED_SDRF = "source name\tassay name\nsample 1\tfixed\n"
MISSING_COLUMN = "ERROR: Required column 'characteristics[cell line]' is missing"

Rule = Callable[[Path, list[str], bool], dict[str, Any]]


def verdict(passed: bool | None, ran: bool = True, messages=()) -> dict[str, Any]:
    return {
        "ran": ran,
        "passed": passed,
        "detail": messages[0] if messages else "",
        "messages": list(messages),
    }


def reject_marked_content(sdrf: Path, templates: list[str], cache_only: bool):
    if "BAD" in sdrf.read_text():
        return verdict(False, messages=[MISSING_COLUMN])
    return verdict(True)


@dataclass
class FakeValidator:
    rule: Rule = lambda sdrf, templates, cache_only: verdict(True)  # noqa: E731
    calls: list[tuple[str, tuple[str, ...], bool]] = field(default_factory=list)

    def __call__(self, sdrf_file, templates, config, use_ols_cache_only=True):
        self.calls.append((sdrf_file.name, tuple(templates), use_ols_cache_only))
        return {
            "templates": templates,
            **self.rule(sdrf_file, templates, use_ols_cache_only),
        }


@pytest.fixture
def validator(monkeypatch) -> FakeValidator:
    fake = FakeValidator()
    monkeypatch.setattr("annotate.runner.validate_sdrf", fake)
    return fake


def sdrf_with_templates(*rows: tuple[str, ...]) -> str:
    """An SDRF whose rows each declare the given templates."""
    width = max(len(row) for row in rows)
    header = ["source name", *["comment[sdrf template]"] * width, "assay name"]
    lines = ["\t".join(header)]
    for index, templates in enumerate(rows):
        cells = [f"NT={name};VV=v1.1.0" for name in templates]
        cells += [""] * (width - len(templates))
        lines.append("\t".join([f"sample {index}", *cells, f"run {index}"]))
    return "\n".join(lines) + "\n"


class TestGateBeforeReview:
    def test_an_invalid_sdrf_goes_back_to_the_creator_unreviewed(
        self, work, config, fake_agent, validator
    ):
        validator.rule = reject_marked_content
        agent = fake_agent(
            [
                (Step.CREATOR, creator_ok(INVALID_SDRF)),
                (Step.CREATOR, creator_ok(FIXED_SDRF)),
                (Step.REVIEWER, reviewer("pass")),
            ]
        )

        rollup = pipeline.process_dataset(work, ACC, "t", config)

        assert agent.calls == [Step.CREATOR, Step.CREATOR, Step.REVIEWER]
        assert rollup.state is State.REVIEWED_PASS
        assert rollup.attempts == 2
        assert any(entry.event == "validation_fail" for entry in rollup.history)

    def test_the_validator_output_is_the_repair_brief(
        self, work, config, fake_agent, validator
    ):
        """A stale review from an earlier attempt must not leak into the brief."""
        validator.rule = reject_marked_content
        fake_agent(
            [
                (Step.CREATOR, creator_ok()),
                (Step.REVIEWER, reviewer("fail", [ERROR_FINDING])),
                (Step.CREATOR, creator_ok(INVALID_SDRF)),
                (Step.CREATOR, creator_ok(FIXED_SDRF)),
                (Step.REVIEWER, reviewer("pass")),
            ]
        )
        paths = DatasetPaths(work, ACC)

        pipeline.process_dataset(work, ACC, "t", config)

        prompt = (paths.step_dir(Step.CREATOR) / "prompt.md").read_text()
        assert "Repair brief (attempt 3)" in prompt
        assert MISSING_COLUMN in prompt
        assert "-t ms-proteomics" in prompt
        assert "MONDO:0005061" not in prompt

    def test_a_file_that_never_validates_exhausts_the_repair_cap(
        self, work, config, fake_agent, validator
    ):
        validator.rule = reject_marked_content
        agent = fake_agent([(Step.CREATOR, creator_ok(INVALID_SDRF))] * 3)

        rollup = pipeline.process_dataset(work, ACC, "t", config.replace(max_repair=2))

        assert rollup.state is State.BLOCKED
        assert Step.REVIEWER not in agent.calls
        assert read_json(DatasetPaths(work, ACC).validation)["live"] is False

    def test_a_validator_that_could_not_run_does_not_gate(
        self, work, config, fake_agent, validator
    ):
        validator.rule = lambda sdrf, templates, cache_only: verdict(None, ran=False)
        agent = fake_agent(
            [(Step.CREATOR, creator_ok()), (Step.REVIEWER, reviewer("pass"))]
        )

        rollup = pipeline.process_dataset(work, ACC, "t", config)

        assert agent.calls == [Step.CREATOR, Step.REVIEWER]
        assert rollup.state is State.REVIEWED_PASS


def reject_live_original(sdrf: Path, templates: list[str], cache_only: bool):
    """Passes the cached check; live OLS rejects the unrepaired file."""
    if not cache_only and "run 1" in sdrf.read_text():
        return verdict(False, messages=["ERROR: term not found in live OLS"])
    return verdict(True)


class TestRepair:
    @pytest.fixture
    def reviewed(self, work, config, fake_agent, validator) -> DatasetPaths:
        fake_agent([(Step.CREATOR, creator_ok()), (Step.REVIEWER, reviewer("pass"))])
        pipeline.process_dataset(work, ACC, "t", config)
        return DatasetPaths(work, ACC)

    @pytest.mark.parametrize(
        ("rule", "dry_run", "status", "state"),
        [
            pytest.param(
                reject_live_original, False, "marked", State.REVIEWED_FAIL, id="fail"
            ),
            pytest.param(
                reject_live_original, True, "marked", State.REVIEWED_PASS, id="dry-run"
            ),
            pytest.param(
                lambda sdrf, templates, cache_only: verdict(True),
                False, "passed", State.REVIEWED_PASS, id="pass",
            ),
            pytest.param(
                lambda sdrf, templates, cache_only: verdict(None, ran=False),
                False, "not_run", State.REVIEWED_PASS, id="not-run",
            ),
        ],
    )  # fmt: skip
    def test_only_a_live_failure_reopens_a_reviewed_dataset(
        self, work, config, validator, reviewed, rule, dry_run, status, state
    ):
        validator.rule = rule

        marks = pipeline.mark_for_repair(work, config, dry_run=dry_run)

        assert [(mark.accession, mark.status) for mark in marks] == [(ACC, status)]
        assert pipeline.load_rollup(reviewed).state is state
        assert reviewed.validation.exists() is (state is State.REVIEWED_FAIL)

    def test_a_dataset_not_at_reviewed_pass_is_skipped(self, work, config, validator):
        DatasetPaths(work, ACC).scaffold()
        pipeline.write_json(
            DatasetPaths(work, ACC).rollup, {"accession": ACC, "state": "blocked"}
        )

        marks = pipeline.mark_for_repair(work, config)

        assert marks[0].status == "skipped"
        assert validator.calls == []

    def test_a_marked_dataset_is_repaired_from_the_live_errors(
        self, work, config, fake_agent, validator, reviewed
    ):
        validator.rule = reject_live_original
        pipeline.mark_for_repair(work, config)
        agent = fake_agent(
            [(Step.CREATOR, creator_ok(FIXED_SDRF)), (Step.REVIEWER, reviewer("pass"))]
        )

        rollup = pipeline.process_dataset(work, ACC, "t", config)

        prompt = (reviewed.step_dir(Step.CREATOR) / "prompt.md").read_text()
        assert agent.calls == [Step.CREATOR, Step.REVIEWER]
        assert rollup.state is State.REVIEWED_PASS
        assert "term not found in live OLS" in prompt
        assert "without `--use_ols_cache_only`" in prompt


class TestLiveValidation:
    @pytest.mark.parametrize(
        ("files", "expected"),
        [
            pytest.param(
                {
                    "a-cell-lines.sdrf.tsv": sdrf_with_templates(("cell-lines",)),
                    "a-yeast.sdrf.tsv": sdrf_with_templates(("ms-proteomics",)),
                },
                {
                    ("a-cell-lines.sdrf.tsv", ("ms-proteomics", "cell-lines")),
                    ("a-yeast.sdrf.tsv", ("ms-proteomics",)),
                },
                id="sibling-files",
            ),
            pytest.param(
                {
                    "a.sdrf.tsv": sdrf_with_templates(
                        ("ms-proteomics",), ("ms-proteomics", "dia-acquisition")
                    )
                },
                {
                    ("group0-a.sdrf.tsv", ("ms-proteomics",)),
                    ("group1-a.sdrf.tsv", ("ms-proteomics", "dia-acquisition")),
                },
                id="mixed-row-groups",
            ),
        ],
    )
    def test_each_file_is_checked_against_only_its_own_templates(
        self, tmp_path, validator, files, expected
    ):
        for name, content in files.items():
            (tmp_path / name).write_text(content)

        pipeline.validate_live(tmp_path, RunConfig())

        assert {(name, templates) for name, templates, _ in validator.calls} == expected
        assert all(cache_only is False for *_, cache_only in validator.calls)

    def test_contribute_reports_the_failing_file(self, work, validator):
        paths = DatasetPaths(work, ACC)
        paths.scaffold()
        (paths.sdrf / f"{ACC}-ok.sdrf.tsv").write_text(FIXED_SDRF)
        (paths.sdrf / f"{ACC}-bad.sdrf.tsv").write_text(INVALID_SDRF)
        pipeline.write_json(paths.rollup, {"accession": ACC, "state": "reviewed_pass"})
        validator.rule = reject_marked_content
        (candidate,) = contribute.select_candidates(work, {})

        failure = contribute._validate_live(candidate, RunConfig())

        assert failure == f"{ACC}-bad.sdrf.tsv: {MISSING_COLUMN}"


class TestValidatorOutput:
    @pytest.mark.parametrize(
        ("returncode", "output", "passed", "messages"),
        [
            pytest.param(0, "WARNING: w\n", True, [], id="pass"),
            pytest.param(
                1, "ERROR: a\nWARNING: w\nERROR: b\n", False, ["ERROR: a", "ERROR: b"],
                id="every-error-line",
            ),
            pytest.param(
                1, "Traceback\n  frame\nKeyError: 'x'\n", False,
                ["Traceback", "  frame", "KeyError: 'x'"], id="crash-without-error-lines",
            ),
        ],
    )  # fmt: skip
    def test_messages(self, monkeypatch, returncode, output, passed, messages):
        monkeypatch.setattr(
            "annotate.runner.subprocess.run",
            lambda command, **kwargs: subprocess.CompletedProcess(
                command, returncode, output, ""
            ),
        )

        result = real_validate_sdrf(Path("x.sdrf.tsv"), ["ms-proteomics"], RunConfig())

        assert result["passed"] is passed
        assert result["messages"] == messages

    def test_a_timeout_is_not_a_verdict(self, monkeypatch):
        def time_out(command, **kwargs):
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        monkeypatch.setattr("annotate.runner.subprocess.run", time_out)

        result = real_validate_sdrf(
            Path("x.sdrf.tsv"), [], RunConfig(), use_ols_cache_only=False
        )

        assert result["ran"] is False
        assert result["detail"] == "validator timed out after 1200 s"
