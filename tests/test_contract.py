"""Contract layer: JSON extraction, schema validation, and hash binding."""

from __future__ import annotations

import re

import jsonschema
import pytest

from annotate import contracts, prompts, utils
from annotate.models import SCHEMA_VERSION, RunConfig, Step


def creator_payload(**overrides):
    payload = {
        "schema_version": "1.0.0",
        "role": "creator",
        "accession": "PXD000001",
        "outcome": "completed",
        "blocked_reason": None,
        "assumptions": [],
        "unresolved": [],
        "sdrf_files": ["sdrf/PXD000001.sdrf.tsv"],
    }
    payload.update(overrides)
    return payload


def reviewer_payload(**overrides):
    payload = {
        "schema_version": "1.0.0",
        "role": "reviewer",
        "accession": "PXD000001",
        "artifacts": [{"path": "sdrf/PXD000001.sdrf.tsv", "sha256": "a" * 64}],
        "verdict": "pass",
        "deterministic": [{"check": "parse_sdrf", "passed": True}],
        "findings": [],
        "literature_agreement": {"reviewed": [], "contradictions": []},
    }
    payload.update(overrides)
    return payload


class TestExtractLastJsonObject:
    def test_last_block_wins(self):
        text = (
            'Here is an example:\n```json\n{"role": "example"}\n```\n'
            'And the real one:\n```json\n{"role": "creator"}\n```\n'
        )

        payload, error = utils.extract_last_json_object(text)

        assert error is None
        assert payload == {"role": "creator"}

    def test_unfenced_prose_after_the_block_is_harmless(self):
        text = '```json\n{"role": "creator"}\n```\nThanks!\n'

        payload, error = utils.extract_last_json_object(text)

        assert error is None
        assert payload == {"role": "creator"}

    def test_skips_a_trailing_unparseable_block(self):
        text = '```json\n{"role": "creator"}\n```\n```json\n{not json at all}\n```\n'

        payload, error = utils.extract_last_json_object(text)

        assert error is None
        assert payload == {"role": "creator"}

    def test_a_preceding_code_block_does_not_break_pairing(self):
        """A regex over ``` pairs mispairs here; the line scanner must not."""
        text = (
            "```bash\nparse_sdrf validate-sdrf --sdrf_file x\n```\n"
            "some prose between the blocks\n"
            '```json\n{"role": "creator"}\n```\n'
        )

        payload, error = utils.extract_last_json_object(text)

        assert error is None
        assert payload == {"role": "creator"}

    @pytest.mark.parametrize(
        "text,fragment",
        [
            ("", "no output"),
            ("plain prose, no fence", "no fenced JSON block"),
            ("```json\n[1, 2, 3]\n```", "JSON object"),
        ],
    )
    def test_reports_why_it_failed(self, text, fragment):
        payload, error = utils.extract_last_json_object(text)

        assert payload is None
        assert fragment in error


class TestValidateContract:
    def test_valid_creator_payload(self):
        assert contracts.validate_contract(creator_payload(), Step.CREATOR) is None

    def test_valid_reviewer_payload(self):
        assert contracts.validate_contract(reviewer_payload(), Step.REVIEWER) is None

    def test_completed_requires_an_sdrf_file(self):
        error = contracts.validate_contract(creator_payload(sdrf_files=[]), Step.CREATOR)

        assert error is not None and "sdrf_files" in error

    def test_blocked_requires_a_reason(self):
        error = contracts.validate_contract(
            creator_payload(outcome="blocked", sdrf_files=[], blocked_reason=None),
            Step.CREATOR,
        )

        assert error is not None and "blocked_reason" in error

    def test_blocked_with_a_reason_is_valid(self):
        payload = creator_payload(
            outcome="blocked",
            sdrf_files=[],
            blocked_reason="cell-lines template forbids a legal value for tissue rows",
        )

        assert contracts.validate_contract(payload, Step.CREATOR) is None

    def test_fail_verdict_requires_a_finding(self):
        error = contracts.validate_contract(
            reviewer_payload(verdict="fail", findings=[]), Step.REVIEWER
        )

        assert error is not None and "findings" in error

    def test_artifact_path_must_stay_under_sdrf(self):
        payload = creator_payload(sdrf_files=["../escape.sdrf.tsv"])

        assert contracts.validate_contract(payload, Step.CREATOR) is not None

    def test_creator_does_not_declare_hashes(self):
        """The host hashes the disk; a producer hashing its own output proves nothing."""
        payload = creator_payload(
            sdrf_files=[{"path": "sdrf/PXD000001.sdrf.tsv", "sha256": "a" * 64}]
        )

        assert contracts.validate_contract(payload, Step.CREATOR) is not None

    def test_role_confusion_is_rejected(self):
        assert contracts.validate_contract(creator_payload(), Step.REVIEWER) is not None

    def test_unknown_field_is_rejected(self):
        assert (
            contracts.validate_contract(creator_payload(verdict="pass"), Step.CREATOR)
            is not None
        )

    def test_finding_needs_evidence_and_recommendation(self):
        payload = reviewer_payload(
            verdict="fail",
            findings=[
                {
                    "severity": "error",
                    "file": "sdrf/PXD000001.sdrf.tsv",
                    "claim": "wrong disease",
                }
            ],
        )

        assert contracts.validate_contract(payload, Step.REVIEWER) is not None


class TestHashBinding:
    def test_matching_hashes_pass(self, tmp_path, sha256_of):
        (tmp_path / "PXD000001.sdrf.tsv").write_text("source name\tassay name\n")
        declared = [
            {
                "path": "sdrf/PXD000001.sdrf.tsv",
                "sha256": sha256_of("source name\tassay name\n"),
            }
        ]

        on_disk = contracts.hash_artifacts(tmp_path)

        assert contracts.compare_artifacts(declared, on_disk) is None

    def test_stale_hash_is_caught(self, tmp_path, sha256_of):
        artifact = tmp_path / "PXD000001.sdrf.tsv"
        artifact.write_text("reviewed content\n")
        declared = [
            {
                "path": "sdrf/PXD000001.sdrf.tsv",
                "sha256": sha256_of("reviewed content\n"),
            }
        ]
        artifact.write_text("content edited after the review\n")

        error = contracts.compare_artifacts(declared, contracts.hash_artifacts(tmp_path))

        assert error is not None and "sha256 mismatch" in error

    def test_declared_file_absent_from_disk(self, tmp_path):
        declared = [{"path": "sdrf/PXD000001.sdrf.tsv", "sha256": "a" * 64}]

        error = contracts.compare_artifacts(declared, contracts.hash_artifacts(tmp_path))

        assert error is not None and "not on disk" in error

    def test_unreviewed_file_on_disk_is_caught(self, tmp_path, sha256_of):
        (tmp_path / "PXD000001.sdrf.tsv").write_text("one\n")
        (tmp_path / "PXD000001-cell-lines.sdrf.tsv").write_text("two\n")
        declared = [{"path": "sdrf/PXD000001.sdrf.tsv", "sha256": sha256_of("one\n")}]

        error = contracts.compare_artifacts(declared, contracts.hash_artifacts(tmp_path))

        assert error is not None and "not declared" in error

    def test_creator_path_check_needs_no_hashes(self, tmp_path):
        (tmp_path / "PXD000001.sdrf.tsv").write_text("one\n")

        assert (
            contracts.compare_paths(
                ["sdrf/PXD000001.sdrf.tsv"], contracts.hash_artifacts(tmp_path)
            )
            is None
        )

    def test_creator_path_check_catches_an_undeclared_file(self, tmp_path):
        (tmp_path / "PXD000001.sdrf.tsv").write_text("one\n")
        (tmp_path / "stray.sdrf.tsv").write_text("two\n")

        error = contracts.compare_paths(
            ["sdrf/PXD000001.sdrf.tsv"], contracts.hash_artifacts(tmp_path)
        )

        assert error is not None and "not declared" in error

    def test_blocked_creator_may_declare_nothing(self):
        payload = creator_payload(outcome="blocked", sdrf_files=[], blocked_reason="gap")

        error, notes = contracts.check_agent_artifacts(Step.CREATOR, payload, {})

        assert error is None
        assert notes == []

    def test_hash_artifacts_ignores_non_sdrf_files(self, tmp_path, sha256_of):
        (tmp_path / "PXD000001.sdrf.tsv").write_text("one\n")
        (tmp_path / "notes.txt").write_text("scratch\n")

        assert contracts.hash_artifacts(tmp_path) == {
            "sdrf/PXD000001.sdrf.tsv": sha256_of("one\n")
        }


class TestSchemasAndPrompts:
    @pytest.mark.parametrize("role", list(Step))
    def test_schema_loads_and_compiles(self, role):
        schema = contracts.load_schema(role)

        jsonschema.Draft202012Validator.check_schema(schema)
        assert schema["$schema"].startswith("https://json-schema.org/draft/2020-12")

    @pytest.mark.parametrize("role", list(Step))
    def test_prompt_documents_the_schema_version(self, role):
        prompt = (prompts.PROMPT_DIR / f"{role}.md").read_text()

        assert f'"schema_version": "{SCHEMA_VERSION}"' in prompt
        assert f'"role": "{role}"' in prompt

    @pytest.mark.parametrize("role", list(Step))
    def test_prompt_example_block_validates(self, role):
        """The example in each prompt is the spec the agent copies, so it must pass."""
        rendered = prompts.build(role, "PXD000001", "t", RunConfig())
        payload, error = utils.extract_last_json_object(rendered)

        assert error is None, error
        if role is Step.REVIEWER:
            payload["artifacts"] = [
                dict(entry, sha256="a" * 64) for entry in payload["artifacts"]
            ]
        assert contracts.validate_contract(payload, role) is None

    @pytest.mark.parametrize("role", list(Step))
    def test_contract_example_is_the_last_json_block(self, role):
        """The host parses the last fenced block, so no example may follow it."""
        payload, error = utils.extract_last_json_object(
            prompts.build(role, "PXD000001", "t", RunConfig())
        )

        assert error is None
        assert payload["role"] == role

    @pytest.mark.parametrize("role", list(Step))
    def test_rendering_leaves_no_placeholder(self, role):
        assert "{{" not in prompts.build(role, "PXD000001", "t", RunConfig())

    def test_an_unsubstituted_placeholder_is_fatal(self, tmp_path):
        """A prompt telling an agent to annotate `{{ACCESSION}}` must not ship."""
        template = tmp_path / "creator.md"
        template.write_text("Annotate {{ACCESSION}} using {{MISSING_KEY}}.\n")

        with pytest.raises(ValueError, match="MISSING_KEY"):
            prompts.render(template, {"ACCESSION": "PXD000001"})

    def test_prompts_dir_can_be_overridden(self, tmp_path):
        override = tmp_path / "prompts"
        override.mkdir()
        (override / "creator.md").write_text("custom prompt for {{ACCESSION}}\n")

        rendered = prompts.build(
            Step.CREATOR, "PXD000001", "t", RunConfig(prompts_dir=override)
        )

        assert rendered.strip() == "custom prompt for PXD000001"


class TestRepairBrief:
    FINDING = {
        "severity": "error",
        "file": "sdrf/PXD000001.sdrf.tsv",
        "row": 12,
        "column": "characteristics[disease]",
        "claim": "annotated as melanoma",
        "evidence": "PMID 35695565 states lung adenocarcinoma",
        "recommendation": "MONDO:0005061",
    }

    def test_renders_each_finding(self):
        brief = prompts.repair_brief({"findings": [self.FINDING]}, attempt=2)

        assert "Repair brief (attempt 2)" in brief
        assert "row 12" in brief
        assert "characteristics[disease]" in brief
        assert "MONDO:0005061" in brief

    def test_no_findings_yields_no_brief(self):
        assert prompts.repair_brief({"findings": []}, attempt=2) == ""

    def test_contradictions_are_carried_over(self):
        brief = prompts.repair_brief(
            {
                "findings": [self.FINDING],
                "literature_agreement": {"contradictions": ["organism disagrees"]},
            },
            attempt=2,
        )

        assert "organism disagrees" in brief


class TestCreatorDeclarationIsNotTheSourceOfTruth:
    """Regression: PXD038699 produced a correct SDRF and was failed anyway.

    The creator listed everything it wrote -- the SDRF plus a dozen evidence
    files under files/ -- and the host rejected the whole run. The host can see
    the deliverable on disk, so a bookkeeping slip must not discard it.
    """

    ON_DISK = {"sdrf/PXD038699.sdrf.tsv": "a" * 64}

    def test_extra_evidence_paths_do_not_fail_the_run(self):
        payload = creator_payload(
            accession="PXD038699",
            sdrf_files=["sdrf/PXD038699.sdrf.tsv"],
        )

        error, notes = contracts.check_agent_artifacts(
            Step.CREATOR,
            payload | {"sdrf_files": ["sdrf/PXD038699.sdrf.tsv"]},
            self.ON_DISK,
        )

        assert error is None
        assert notes == []

    def test_a_mismatched_declaration_is_noted_not_fatal(self):
        payload = creator_payload(sdrf_files=["sdrf/wrong-name.sdrf.tsv"])

        error, notes = contracts.check_agent_artifacts(
            Step.CREATOR, payload, self.ON_DISK
        )

        assert error is None
        assert any("not on disk" in note for note in notes)
        assert any("not declared" in note for note in notes)

    def test_completed_with_no_sdrf_on_disk_is_still_fatal(self):
        """The one creator claim the host cannot verify away."""
        payload = creator_payload(sdrf_files=["sdrf/PXD000001.sdrf.tsv"])

        error, _ = contracts.check_agent_artifacts(Step.CREATOR, payload, {})

        assert error is not None and "no SDRF" in error

    def test_reviewer_hash_binding_stays_strict(self):
        """Leniency is creator-only; the reviewer's hashes are the proof."""
        payload = reviewer_payload(
            artifacts=[{"path": "sdrf/PXD038699.sdrf.tsv", "sha256": "b" * 64}]
        )

        error, _ = contracts.check_agent_artifacts(Step.REVIEWER, payload, self.ON_DISK)

        assert error is not None and "sha256 mismatch" in error


class TestAnnotationToolValue:
    """`manual curation` is what the skill suggests and is false for an agent."""

    PATTERN = re.compile(
        r"^(NT=[\w-]+;VV=v[\d.]+[\w.-]*|[\w-]+ v[\d.]+[\w.-]*|manual curation)$"
    )

    def test_the_prompt_corrects_the_skill_default(self):
        prompt = (prompts.PROMPT_DIR / "creator.md").read_text()

        assert "manual curation" in prompt  # named as the thing not to do
        assert "NT=sdrf-skills;VV=" in prompt

    def test_the_recommended_value_matches_the_spec_pattern(self):
        """There is no ontology for this column, only this pattern."""
        assert self.PATTERN.match("NT=sdrf-skills;VV=v0.2.0")
        assert not self.PATTERN.match("LLM annotation")
        assert not self.PATTERN.match("agentic")
