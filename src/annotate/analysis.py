"""Post-hoc analysis of a finished batch.

Reads only what is already on disk. Cost attribution reopens `session.jsonl`
because per-message usage is the one signal too large to lift into
`status.json`; everything else in here comes from the run statuses.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from annotate.models import DatasetPaths, RunStatus, Step
from annotate.utils import read_json

# Billing weights relative to one uncached input token. These are the ratios
# Anthropic's list prices hold across the model family, which is what lets a
# run's own `total_cost_usd` be split between phases without knowing the
# absolute price of the model that produced it.
TOKEN_WEIGHTS = {
    "input_tokens": 1.0,
    "cache_creation_input_tokens": 1.25,
    "cache_read_input_tokens": 0.1,
    "output_tokens": 5.0,
}

# Everything except output, which a message event reports before the message
# has finished: measured over a complete run it came to 0.5% of the figure the
# result event billed, so publishing it per phase would only mislead.
COUNTED_FIELDS = tuple(f for f in TOKEN_WEIGHTS if f != "output_tokens")

# Derived from the three creator traces rather than guessed. `other` was 45%
# of spend under a coarser scheme, which is not an attribution; `spec` and the
# supplementary-file handling inside `literature` are what that 45% turned out
# to be.
PHASES = (
    "spec",
    "literature",
    "pride",
    "ontology",
    "raw",
    "validation",
    "authoring",
    "waiting",
    "reasoning",
    "other",
)

# Tool names map to a phase directly. Bash does not: it is 80% of all calls
# here, so leaving it as one bucket would make the attribution useless.
_TOOL_PHASES = {
    "Skill": "spec",
    "WebFetch": "literature",
    "WebSearch": "literature",
    "Write": "authoring",
    "Edit": "authoring",
    "NotebookEdit": "authoring",
}

_MCP_PHASES = (
    ("searchclasses", "ontology"),
    ("embedding", "ontology"),
    ("ols", "ontology"),
    ("project", "pride"),
    ("pride", "pride"),
    ("article", "literature"),
    ("full_text", "literature"),
    ("pdf", "literature"),
    ("preprint", "literature"),
    ("pubmed", "literature"),
)

# First match wins, so the order is the classification. Validation is checked
# before the specification because a `parse_sdrf` call naming a template is
# validation, not reading the template.
_BASH_PHASES = (
    (re.compile(r"\bparse_sdrf\b|python -m tools\b|\btechsdrf\b"), "validation"),
    (
        re.compile(
            r"sdrf-skills|sdrf-templates|templates\.yaml|TERMS\.tsv"
            r"|resolve_templates|spec/sdrf-proteomics"
        ),
        "spec",
    ),
    (re.compile(r"ThermoRawFileParser|/raw/|\braw/|\.raw\b", re.I), "raw"),
    (re.compile(r"tasks/\S+\.output|^\s*sleep\b|until \[ -f", re.M), "waiting"),
    (
        re.compile(
            r"ebi\.ac\.uk/pride|proteomecentral|ProteomeXchange"
            r"|sdrf-annotated-datasets",
            re.I,
        ),
        "pride",
    ),
    (
        re.compile(
            r"ncbi\.nlm\.nih\.gov|europepmc|\bPMC\d|doi\.org|pdftotext|pypdf"
            r"|pdfminer|openpyxl|MOESM|\.xlsx|suppl|biostudies|github",
            re.I,
        ),
        "literature",
    ),
    (re.compile(r"ols4|ontology|\bobo\b", re.I), "ontology"),
    (re.compile(r"\.sdrf\.tsv|build_sdrf"), "authoring"),
)


def phase_of_tool(name: str, payload: dict[str, Any]) -> str:
    """Classify one tool call into a workflow phase.

    Args:
        name: The tool name from the stream event.
        payload: The tool's input, needed to read a Bash command line.

    Returns:
        One of `PHASES`.
    """
    if name in _TOOL_PHASES:
        return _TOOL_PHASES[name]
    if name.startswith("mcp__"):
        lowered = name.lower()
        for marker, phase in _MCP_PHASES:
            if marker in lowered:
                return phase
        return "other"
    if name == "Bash":
        command = str(payload.get("command", ""))
        for pattern, phase in _BASH_PHASES:
            if pattern.search(command):
                return phase
    return "other"


def message_phase(content: list[dict[str, Any]]) -> str:
    """The phase an assistant message belongs to.

    A message is attributed to its first tool call, which is the work it
    committed to. Its thinking counts towards the same phase: the model was
    deciding what to do next, and charging that to a separate "reasoning"
    bucket would say only that the model thinks, which is not a finding. A
    message that calls no tool at all is genuinely reasoning.
    """
    for block in content:
        if block.get("type") == "tool_use":
            return phase_of_tool(block.get("name", ""), block.get("input") or {})
    return "reasoning"


def weighted_tokens(usage: dict[str, Any]) -> float:
    return sum(usage.get(field, 0) * weight for field, weight in TOKEN_WEIGHTS.items())


def group_messages(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reassemble the stream's assistant events into whole messages.

    One message is emitted as several events -- thinking, text, then one per
    tool call -- each repeating the same `usage`. Counting events would both
    double-count the tokens and file a message's thinking separately from the
    tool call it led to. Interleaved `thinking_tokens` deltas are attributed
    to the message they precede, which is the only per-message record of
    output the stream carries.

    Args:
        events: Every parsed line of a session trace, in order.

    Returns:
        One entry per message, in order, with merged content, its single
        usage, and the thinking tokens spent producing it.
    """
    messages: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    pending_thinking = 0
    for event in events:
        if event.get("subtype") == "thinking_tokens":
            pending_thinking += event.get("estimated_tokens_delta", 0)
            continue
        if event.get("type") != "assistant":
            continue
        message = event.get("message") or {}
        message_id = message.get("id") or ""
        if message_id not in messages:
            messages[message_id] = {
                "usage": message.get("usage") or {},
                "content": [],
                "thinking_tokens": 0,
            }
            order.append(message_id)
            messages[message_id]["thinking_tokens"] = pending_thinking
            pending_thinking = 0
        messages[message_id]["content"] += message.get("content") or []
    return [messages[message_id] for message_id in order]


def phase_costs(session: Path) -> dict[str, Any]:
    """Attribute one run's tokens and cost to the phases that spent them.

    Args:
        session: Path to a `session.jsonl` trace.

    Returns:
        `phases` mapping each phase to its message count, token counts and
        apportioned cost, plus the run total and `output_coverage` -- the
        fraction of the run's output tokens the attribution could actually
        see. The rest is generated text, which the stream reports per message
        only as a partial count taken before the message finished.
    """
    events = _lines(session)
    result = next((e for e in events if e.get("type") == "result"), {})
    total_cost = result.get("total_cost_usd") or 0.0
    output_total = (result.get("usage") or {}).get("output_tokens", 0)

    per_phase: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "messages": 0,
            "weighted": 0.0,
            "thinking_tokens": 0,
            **dict.fromkeys(COUNTED_FIELDS, 0),
        }
    )
    for message in group_messages(events):
        usage = message["usage"]
        if not usage:
            continue
        bucket = per_phase[message_phase(message["content"])]
        bucket["messages"] += 1
        bucket["thinking_tokens"] += message["thinking_tokens"]
        for field in COUNTED_FIELDS:
            bucket[field] += usage.get(field, 0)
        # Thinking stands in for this message's output: the stream's own
        # output_tokens is a floor taken before the message finished, and
        # measured against a complete run it accounted for 0.5% of the real
        # figure against thinking's 48%.
        bucket["weighted"] += (
            weighted_tokens({**usage, "output_tokens": 0})
            + message["thinking_tokens"] * TOKEN_WEIGHTS["output_tokens"]
        )

    total_weight = sum(bucket["weighted"] for bucket in per_phase.values())
    attributed_output = sum(b["thinking_tokens"] for b in per_phase.values())
    for bucket in per_phase.values():
        share = bucket["weighted"] / total_weight if total_weight else 0.0
        bucket["cost_share"] = round(share, 4)
        bucket["cost_usd"] = round(total_cost * share, 4)
        bucket["weighted"] = round(bucket["weighted"])
    return {
        "phases": dict(sorted(per_phase.items(), key=lambda kv: -kv[1]["weighted"])),
        "total_cost_usd": round(total_cost, 4),
        "output_coverage": round(attributed_output / output_total, 3)
        if output_total
        else 0.0,
    }


def _lines(path: Path) -> list[dict[str, Any]]:
    try:
        raw = path.read_text().splitlines()
    except OSError:
        return []
    events = []
    for line in raw:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def sessions(work: Path) -> list[Path]:
    """Every agent trace under `work`, including archived repair attempts."""
    found: list[Path] = []
    for logs_dir in sorted(work.glob("*/logs")):
        paths = DatasetPaths(work, logs_dir.parent.name)
        for step in Step:
            step_dir = paths.step_dir(step)
            found += [
                path
                for path in (
                    step_dir / "session.jsonl",
                    *sorted(step_dir.glob("attempt-*/session.jsonl")),
                )
                if path.is_file()
            ]
    return found


def batch_phase_costs(work: Path) -> dict[str, Any]:
    """Sum per-phase attribution across every trace in the batch.

    Args:
        work: Workflow root.

    Returns:
        The same shape as `phase_costs`, over every run, plus the number of
        traces read.
    """
    totals: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "messages": 0,
            "weighted": 0,
            "thinking_tokens": 0,
            "cost_usd": 0.0,
            **dict.fromkeys(COUNTED_FIELDS, 0),
        }
    )
    total_cost = 0.0
    traces = sessions(work)
    for trace in traces:
        run = phase_costs(trace)
        total_cost += run["total_cost_usd"]
        for phase, bucket in run["phases"].items():
            for field, value in bucket.items():
                if field != "cost_share":
                    totals[phase][field] += value

    grand = sum(bucket["weighted"] for bucket in totals.values())
    for bucket in totals.values():
        bucket["cost_share"] = round(bucket["weighted"] / grand, 4) if grand else 0.0
        bucket["cost_usd"] = round(bucket["cost_usd"], 4)
    return {
        "traces": len(traces),
        "phases": dict(sorted(totals.items(), key=lambda kv: -kv[1]["weighted"])),
        "total_cost_usd": round(total_cost, 4),
    }


def batch_report(work: Path) -> dict[str, Any]:
    """Aggregate everything the batch recorded that is worth counting.

    Args:
        work: Workflow root.

    Returns:
        Failure kinds, spend, specification gaps and unresolved values,
        counted across every run status under `work`.
    """
    runs = list(_run_statuses(work))
    gaps: Counter[str] = Counter()
    unresolved: Counter[str] = Counter()
    failures: Counter[str] = Counter()
    per_step: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"runs": 0, "cost_usd": 0.0, "duration_s": 0.0, "uncosted": 0}
    )
    validation = {"passed": 0, "failed": 0, "not_run": 0}

    for run in runs:
        bucket = per_step[str(run.step)]
        bucket["runs"] += 1
        bucket["duration_s"] += run.duration_s
        cost = run.usage.get("cost_usd")
        if cost is None:
            bucket["uncosted"] += 1
        else:
            bucket["cost_usd"] += cost
        if run.failure_kind:
            failures[str(run.failure_kind)] += 1
        for entry in run.agent_output.get("spec_gaps") or []:
            gaps[entry.get("column") or "(no column)"] += 1
        for entry in run.agent_output.get("unresolved") or []:
            if isinstance(entry, dict):
                unresolved[entry.get("column") or "(no column)"] += 1
        for artifact in run.artifact_summary:
            for check in artifact.get("validation") or []:
                key = (
                    "not_run"
                    if not check.get("ran")
                    else "passed"
                    if check.get("passed")
                    else "failed"
                )
                validation[key] += 1

    for bucket in per_step.values():
        bucket["cost_usd"] = round(bucket["cost_usd"], 2)
        bucket["duration_s"] = round(bucket["duration_s"])
    return {
        "runs": len(runs),
        "by_step": dict(per_step),
        "failure_kinds": dict(failures.most_common()),
        "validation": validation,
        "spec_gaps": dict(gaps.most_common()),
        "unresolved": dict(unresolved.most_common()),
    }


def _run_statuses(work: Path):
    for logs_dir in sorted(work.glob("*/logs")):
        paths = DatasetPaths(work, logs_dir.parent.name)
        for step in Step:
            for status_file in (
                paths.status(step),
                *sorted(paths.step_dir(step).glob("attempt-*/status.json")),
            ):
                if data := read_json(status_file):
                    yield RunStatus.from_dict(data)
