"""What a finished batch has to be able to answer: cost, cause, gaps, provenance.

None of this changes what the pipeline does. It is all recorded so that 150
unreviewed runs can be analysed afterwards, and almost none of it can be
backfilled once the batch has run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from annotate import analysis, artifacts, contracts, pipeline, prompts, runner
from annotate.models import (
    DatasetPaths,
    DatasetRollup,
    Event,
    FailureKind,
    HistoryEntry,
    RunResult,
    State,
    Step,
    TransitionError,
)
from annotate.models import transition as apply_transition
from annotate.utils import read_json, read_jsonl, write_json
from test_pipeline import ACC, ERROR_FINDING, creator_ok, reviewer

RESULT_EVENT = {
    "type": "result",
    "num_turns": 67,
    "total_cost_usd": 6.63,
    "usage": {
        "input_tokens": 124,
        "output_tokens": 72031,
        "cache_read_input_tokens": 7355378,
        "cache_creation_input_tokens": 184111,
    },
    "modelUsage": {"claude-opus-5[1m]": {"costUSD": 6.63}},
    "api_error_status": None,
    "permission_denials": [],
}


class TestUsageAccounting:
    """A run's cost has to be recomputable and attributable afterwards."""

    def test_result_event_usage_is_copied_verbatim(self):
        result = RunResult(exit_code=0, result_event=RESULT_EVENT)

        usage = result.usage

        assert usage["cache_creation_input_tokens"] == 184111
        assert usage["model_usage"] == RESULT_EVENT["modelUsage"]
        assert usage["cost_usd"] == 6.63
        assert usage["source"] == "result_event"

    def test_a_killed_run_is_costed_from_the_stream(self):
        """The runs that cost the most are the ones that never report a result."""
        result = RunResult(
            exit_code=-9,
            timed_out=True,
            streamed_usage={"input_tokens": 124, "cache_read_input_tokens": 7355378},
        )

        usage = result.usage

        assert usage["source"] == "stream"
        assert usage["cache_read_input_tokens"] == 7355378
        # Not zero, which would book the run as free and silently undercount.
        assert usage["cost_usd"] is None

    def test_one_message_split_across_events_is_counted_once(self):
        """Every event of a message repeats the same usage object."""
        totals: dict = {}
        seen: set[str] = set()
        message = {
            "id": "msg_1",
            "model": "claude-opus-5",
            "usage": {"input_tokens": 10, "cache_read_input_tokens": 1000},
        }

        for _ in range(3):
            runner.accumulate_usage(totals, message, seen)

        assert totals["input_tokens"] == 10
        assert totals["cache_read_input_tokens"] == 1000
        assert totals["num_turns"] == 1
        assert totals["output_tokens_partial"] is True

    def test_separate_messages_accumulate_per_model(self):
        totals: dict = {}
        seen: set[str] = set()

        for index in range(2):
            runner.accumulate_usage(
                totals,
                {"id": f"msg_{index}", "model": "m", "usage": {"input_tokens": 5}},
                seen,
            )

        assert totals["input_tokens"] == 10
        assert totals["model_usage"]["m"]["input_tokens"] == 10


class TestFailureKind:
    """`failed_infra` alone cannot distinguish an OOM from a docker refusal."""

    @pytest.mark.parametrize(
        ("result", "expected"),
        [
            (RunResult(exit_code=0), None),
            (RunResult(exit_code=-9, over_budget="raw cap"), FailureKind.DISK_BUDGET),
            (RunResult(exit_code=1, auth_failed=True), FailureKind.AUTH),
            (RunResult(exit_code=-9, timed_out=True), FailureKind.TIMEOUT),
            (RunResult(exit_code=137), FailureKind.OOM),
            (RunResult(exit_code=125), FailureKind.DOCKER_ERROR),
            (RunResult(exit_code=1), FailureKind.AGENT_FAILED),
            (
                RunResult(
                    exit_code=0,
                    result_event={"api_error_status": "overloaded_error"},
                ),
                FailureKind.API_ERROR,
            ),
        ],
    )
    def test_each_cause_gets_its_own_name(self, result, expected):
        assert pipeline.classify_failure(result, None, None) is expected

    def test_a_contract_failure_on_a_clean_exit_is_named(self):
        assert (
            pipeline.classify_failure(RunResult(exit_code=0), "bad json", None)
            is FailureKind.CONTRACT
        )

    def test_an_agent_reporting_its_own_failure_is_named(self):
        assert (
            pipeline.classify_failure(RunResult(exit_code=0), None, {"outcome": "failed"})
            is FailureKind.AGENT_FAILED
        )

    def test_the_kind_reaches_the_run_status(self, work, config, fake_agent):
        fake_agent([(Step.CREATOR, creator_ok())], exit_code=137)

        pipeline.process_dataset(work, ACC, "t", config)

        status = read_json(DatasetPaths(work, ACC).status(Step.CREATOR))
        assert status["failure_kind"] == "oom"


class TestEventLog:
    """The rollup is derived and gets rewritten; the event log does not."""

    def test_every_transition_is_appended(self, work, config, fake_agent):
        fake_agent([(Step.CREATOR, creator_ok()), (Step.REVIEWER, reviewer("pass"))])

        pipeline.process_dataset(work, ACC, "t", config)

        events = read_jsonl(DatasetPaths(work, ACC).events)
        assert [e["event"] for e in events] == [
            "start_creator",
            "creator_completed",
            "start_reviewer",
            "review_pass",
        ]

    def test_a_rebuild_does_not_rewrite_the_log(self, work, config, fake_agent):
        fake_agent([(Step.CREATOR, creator_ok()), (Step.REVIEWER, reviewer("pass"))])
        pipeline.process_dataset(work, ACC, "t", config)
        paths = DatasetPaths(work, ACC)
        before = read_jsonl(paths.events)

        pipeline.rebuild_rollups(work, config.max_repair)

        assert read_jsonl(paths.events) == before

    def test_a_rollup_written_before_the_log_existed_is_seeded(self, work, config):
        """The history inside an old rollup is the only record of those events."""
        paths = DatasetPaths(work, ACC)
        paths.scaffold()
        rollup = DatasetRollup(accession=ACC, state=State.PENDING)
        rollup.history = [
            HistoryEntry("t0", State.PENDING, Event.START_CREATOR, State.CREATING)
        ]
        write_json(paths.rollup, rollup.to_dict())

        pipeline.rebuild_rollups(work, config.max_repair)

        assert read_jsonl(paths.events)[0]["event"] == "start_creator"

    def test_a_re_derivation_that_disagrees_records_why(self, work, config, fake_agent):
        """A contract fix is retroactive, so the log has to explain the change."""
        fake_agent([(Step.CREATOR, creator_ok()), (Step.REVIEWER, reviewer("pass"))])
        pipeline.process_dataset(work, ACC, "t", config)
        paths = DatasetPaths(work, ACC)
        # A run rejected under an older contract rule, later recovered: the log
        # ends at the rejection while derivation now reaches reviewed_pass.
        paths.events.write_text(
            json.dumps(
                HistoryEntry(
                    "t0", State.CREATING, Event.CONTRACT_FAILURE, State.FAILED_CONTRACT
                ).to_dict()
            )
            + "\n"
        )

        pipeline.rebuild_rollups(work, config.max_repair)

        events = read_jsonl(paths.events)
        assert events[-1]["event"] == "rederived"
        assert events[-1]["from"] == str(State.FAILED_CONTRACT)
        assert events[-1]["to"] == read_json(paths.rollup)["state"]

    def test_rederived_is_not_a_legal_transition(self):
        """It describes a correction, not something the pipeline can do."""
        with pytest.raises(TransitionError):
            apply_transition(State.FAILED_CONTRACT, Event.REDERIVED)


class TestArtifactSummary:
    """`created` means a file exists; it has to also mean the file parses."""

    def _write(self, paths, per_row_templates, extra_columns=()):
        """Write an SDRF whose template column is repeated once per template.

        That repetition is how a real SDRF names its templates -- all of them
        apply to every row -- and it is also why the columns cannot be keyed
        by name.
        """
        width = max(len(names) for names in per_row_templates)
        header = ["source name", *extra_columns] + [
            artifacts.TEMPLATE_COLUMN for _ in range(width)
        ]
        out = ["\t".join(header)]
        for index, names in enumerate(per_row_templates, start=1):
            padded = list(names) + [""] * (width - len(names))
            out.append(
                "\t".join(
                    [f"sample {index}", *extra_columns]
                    + [f"NT={name};VV=v1.0.0" if name else "" for name in padded]
                )
            )
        (paths.sdrf / f"{ACC}.sdrf.tsv").write_text("\n".join(out) + "\n")

    def test_every_template_column_is_read(self, work):
        paths = DatasetPaths(work, ACC)
        paths.scaffold()
        self._write(paths, [["ms-proteomics", "human"], ["ms-proteomics", "human"]])

        summary = artifacts.summarize(paths.sdrf)

        assert summary == [
            {
                "path": f"sdrf/{ACC}.sdrf.tsv",
                "rows": 2,
                "columns": 3,
                "templates": ["ms-proteomics", "human"],
                "validation": [],
            }
        ]

    def test_a_uniform_file_is_validated_whole_against_the_union(self, work):
        """Every row is subject to every template, which is what the union means."""
        paths = DatasetPaths(work, ACC)
        paths.scaffold()
        self._write(paths, [["human", "dia-acquisition"]])
        seen = []

        artifacts.summarize(
            paths.sdrf, validate=lambda path, names: seen.append(names) or {"ran": True}
        )

        assert seen == [["ms-proteomics", "human", "dia-acquisition"]]

    def test_duplicate_columns_survive_a_split(self, work, tmp_path):
        """An SDRF repeats columns by design; keying by name loses all but one."""
        paths = DatasetPaths(work, ACC)
        paths.scaffold()
        self._write(
            paths,
            [["single-cell"], ["dia-acquisition"]],
            extra_columns=("comment[modification parameters]",) * 2,
        )
        validated = []

        artifacts.summarize(
            paths.sdrf,
            validate=lambda path, names: validated.append((path, names)) or {"ran": True},
            scratch=tmp_path / "scratch",
        )

        for target, _ in validated:
            header = target.read_text().splitlines()[0].split("\t")
            assert header.count("comment[modification parameters]") == 2

    def test_a_file_mixing_template_sets_is_split(self, work, tmp_path):
        """The union would impose each group's constraints on the other's rows."""
        paths = DatasetPaths(work, ACC)
        paths.scaffold()
        self._write(paths, [["single-cell"], ["dia-acquisition"]])
        validated = []

        artifacts.summarize(
            paths.sdrf,
            validate=lambda path, names: validated.append((path, names)) or {"ran": True},
            scratch=tmp_path / "scratch",
        )

        assert [names for _, names in validated] == [
            ["ms-proteomics", "single-cell"],
            ["ms-proteomics", "dia-acquisition"],
        ]
        for target, _ in validated:
            assert len(target.read_text().strip().splitlines()) == 2

    def test_the_summary_reaches_the_run_status(self, work, config, fake_agent):
        fake_agent([(Step.CREATOR, creator_ok()), (Step.REVIEWER, reviewer("pass"))])

        pipeline.process_dataset(work, ACC, "t", config)

        status = read_json(DatasetPaths(work, ACC).status(Step.CREATOR))
        assert status["artifact_summary"][0]["rows"] == 1
        assert status["artifact_summary"][0]["validation"][0]["passed"] is True

    def test_validation_can_be_turned_off(self, work, config, fake_agent):
        fake_agent([(Step.CREATOR, creator_ok())], exit_code=1)

        pipeline.process_dataset(work, ACC, "t", config.replace(validate_artifacts=False))

        status = read_json(DatasetPaths(work, ACC).status(Step.CREATOR))
        assert status["artifact_summary"][0]["validation"] == []


class TestReviewReport:
    """The reviewer's report is an output, not a side effect of a passing run."""

    def _reviewer_writing(self, verdict: str, report: str = "{}"):
        """A reviewer that writes its report the way the prompt asks."""
        inner = reviewer(verdict, findings=[ERROR_FINDING] if verdict == "fail" else [])

        def produce(paths):
            paths.report().write_text(report)
            return inner(paths)

        return produce

    def test_the_report_survives_the_run(self, work, config, fake_agent):
        fake_agent(
            [
                (Step.CREATOR, creator_ok()),
                (Step.REVIEWER, self._reviewer_writing("pass", '{"verdict": "PASS"}')),
            ]
        )

        pipeline.process_dataset(work, ACC, "t", config)

        assert DatasetPaths(work, ACC).report().read_text() == '{"verdict": "PASS"}'

    def test_a_rejection_keeps_its_report_too(self, work, config, fake_agent):
        """The report of a failed review is the one worth reading."""
        fake_agent(
            [
                (Step.CREATOR, creator_ok()),
                (Step.REVIEWER, self._reviewer_writing("fail", '{"verdict": "FAIL"}')),
                (Step.CREATOR, creator_ok()),
                (Step.REVIEWER, self._reviewer_writing("pass", '{"verdict": "PASS"}')),
            ]
        )

        pipeline.process_dataset(work, ACC, "t", config)

        paths = DatasetPaths(work, ACC)
        assert paths.report().read_text() == '{"verdict": "PASS"}'
        archived = paths.review / "attempt-1" / paths.report().name
        assert archived.read_text() == '{"verdict": "FAIL"}'

    def test_a_missing_report_is_noted_but_does_not_fail_the_run(
        self, work, config, fake_agent
    ):
        fake_agent([(Step.CREATOR, creator_ok()), (Step.REVIEWER, reviewer("pass"))])

        rollup = pipeline.process_dataset(work, ACC, "t", config)

        assert rollup.state is State.REVIEWED_PASS
        notes = read_json(DatasetPaths(work, ACC).status(Step.REVIEWER))["notes"]
        assert any("no review report" in note for note in notes)

    def test_the_prompt_names_the_path_the_host_looks_for(self, config):
        """The host must not have to guess where the agent put its report."""
        rendered = prompts.build(Step.REVIEWER, ACC, "t", config)

        assert f"review/{DatasetPaths(Path('.'), ACC).report().name}" in rendered


class TestProvenance:
    """A batch spanning a prompt change cannot be partitioned without this."""

    def test_the_image_and_skills_are_stamped_on_every_run(
        self, work, config, fake_agent
    ):
        fake_agent([(Step.CREATOR, creator_ok())], exit_code=1)

        pipeline.process_dataset(work, ACC, "t", config)

        provenance = read_json(DatasetPaths(work, ACC).status(Step.CREATOR))["provenance"]
        assert provenance["image_id"] == "sha256:test"
        assert provenance["skills_version"] == "0"


class TestGapReporting:
    """A sentence cannot be counted across 150 datasets; a column can."""

    def test_a_legacy_sentence_keeps_its_text_and_stays_valid(self):
        payload = {
            "schema_version": "1.0.0",
            "role": "creator",
            "accession": ACC,
            "outcome": "completed",
            "sdrf_files": [f"sdrf/{ACC}.sdrf.tsv"],
            "unresolved": ["sample->file mapping for 4 runs not determinable"],
        }

        migrated = contracts.normalize(Step.CREATOR, payload)

        assert migrated["unresolved"] == [
            {
                "column": None,
                "detail": "sample->file mapping for 4 runs not determinable",
            }
        ]
        assert contracts.validate_contract(migrated, Step.CREATOR) is None

    @pytest.mark.parametrize("field", ["spec_gaps", "unresolved"])
    def test_a_column_the_file_does_not_have_is_noted(self, field):
        payload = {field: [{"column": "characteristics[age]", "detail": "absent"}]}

        notes = contracts.gap_notes(payload, {"source name"})

        assert notes == [
            f"{field} names column(s) absent from the SDRF: characteristics[age]"
        ]

    def test_a_real_column_is_not_noted(self):
        payload = {"spec_gaps": [{"column": "source name", "detail": "x"}]}

        assert contracts.gap_notes(payload, {"source name"}) == []

    def test_a_wrong_column_does_not_fail_the_run(self, work, config, fake_agent):
        """Bookkeeping must never throw away a 30-minute annotation."""
        fake_agent(
            [
                (
                    Step.CREATOR,
                    creator_ok(
                        spec_gaps=[{"column": "comment[invented]", "detail": "nope"}]
                    ),
                ),
                (Step.REVIEWER, reviewer("pass")),
            ]
        )

        rollup = pipeline.process_dataset(work, ACC, "t", config)

        assert rollup.state is State.REVIEWED_PASS
        notes = read_json(DatasetPaths(work, ACC).status(Step.CREATOR))["notes"]
        assert any("comment[invented]" in note for note in notes)


class TestSourcesRecord:
    """Provenance is only aggregable if every dataset records it the same way."""

    VALID = {
        "accession": ACC,
        "sources": [
            {"id": "pride-project", "url": "https://example.org", "used_for": ["x"]}
        ],
    }

    def test_a_sound_record_is_silent(self, tmp_path):
        (tmp_path / "sources.json").write_text(json.dumps(self.VALID))

        assert contracts.sources_notes(tmp_path) == []

    def test_a_missing_record_is_noted(self, tmp_path):
        assert contracts.sources_notes(tmp_path) == ["files/sources.json missing"]

    @pytest.mark.parametrize(
        "sources",
        [
            [{"id": "x", "used_for": ["y"]}],
            [{"url": "https://example.org", "used_for": ["y"]}],
            [{"id": "x", "url": "https://example.org"}],
        ],
        ids=["no locator", "no id", "no used_for"],
    )
    def test_an_entry_missing_the_core_is_noted(self, tmp_path, sources):
        (tmp_path / "sources.json").write_text(
            json.dumps({"accession": ACC, "sources": sources})
        )

        assert contracts.sources_notes(tmp_path) != []

    def test_a_local_only_source_needs_no_url(self, tmp_path):
        """A raw-file header is a real source with no URL to cite."""
        (tmp_path / "sources.json").write_text(
            json.dumps(
                {
                    "accession": ACC,
                    "sources": [
                        {"id": "raw-header", "local": "files/h.txt", "used_for": "x"}
                    ],
                }
            )
        )

        assert contracts.sources_notes(tmp_path) == []


class TestPhaseAttribution:
    """Where a run's money went, recovered from the trace."""

    @pytest.mark.parametrize(
        ("name", "payload", "expected"),
        [
            ("WebFetch", {}, "literature"),
            ("Skill", {}, "spec"),
            ("mcp__sdrf-pride-pmc__get_project_details", {}, "pride"),
            ("mcp__sdrf-pride-pmc__searchClasses", {}, "ontology"),
            ("Bash", {"command": "parse_sdrf validate-sdrf -t human"}, "validation"),
            ("Bash", {"command": "sed -n 1,40p spec/sdrf-proteomics/TERMS.tsv"}, "spec"),
            ("Bash", {"command": "ThermoRawFileParser -i a.raw"}, "raw"),
            ("Bash", {"command": "openpyxl MOESM5.xlsx"}, "literature"),
            ("Bash", {"command": "curl https://www.ebi.ac.uk/pride/ws/x"}, "pride"),
            ("Bash", {"command": "sleep 45; tail -3 tasks/bm.output"}, "waiting"),
            ("Bash", {"command": f"head -1 sdrf/{ACC}.sdrf.tsv"}, "authoring"),
            ("Bash", {"command": "echo hello"}, "other"),
        ],
    )
    def test_a_tool_call_is_classified(self, name, payload, expected):
        assert analysis.phase_of_tool(name, payload) == expected

    def test_a_message_is_charged_to_the_tool_it_called(self):
        content = [
            {"type": "thinking", "thinking": "..."},
            {"type": "tool_use", "name": "WebFetch", "input": {}},
        ]

        assert analysis.message_phase(content) == "literature"

    def test_a_message_that_calls_nothing_is_reasoning(self):
        assert analysis.message_phase([{"type": "text", "text": "done"}]) == "reasoning"

    def test_events_of_one_message_are_reassembled(self):
        events = [
            {"subtype": "thinking_tokens", "estimated_tokens_delta": 40},
            {
                "type": "assistant",
                "message": {
                    "id": "m1",
                    "usage": {"input_tokens": 3},
                    "content": [{"type": "thinking"}],
                },
            },
            {
                "type": "assistant",
                "message": {
                    "id": "m1",
                    "usage": {"input_tokens": 3},
                    "content": [{"type": "tool_use", "name": "WebFetch", "input": {}}],
                },
            },
        ]

        messages = analysis.group_messages(events)

        assert len(messages) == 1
        assert messages[0]["thinking_tokens"] == 40
        assert analysis.message_phase(messages[0]["content"]) == "literature"

    def test_a_run_is_split_between_its_phases(self, tmp_path):
        trace = tmp_path / "session.jsonl"
        trace.write_text(
            "\n".join(
                json.dumps(event)
                for event in [
                    {"subtype": "thinking_tokens", "estimated_tokens_delta": 100},
                    {
                        "type": "assistant",
                        "message": {
                            "id": "m1",
                            "usage": {"cache_read_input_tokens": 1000},
                            "content": [
                                {"type": "tool_use", "name": "WebFetch", "input": {}}
                            ],
                        },
                    },
                    {"subtype": "thinking_tokens", "estimated_tokens_delta": 100},
                    {
                        "type": "assistant",
                        "message": {
                            "id": "m2",
                            "usage": {"cache_read_input_tokens": 1000},
                            "content": [
                                {
                                    "type": "tool_use",
                                    "name": "Bash",
                                    "input": {"command": "parse_sdrf validate-sdrf"},
                                }
                            ],
                        },
                    },
                    {
                        "type": "result",
                        "total_cost_usd": 2.0,
                        "usage": {"output_tokens": 400},
                    },
                ]
            )
        )

        report = analysis.phase_costs(trace)

        assert report["phases"]["literature"]["cost_usd"] == 1.0
        assert report["phases"]["validation"]["cost_usd"] == 1.0
        # 200 thinking tokens attributed against 400 output tokens billed.
        assert report["output_coverage"] == 0.5
