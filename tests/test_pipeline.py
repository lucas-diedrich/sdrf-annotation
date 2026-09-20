"""End-to-end per-dataset pipeline against a fake agent: pass, fail, blocked, resume."""

from __future__ import annotations

import json

import pytest

from annotate import pipeline, runner
from annotate.contracts import hash_artifacts
from annotate.models import DatasetPaths, State, Step
from annotate.utils import read_json, write_json

ACC = "PXD000001"
DEFAULT_SDRF = "source name\tassay name\nsample 1\trun 1\n"


def creator_ok(content: str = DEFAULT_SDRF, **extra):
    def produce(paths):
        (paths.sdrf / f"{ACC}.sdrf.tsv").write_text(content)
        payload = {
            "schema_version": "1.0.0",
            "role": "creator",
            "accession": ACC,
            "outcome": "completed",
            "blocked_reason": None,
            "assumptions": ["label-free inferred from Methods"],
            "unresolved": [],
            "artifacts": [f"sdrf/{ACC}.sdrf.tsv"],
            "raw_files": {"paths": [], "bytes": 0},
        }
        payload.update(extra)
        return f"Done.\n\n```json\n{json.dumps(payload)}\n```\n"

    return produce


def creator_blocked(reason: str):
    def produce(paths):
        payload = {
            "schema_version": "1.0.0",
            "role": "creator",
            "accession": ACC,
            "outcome": "blocked",
            "blocked_reason": reason,
            "assumptions": [],
            "unresolved": [],
            "artifacts": [],
        }
        return f"```json\n{json.dumps(payload)}\n```"

    return produce


def reviewer(verdict: str, findings=None, **extra):
    def produce(paths):
        payload = {
            "schema_version": "1.0.0",
            "role": "reviewer",
            "accession": ACC,
            "artifacts": [
                {"path": path, "sha256": digest}
                for path, digest in sorted(hash_artifacts(paths.sdrf).items())
            ],
            "verdict": verdict,
            "blocked_reason": None,
            "deterministic": [
                {"check": "parse_sdrf", "templates": ["ms-proteomics"], "passed": True}
            ],
            "findings": findings or [],
            "literature_agreement": {"reviewed": ["35695565"], "contradictions": []},
        }
        payload.update(extra)
        return f"```json\n{json.dumps(payload)}\n```"

    return produce


ERROR_FINDING = {
    "severity": "error",
    "file": f"sdrf/{ACC}.sdrf.tsv",
    "row": 2,
    "column": "characteristics[disease]",
    "claim": "annotated as melanoma",
    "evidence": "PMID 35695565 Methods para 3 states lung adenocarcinoma",
    "recommendation": "MONDO:0005061",
}


class TestHappyPath:
    def test_creator_then_reviewer_pass(self, work, config, fake_agent):
        agent = fake_agent(
            [(Step.CREATOR, creator_ok()), (Step.REVIEWER, reviewer("pass"))]
        )

        rollup = pipeline.process_dataset(work, ACC, "A test dataset", config)

        assert rollup.state is State.REVIEWED_PASS
        assert agent.calls == [Step.CREATOR, Step.REVIEWER]

    def test_run_statuses_are_written_for_both_steps(self, work, config, fake_agent):
        fake_agent([(Step.CREATOR, creator_ok()), (Step.REVIEWER, reviewer("pass"))])
        paths = DatasetPaths(work, ACC)

        pipeline.process_dataset(work, ACC, "t", config)

        for step in Step:
            status = read_json(paths.status(step))
            assert status["contract"]["valid"] is True
            assert status["accession"] == ACC
            assert status["artifacts"][0]["path"] == f"sdrf/{ACC}.sdrf.tsv"
            assert status["usage"]["input_tokens"] == 10

    def test_terminal_dataset_is_skipped_on_rerun(self, work, config, fake_agent):
        fake_agent([(Step.CREATOR, creator_ok()), (Step.REVIEWER, reviewer("pass"))])
        pipeline.process_dataset(work, ACC, "t", config)

        agent = fake_agent([])  # any call would pop from an empty script and raise
        rollup = pipeline.process_dataset(work, ACC, "t", config)

        assert rollup.state is State.REVIEWED_PASS
        assert agent.calls == []


class TestRepairLoop:
    def test_fail_then_repair_then_pass(self, work, config, fake_agent):
        agent = fake_agent(
            [
                (Step.CREATOR, creator_ok()),
                (Step.REVIEWER, reviewer("fail", [ERROR_FINDING])),
                (Step.CREATOR, creator_ok("source name\tassay name\nsample 1\tfixed\n")),
                (Step.REVIEWER, reviewer("pass")),
            ]
        )

        rollup = pipeline.process_dataset(work, ACC, "t", config)

        assert rollup.state is State.REVIEWED_PASS
        assert agent.calls == [Step.CREATOR, Step.REVIEWER, Step.CREATOR, Step.REVIEWER]
        assert rollup.attempts == 2

    def test_repair_brief_reaches_the_creator_prompt(self, work, config, fake_agent):
        fake_agent(
            [
                (Step.CREATOR, creator_ok()),
                (Step.REVIEWER, reviewer("fail", [ERROR_FINDING])),
                (Step.CREATOR, creator_ok()),
                (Step.REVIEWER, reviewer("pass")),
            ]
        )
        paths = DatasetPaths(work, ACC)

        pipeline.process_dataset(work, ACC, "t", config)

        prompt = (paths.step_dir(Step.CREATOR) / "prompt.md").read_text()
        assert "Repair brief (attempt 2)" in prompt
        assert "MONDO:0005061" in prompt
        assert "lung adenocarcinoma" in prompt

    def test_first_attempt_has_no_repair_brief(self, work, config, fake_agent):
        fake_agent([(Step.CREATOR, creator_ok()), (Step.REVIEWER, reviewer("pass"))])
        paths = DatasetPaths(work, ACC)

        pipeline.process_dataset(work, ACC, "t", config)

        assert (
            "Repair brief" not in (paths.step_dir(Step.CREATOR) / "prompt.md").read_text()
        )

    def test_repair_cap_ends_in_blocked(self, work, config, fake_agent):
        fake_agent(
            [
                (Step.CREATOR, creator_ok("v1\n")),
                (Step.REVIEWER, reviewer("fail", [ERROR_FINDING])),
                (Step.CREATOR, creator_ok("v2\n")),
                (Step.REVIEWER, reviewer("fail", [ERROR_FINDING])),
                (Step.CREATOR, creator_ok("v3\n")),
                (Step.REVIEWER, reviewer("fail", [ERROR_FINDING])),
            ]
        )

        rollup = pipeline.process_dataset(work, ACC, "t", config.replace(max_repair=2))

        assert rollup.state is State.BLOCKED
        assert "repair cap" in rollup.blocked_reason

    def test_previous_attempt_is_archived_not_overwritten(self, work, config, fake_agent):
        fake_agent(
            [
                (Step.CREATOR, creator_ok("v1\n")),
                (Step.REVIEWER, reviewer("fail", [ERROR_FINDING])),
                (Step.CREATOR, creator_ok("v2\n")),
                (Step.REVIEWER, reviewer("pass")),
            ]
        )
        paths = DatasetPaths(work, ACC)

        pipeline.process_dataset(work, ACC, "t", config)

        archived = read_json(paths.step_dir(Step.CREATOR) / "attempt-1" / "status.json")
        assert archived["attempt"] == 1
        assert read_json(paths.status(Step.CREATOR))["attempt"] == 2


class TestNegativeReview:
    """Phase 3's real test: the reviewer must reject a corrupted artifact."""

    def test_reviewer_rejecting_an_artifact_blocks_the_pass(
        self, work, config, fake_agent
    ):
        fake_agent(
            [
                (Step.CREATOR, creator_ok()),
                (Step.REVIEWER, reviewer("fail", [ERROR_FINDING])),
            ]
        )

        rollup = pipeline.process_dataset(work, ACC, "t", config.replace(max_repair=0))

        assert rollup.state is not State.REVIEWED_PASS

    def test_verdict_on_a_stale_hash_is_discarded(self, work, config, fake_agent):
        """A reviewer that judged different bytes than are on disk is not trusted."""

        def stale_reviewer(paths):
            body = reviewer("pass")(paths).removeprefix("```json\n").removesuffix("\n```")
            payload = json.loads(body)
            payload["artifacts"][0]["sha256"] = "b" * 64
            return f"```json\n{json.dumps(payload)}\n```"

        fake_agent([(Step.CREATOR, creator_ok()), (Step.REVIEWER, stale_reviewer)])
        paths = DatasetPaths(work, ACC)

        rollup = pipeline.process_dataset(work, ACC, "t", config)

        assert rollup.state is State.FAILED_CONTRACT
        assert (
            "sha256 mismatch"
            in read_json(paths.status(Step.REVIEWER))["contract"]["error"]
        )

    def test_reviewer_that_judged_only_some_artifacts_is_rejected(
        self, work, config, fake_agent
    ):
        def partial_reviewer(paths):
            (paths.sdrf / f"{ACC}-cell-lines.sdrf.tsv").write_text("second template\n")
            body = reviewer("pass")(paths).removeprefix("```json\n").removesuffix("\n```")
            payload = json.loads(body)
            payload["artifacts"] = [payload["artifacts"][0]]
            return f"```json\n{json.dumps(payload)}\n```"

        fake_agent([(Step.CREATOR, creator_ok()), (Step.REVIEWER, partial_reviewer)])

        rollup = pipeline.process_dataset(work, ACC, "t", config)

        assert rollup.state is State.FAILED_CONTRACT


class TestBlocked:
    def test_creator_blocked_routes_to_sandbox(self, work, config, fake_agent):
        reason = "cell-lines template leaves no legal value for tissue rows"
        agent = fake_agent([(Step.CREATOR, creator_blocked(reason))])

        rollup = pipeline.process_dataset(work, ACC, "t", config)

        assert rollup.state is State.BLOCKED
        assert agent.calls == [Step.CREATOR]  # no reviewer for a blocked dataset
        assert reason in (work / "sandbox" / ACC / "BLOCKED.md").read_text()

    def test_reviewer_blocked_is_terminal(self, work, config, fake_agent):
        fake_agent(
            [
                (Step.CREATOR, creator_ok()),
                (Step.REVIEWER, reviewer("blocked", blocked_reason="units lack second")),
            ]
        )

        rollup = pipeline.process_dataset(work, ACC, "t", config)

        assert rollup.state is State.BLOCKED
        assert (work / "sandbox" / ACC / f"{ACC}.sdrf.tsv").exists()


class TestDiskEnforcement:
    """The cap is enforced on the host, never asked of the agent."""

    class FakeProcess:
        def __init__(self):
            self.killed = False

        def kill(self):
            self.killed = True

    def test_watchdog_kills_a_run_that_crosses_the_cap(self, tmp_path, monkeypatch):
        import time

        monkeypatch.setattr(runner.RawBudgetWatchdog, "POLL_S", 0.01)
        raw = tmp_path / "raw"
        raw.mkdir()
        process = self.FakeProcess()
        watchdog = runner.RawBudgetWatchdog(raw, 1 / 1024**3, process)  # 1 byte

        watchdog.start()
        (raw / "big.raw").write_bytes(b"x" * 4096)
        deadline = time.monotonic() + 2
        while not process.killed and time.monotonic() < deadline:
            time.sleep(0.01)
        watchdog.stop()

        assert process.killed
        assert "budget exceeded" in watchdog.breach

    def test_watchdog_leaves_an_under_budget_run_alone(self, tmp_path, monkeypatch):
        import time

        monkeypatch.setattr(runner.RawBudgetWatchdog, "POLL_S", 0.01)
        raw = tmp_path / "raw"
        raw.mkdir()
        (raw / "small.raw").write_bytes(b"x" * 16)
        process = self.FakeProcess()
        watchdog = runner.RawBudgetWatchdog(raw, 1.0, process)

        watchdog.start()
        time.sleep(0.1)
        watchdog.stop()

        assert not process.killed
        assert watchdog.breach == ""

    def test_a_killed_over_budget_run_blocks_without_a_contract(
        self, work, config, fake_agent
    ):
        """A killed run has no JSON output; the breach must still classify it."""

        def killed_mid_download(paths):
            (paths.raw / "huge.raw").write_bytes(b"x" * 8192)
            return ""

        agent = fake_agent([(Step.CREATOR, killed_mid_download)], exit_code=-9)
        agent.over_budget = "raw download budget exceeded: 21.0 GB in raw/; run killed"

        rollup = pipeline.process_dataset(work, ACC, "t", config)

        assert rollup.state is State.BLOCKED
        assert "budget exceeded" in rollup.blocked_reason

    def test_post_hoc_backstop_catches_a_run_that_beat_the_poll(
        self, work, config, fake_agent
    ):
        def creator_with_a_big_download(paths):
            (paths.raw / "huge.raw").write_bytes(b"x" * 8192)
            return creator_ok()(paths)

        fake_agent([(Step.CREATOR, creator_with_a_big_download)])

        rollup = pipeline.process_dataset(
            work, ACC, "t", config.replace(raw_budget_gb=1e-6, keep_raw=True)
        )

        assert rollup.state is State.BLOCKED
        assert "budget exceeded" in rollup.blocked_reason

    def test_prompts_do_not_ask_the_agent_to_police_disk(self):
        from annotate import prompts

        for step in Step:
            prompt = (prompts.PROMPT_DIR / f"{step}.md").read_text()
            assert "{{RAW_BUDGET_GB}}" not in prompt
            assert " GB" not in prompt


class TestFailures:
    def test_unparseable_output_is_a_contract_failure(self, work, config, fake_agent):
        fake_agent([(Step.CREATOR, lambda paths: "I could not finish, sorry.")])

        rollup = pipeline.process_dataset(work, ACC, "t", config)

        assert rollup.state is State.FAILED_CONTRACT
        assert rollup.failed_step is Step.CREATOR

    def test_nonzero_exit_is_an_infra_failure(self, work, config, fake_agent):
        fake_agent([(Step.CREATOR, creator_ok())], exit_code=137)

        assert (
            pipeline.process_dataset(work, ACC, "t", config).state is State.FAILED_INFRA
        )

    def test_timeout_is_an_infra_failure(self, work, config, fake_agent):
        fake_agent([(Step.CREATOR, creator_ok())], timed_out=True)
        paths = DatasetPaths(work, ACC)

        rollup = pipeline.process_dataset(work, ACC, "t", config)

        assert rollup.state is State.FAILED_INFRA
        assert read_json(paths.status(Step.CREATOR))["timed_out"] is True

    def test_retry_after_a_reviewer_failure_reruns_only_the_reviewer(
        self, work, config, fake_agent
    ):
        fake_agent(
            [(Step.CREATOR, creator_ok()), (Step.REVIEWER, lambda paths: "no json")]
        )
        pipeline.process_dataset(work, ACC, "t", config)

        agent = fake_agent([(Step.REVIEWER, reviewer("pass"))])
        rollup = pipeline.process_dataset(work, ACC, "t", config)

        assert agent.calls == [Step.REVIEWER]
        assert rollup.state is State.REVIEWED_PASS


class TestResume:
    def test_a_run_killed_mid_creator_restarts_the_creator(
        self, work, config, fake_agent
    ):
        from annotate.models import Event

        paths = DatasetPaths(work, ACC)
        paths.scaffold()
        rollup = pipeline.load_rollup(paths)
        pipeline.apply_event(paths, rollup, Event.START_CREATOR, step=Step.CREATOR)

        agent = fake_agent(
            [(Step.CREATOR, creator_ok()), (Step.REVIEWER, reviewer("pass"))]
        )
        resumed = pipeline.process_dataset(work, ACC, "t", config)

        assert agent.calls == [Step.CREATOR, Step.REVIEWER]
        assert resumed.state is State.REVIEWED_PASS

    def test_a_run_killed_mid_review_restarts_only_the_reviewer(
        self, work, config, fake_agent
    ):
        fake_agent([(Step.CREATOR, creator_ok()), (Step.REVIEWER, reviewer("pass"))])
        pipeline.process_dataset(work, ACC, "t", config)
        paths = DatasetPaths(work, ACC)
        rollup = pipeline.load_rollup(paths)
        rollup.state = State.REVIEWING  # simulate a kill after the reviewer started
        write_json(paths.rollup, rollup.to_dict())

        agent = fake_agent([(Step.REVIEWER, reviewer("pass"))])
        resumed = pipeline.process_dataset(work, ACC, "t", config)

        assert agent.calls == [Step.REVIEWER]
        assert resumed.state is State.REVIEWED_PASS

    def test_rollup_rebuilds_a_deleted_dataset_rollup(self, work, config, fake_agent):
        fake_agent([(Step.CREATOR, creator_ok()), (Step.REVIEWER, reviewer("pass"))])
        pipeline.process_dataset(work, ACC, "t", config)
        paths = DatasetPaths(work, ACC)
        paths.rollup.unlink()

        pipeline.rebuild_rollups(work, max_repair=2)

        assert read_json(paths.rollup)["state"] == State.REVIEWED_PASS
        assert read_json(work / "status.json")["counts"] == {State.REVIEWED_PASS: 1}

    def test_rollup_preserves_history_it_cannot_derive(self, work, config, fake_agent):
        fake_agent([(Step.CREATOR, creator_ok()), (Step.REVIEWER, reviewer("pass"))])
        pipeline.process_dataset(work, ACC, "t", config)
        paths = DatasetPaths(work, ACC)
        before = len(pipeline.load_rollup(paths).history)

        pipeline.rebuild_rollups(work, max_repair=2)

        assert len(pipeline.load_rollup(paths).history) == before


class TestRawLifecycle:
    @staticmethod
    def _creator_with_a_download(paths):
        (paths.raw / "one.raw").write_bytes(b"x" * 1024)
        return creator_ok()(paths)

    def test_raw_is_purged_when_the_dataset_terminates(self, work, config, fake_agent):
        fake_agent(
            [
                (Step.CREATOR, self._creator_with_a_download),
                (Step.REVIEWER, reviewer("pass")),
            ]
        )
        paths = DatasetPaths(work, ACC)

        pipeline.process_dataset(work, ACC, "t", config)

        assert paths.raw.is_dir()
        assert list(paths.raw.iterdir()) == []

    def test_purge_after_creator_frees_before_the_review(self, work, config, fake_agent):
        seen = {}

        def reviewer_checking_raw(paths):
            seen["raw_entries"] = list(paths.raw.iterdir())
            return reviewer("pass")(paths)

        fake_agent(
            [
                (Step.CREATOR, self._creator_with_a_download),
                (Step.REVIEWER, reviewer_checking_raw),
            ]
        )

        pipeline.process_dataset(
            work, ACC, "t", config.replace(purge_raw_after="creator")
        )

        assert seen["raw_entries"] == []

    def test_raw_survives_the_creator_by_default(self, work, config, fake_agent):
        """The reviewer can inspect raw, and a repair reuses the download."""
        seen = {}

        def reviewer_checking_raw(paths):
            seen["raw_entries"] = [p.name for p in paths.raw.iterdir()]
            return reviewer("pass")(paths)

        fake_agent(
            [
                (Step.CREATOR, self._creator_with_a_download),
                (Step.REVIEWER, reviewer_checking_raw),
            ]
        )

        pipeline.process_dataset(work, ACC, "t", config)

        assert seen["raw_entries"] == ["one.raw"]

    def test_keep_raw_survives_a_terminal_state(self, work, config, fake_agent):
        fake_agent(
            [
                (Step.CREATOR, self._creator_with_a_download),
                (Step.REVIEWER, reviewer("pass")),
            ]
        )
        paths = DatasetPaths(work, ACC)

        pipeline.process_dataset(work, ACC, "t", config.replace(keep_raw=True))

        assert (paths.raw / "one.raw").exists()

    def test_raw_is_purged_on_a_failure_path(self, work, config, fake_agent):
        def creator_that_downloads_then_breaks(paths):
            (paths.raw / "one.raw").write_bytes(b"x" * 1024)
            return "no json block here"

        fake_agent([(Step.CREATOR, creator_that_downloads_then_breaks)])
        paths = DatasetPaths(work, ACC)

        pipeline.process_dataset(work, ACC, "t", config)

        assert list(paths.raw.iterdir()) == []

    def test_purge_command_reclaims_leftovers(self, work, config, fake_agent):
        fake_agent([(Step.CREATOR, creator_ok()), (Step.REVIEWER, reviewer("pass"))])
        pipeline.process_dataset(work, ACC, "t", config.replace(keep_raw=True))
        paths = DatasetPaths(work, ACC)
        (paths.raw / "stranded.raw").write_bytes(b"x" * 2048)

        reclaimed = pipeline.purge_workflow_raw(work)

        assert reclaimed == 2048
        assert list(paths.raw.iterdir()) == []

    def test_purge_skips_a_dataset_still_in_flight(self, work, config):
        paths = DatasetPaths(work, ACC)
        paths.scaffold()
        write_json(paths.rollup, {"accession": ACC, "state": str(State.CREATING)})
        (paths.raw / "in-use.raw").write_bytes(b"x" * 512)

        assert pipeline.purge_workflow_raw(work) == 0
        assert pipeline.purge_workflow_raw(work, include_in_flight=True) == 512


class TestMounts:
    @pytest.mark.parametrize(
        "step,expected_ro", [(Step.CREATOR, False), (Step.REVIEWER, True)]
    )
    def test_reviewer_data_mounts_are_read_only(self, work, config, step, expected_ro):
        paths = DatasetPaths(work, ACC)
        paths.scaffold()

        command = runner.build_docker_command(step, paths, "prompt", "sid", config)
        data_mounts = [
            arg
            for arg in command
            if arg.startswith(str(paths.root)) and "/workspace/" in arg
        ]

        assert len(data_mounts) == 3
        assert all(mount.endswith(":ro") == expected_ro for mount in data_mounts)

    def test_logs_are_never_mounted(self, work, config):
        paths = DatasetPaths(work, ACC)
        paths.scaffold()

        for step in Step:
            command = runner.build_docker_command(step, paths, "prompt", "sid", config)
            mounts = [command[i + 1] for i, arg in enumerate(command) if arg == "-v"]
            # The per-run config dir lives under logs/ but is mounted at /.claude,
            # so the agent still cannot reach logs/ itself.
            assert all("/workspace/logs" not in mount for mount in mounts)

    def test_scratch_space_is_a_size_capped_tmpfs(self, work, config):
        paths = DatasetPaths(work, ACC)
        paths.scaffold()

        command = runner.build_docker_command(
            Step.CREATOR, paths, "p", "s", config.replace(scratch_gb=2.0)
        )

        mount = next(command[i + 1] for i, arg in enumerate(command) if arg == "--mount")
        assert "type=tmpfs" in mount
        assert "destination=/workspace/scratchpad" in mount
        assert f"tmpfs-size={2 * 1024**3}" in mount

    def test_config_dir_is_per_run(self, work, config):
        paths = DatasetPaths(work, ACC)
        paths.scaffold()

        creator_cmd = runner.build_docker_command(Step.CREATOR, paths, "p", "s", config)
        reviewer_cmd = runner.build_docker_command(Step.REVIEWER, paths, "p", "s", config)

        assert next(a for a in creator_cmd if a.endswith(":/.claude")) != next(
            a for a in reviewer_cmd if a.endswith(":/.claude")
        )

    def test_permission_mode_is_configurable(self, work, config):
        paths = DatasetPaths(work, ACC)
        paths.scaffold()

        command = runner.build_docker_command(
            Step.CREATOR, paths, "p", "s", config.replace(permission_mode="acceptEdits")
        )

        assert command[command.index("--permission-mode") + 1] == "acceptEdits"


class TestSeedSelection:
    def test_annotated_datasets_are_skipped_by_default(self, config):
        assert [row.accession for row in pipeline.select_datasets(config)] == [ACC]

    def test_include_annotated_widens_the_selection(self, config):
        selected = pipeline.select_datasets(config.replace(include_annotated=True))

        assert [row.accession for row in selected] == [ACC, "PXD000002"]

    def test_accession_not_in_the_seed_is_still_runnable(self, config):
        selected = pipeline.select_datasets(config.replace(accessions=("PXD999999",)))

        assert [row.accession for row in selected] == ["PXD999999"]

    def test_limit_truncates(self, config):
        selected = pipeline.select_datasets(
            config.replace(include_annotated=True, limit=1)
        )

        assert len(selected) == 1

    def test_terminal_datasets_drop_out_of_the_selection(self, work, config, fake_agent):
        fake_agent([(Step.CREATOR, creator_ok()), (Step.REVIEWER, reviewer("pass"))])
        pipeline.process_dataset(work, ACC, "t", config)

        assert pipeline.select_datasets(config) == []

    def test_state_filter_selects_only_that_state(self, work, config, fake_agent):
        fake_agent([(Step.CREATOR, lambda paths: "broken")])
        pipeline.process_dataset(work, ACC, "t", config)

        selected = pipeline.select_datasets(
            config.replace(states=(State.FAILED_CONTRACT,))
        )

        assert [row.accession for row in selected] == [ACC]


class TestConfigPruning:
    def test_the_whole_config_dir_is_removed(self, work):
        paths = DatasetPaths(work, ACC)
        paths.scaffold()
        config_dir = paths.config_dir(Step.CREATOR)
        (config_dir / "plugins" / "cache").mkdir(parents=True)
        (config_dir / "plugins" / "cache" / "big.bin").write_bytes(b"x" * 4096)

        runner.prune_config_dir(paths, Step.CREATOR)

        assert not config_dir.exists()
        # session.jsonl is the trace; nothing under the config dir is kept.
        assert not (paths.step_dir(Step.CREATOR) / "transcript.jsonl").exists()

    def test_pruning_a_missing_config_dir_is_a_no_op(self, work):
        paths = DatasetPaths(work, ACC)
        paths.scaffold()

        runner.prune_config_dir(paths, Step.CREATOR)  # must not raise


class TestBatch:
    def test_dry_run_leaves_the_state_machine_untouched(self, work, config):
        """Uses the real runner: its dry-run path must not start a container."""
        paths = DatasetPaths(work, ACC)

        outcome = pipeline.run_batch(config.replace(dry_run=True))

        assert not outcome.interrupted
        assert outcome.results == {ACC: str(State.PENDING)}
        assert pipeline.load_rollup(paths).state is State.PENDING
        assert (paths.step_dir(Step.CREATOR) / "command.txt").exists()

    def test_reports_each_dataset_as_it_settles(self, work, config, fake_agent):
        fake_agent([(Step.CREATOR, creator_ok()), (Step.REVIEWER, reviewer("pass"))])
        seen = []

        outcome = pipeline.run_batch(config, on_result=lambda a, s: seen.append((a, s)))

        assert seen == [(ACC, str(State.REVIEWED_PASS))]
        assert outcome.results == {ACC: str(State.REVIEWED_PASS)}

    def test_one_dataset_crashing_does_not_sink_the_batch(
        self, work, config, monkeypatch
    ):
        def explode(work_, accession, title, config_):
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(pipeline, "process_dataset", explode)

        outcome = pipeline.run_batch(config)

        assert "orchestrator_error" in outcome.results[ACC]


class TestAuthentication:
    """The failure that burned two runs per dataset with `apiKeySource: none`."""

    AUTH_REFUSAL = (
        '{"type":"result","is_error":true,"terminal_reason":"api_error",'
        '"result":"Not logged in \u00b7 Please run /login"}'
    )

    def _refusing_agent(self, paths):
        return "Not logged in · Please run /login"

    def test_key_is_resolved_from_the_environment(self, config, monkeypatch):
        monkeypatch.setenv(runner.API_KEY_VAR, "sk-from-env")

        assert runner.resolve_api_key(config) == "sk-from-env"

    def test_key_falls_back_to_the_env_file(self, config, tmp_path, monkeypatch):
        monkeypatch.delenv(runner.API_KEY_VAR, raising=False)
        env_file = tmp_path / "custom.env"
        env_file.write_text(f'# comment\nexport {runner.API_KEY_VAR}="sk-from-file"\n')

        assert runner.resolve_api_key(config.replace(env_file=env_file)) == "sk-from-file"

    def test_environment_wins_over_the_env_file(self, config, tmp_path, monkeypatch):
        monkeypatch.setenv(runner.API_KEY_VAR, "sk-from-env")
        env_file = tmp_path / ".env"
        env_file.write_text(f"{runner.API_KEY_VAR}=sk-from-file\n")

        assert runner.resolve_api_key(config.replace(env_file=env_file)) == "sk-from-env"

    def test_env_file_may_live_outside_the_repo(self, config, tmp_path, monkeypatch):
        """The workflow must be runnable from any directory."""
        monkeypatch.delenv(runner.API_KEY_VAR, raising=False)
        elsewhere = tmp_path / "somewhere" / "else"
        elsewhere.mkdir(parents=True)
        env_file = elsewhere / "secrets.env"
        env_file.write_text(f"{runner.API_KEY_VAR}=sk-elsewhere\n")

        assert runner.resolve_api_key(config.replace(env_file=env_file)) == "sk-elsewhere"

    def test_missing_credential_raises_with_the_paths_it_searched(
        self, config, tmp_path, monkeypatch
    ):
        monkeypatch.delenv(runner.API_KEY_VAR, raising=False)
        missing = tmp_path / "absent.env"

        with pytest.raises(runner.MissingCredentials, match="absent.env"):
            runner.resolve_api_key(config.replace(env_file=missing))

    def test_key_reaches_the_container_through_the_environment(self, config, monkeypatch):
        """Never as `-e KEY=value`: the argv is written to command.txt."""
        monkeypatch.setenv(runner.API_KEY_VAR, "sk-secret-value")
        paths = DatasetPaths(config.work, ACC)
        paths.scaffold()

        command = runner.build_docker_command(Step.CREATOR, paths, "p", "s", config)
        env = runner.agent_env(config)

        assert runner.API_KEY_VAR in command  # the passthrough form
        assert not any("sk-secret-value" in arg for arg in command)
        assert env[runner.API_KEY_VAR] == "sk-secret-value"

    @pytest.mark.parametrize(
        "text",
        [
            "Not logged in · Please run /login",
            "Invalid API key · Please run /login",
            "OAuth token has expired",
        ],
    )
    def test_refusals_are_recognised(self, text):
        assert runner.looks_like_auth_failure({}, text)

    def test_a_normal_result_is_not_an_auth_failure(self):
        assert not runner.looks_like_auth_failure({}, "```json\n{}\n```")

    def test_per_message_error_field_is_recognised(self, tmp_path):
        stream = tmp_path / "stream.jsonl"
        stream.write_text(
            '{"type":"assistant","error":"authentication_failed",'
            '"message":{"content":[{"type":"text","text":"Not logged in"}]}}\n'
        )

        class FakeProcess:
            stdout = stream.read_text().splitlines(keepends=True)

        with (tmp_path / "session.jsonl").open("w") as log:
            _, _, auth_failed = runner._consume_stream(FakeProcess(), log)

        assert auth_failed

    def test_auth_failure_stops_the_batch_instead_of_retrying(
        self, work, config, fake_agent
    ):
        agent = fake_agent([(Step.CREATOR, self._refusing_agent)])

        outcome = pipeline.run_batch(config)

        assert "authentication failed" in outcome.auth_error
        # One run, not the two a retryable infra failure would have spent.
        assert agent.calls == [Step.CREATOR]

    def test_the_dataset_is_still_recorded_before_the_batch_aborts(
        self, work, config, fake_agent
    ):
        fake_agent([(Step.CREATOR, self._refusing_agent)])
        paths = DatasetPaths(work, ACC)

        pipeline.run_batch(config)

        rollup = pipeline.load_rollup(paths)
        assert rollup.state is State.FAILED_INFRA
        assert "authentication failed" in rollup.history[-1].detail

    def test_status_surfaces_why_a_dataset_failed(self, work, config, fake_agent):
        fake_agent([(Step.CREATOR, self._refusing_agent)])
        pipeline.run_batch(config)

        summary = pipeline.workflow_rollup(work)

        assert "authentication failed" in summary["datasets"][ACC]["last_detail"]
