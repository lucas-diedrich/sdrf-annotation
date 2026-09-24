"""Prompt rendering and the repair brief handed back to the creator."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from annotate.models import RunConfig, Step

PROMPT_DIR = Path(__file__).parent / "prompts"
_PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")


def prompt_path(step: Step, config: RunConfig) -> Path:
    return (config.prompts_dir or PROMPT_DIR) / f"{step}.md"


def render(template: Path, mapping: dict[str, str]) -> str:
    """Substitute `{{PLACEHOLDER}}` tokens in a prompt template.

    Args:
        template: Path to the markdown template.
        mapping: Placeholder name (without braces) to replacement text.

    Returns:
        The rendered prompt.

    Raises:
        ValueError: A placeholder was left unsubstituted. This is fatal rather
            than silent: an agent told to annotate `{{ACCESSION}}` would burn a
            whole run before anyone noticed.
    """
    text = template.read_text()
    for key, value in mapping.items():
        text = text.replace("{{" + key + "}}", value)
    if leftover := set(_PLACEHOLDER.findall(text)):
        raise ValueError(
            f"unsubstituted placeholders in {template.name}: {sorted(leftover)}"
        )
    return text


def repair_brief(review: dict[str, Any], attempt: int) -> str:
    """Render the reviewer's findings as a repair brief for the creator.

    Args:
        review: The reviewer's validated payload from the previous attempt.
        attempt: The attempt number this repair run will be.

    Returns:
        A markdown block, or "" when there is nothing to repair.
    """
    findings = review.get("findings") or []
    if not findings:
        return ""
    lines = [
        "",
        f"## Repair brief (attempt {attempt})",
        "",
        "An independent reviewer rejected the previous SDRF. Its findings are",
        "below. Fix every `error`; judge the rest on the evidence. The reviewer",
        "did not see your earlier reasoning and will not see this one either, so",
        "a finding you disagree with must be answered in `assumptions` with the",
        "evidence that refutes it -- not silently ignored.",
        "",
    ]
    for index, finding in enumerate(findings, start=1):
        location = finding.get("file", "")
        if finding.get("row") is not None:
            location += f" row {finding['row']}"
        if finding.get("column"):
            location += f" · {finding['column']}"
        lines += [
            f"{index}. **{finding.get('severity', 'error')}** — {location}",
            f"   - claim: {finding.get('claim', '')}",
            f"   - evidence: {finding.get('evidence', '')}",
            f"   - recommendation: {finding.get('recommendation', '')}",
        ]
    contradictions = (review.get("literature_agreement") or {}).get("contradictions")
    if contradictions:
        lines += ["", "Literature contradictions the reviewer recorded:", ""]
        lines += [f"- {item}" for item in contradictions]
    lines.append("")
    return "\n".join(lines)


def validation_brief(validation: dict[str, Any], attempt: int) -> str:
    """Render a host validation rejection as a repair brief for the creator.

    Args:
        validation: The host's validation record, as `logs/validation.json`
            holds it.
        attempt: The attempt number this repair run will be.

    Returns:
        A markdown block, or "" when the record holds no failure.
    """
    failed = validation.get("failed") or []
    if not failed:
        return ""
    live = bool(validation.get("live"))
    source = (
        "against live OLS (without `--use_ols_cache_only`)"
        if live
        else "with `--use_ols_cache_only`"
    )
    lines = [
        "",
        f"## Repair brief (attempt {attempt})",
        "",
        f"The host ran `parse_sdrf validate-sdrf` {source} over the previous",
        "SDRF and it failed. No reviewer judged that version: a file that does",
        "not validate is sent back before review. Fix every error below, then",
        "re-run the validator on each file yourself, with exactly the",
        "`--template` values listed for it, until it passes.",
        "",
    ]
    if live:
        lines += [
            "Your final validation must also run without `--use_ols_cache_only`:",
            "that is the check this file failed.",
            "",
        ]
    for index, check in enumerate(failed, start=1):
        templates = " ".join(f"-t {name}" for name in check.get("templates") or [])
        lines.append(f"{index}. `{check.get('path', '')}` ({templates})")
        messages = check.get("messages") or [check.get("detail") or "failed"]
        lines += [f"   - {message}" for message in messages]
    lines.append("")
    return "\n".join(lines)


def build(
    step: Step,
    accession: str,
    title: str,
    config: RunConfig,
    review_for_repair: dict[str, Any] | None = None,
    attempt: int = 1,
    validation_for_repair: dict[str, Any] | None = None,
) -> str:
    """Render the prompt one agent run will receive.

    Args:
        step: Which agent is about to run.
        accession: The dataset accession.
        title: Seed title, or "" when the accession is not in the seed.
        config: Run configuration, for a prompts-dir override.
        review_for_repair: Previous reviewer payload on a repair run.
        attempt: Attempt number, shown in the repair brief.
        validation_for_repair: The host validation record that rejected the
            previous SDRF, on a repair run caused by it.

    Returns:
        The fully rendered prompt.
    """
    if validation_for_repair:
        repair = validation_brief(validation_for_repair, attempt)
    elif review_for_repair:
        repair = repair_brief(review_for_repair, attempt)
    else:
        repair = ""
    return render(
        prompt_path(step, config),
        {
            "ACCESSION": accession,
            "TITLE": title or "(no title in seed)",
            "REPAIR_SECTION": repair,
        },
    )
