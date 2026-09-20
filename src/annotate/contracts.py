"""The agent/host boundary: schema validation and artifact hash binding.

The JSON block an agent prints is the only channel from the container to the
host, so it is treated as untrusted input: schema-checked first, then
cross-examined against what is actually on disk.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from functools import cache
from pathlib import Path
from typing import Any

import jsonschema

from annotate.models import Step
from annotate.utils import sha256_file

SCHEMA_DIR = Path(__file__).parent / "schemas"


@cache
def load_schema(role: Step) -> dict[str, Any]:
    return json.loads((SCHEMA_DIR / f"{role}.schema.json").read_text())


def normalize(step: Step, payload: dict[str, Any]) -> dict[str, Any]:
    """Map an older creator payload onto the current contract.

    `sdrf_files` was called `artifacts`, a name agents reasonably read as
    "everything I produced" -- they listed evidence and scripts under `files/`
    alongside the SDRF, and the run was rejected for it. Runs recorded under
    the old name are mapped rather than failed, so a naming fix does not strand
    a completed annotation, and an agent that still uses it is tolerated. Only
    SDRF paths survive; the evidence paths the old name invited are dropped.

    Args:
        step: Which agent produced `payload`.
        payload: The parsed agent JSON.

    Returns:
        The payload in the current shape. Unchanged when already current.
    """
    if step is not Step.CREATOR or "artifacts" not in payload:
        return payload
    migrated = dict(payload)
    legacy = migrated.pop("artifacts") or []
    migrated.setdefault(
        "sdrf_files",
        [p for p in legacy if isinstance(p, str) and p.startswith("sdrf/")],
    )
    return migrated


def validate_contract(payload: dict[str, Any], role: Step) -> str | None:
    """Validate an agent payload against its role schema.

    Args:
        payload: Parsed agent JSON.
        role: Which agent produced it.

    Returns:
        None when valid, otherwise a single-line description of the first error.
    """
    validator = jsonschema.Draft202012Validator(load_schema(role))
    errors = sorted(validator.iter_errors(payload), key=lambda e: list(e.absolute_path))
    if not errors:
        return None
    first = errors[0]
    location = "/".join(str(part) for part in first.absolute_path) or "<root>"
    return f"{location}: {first.message}"


def hash_artifacts(sdrf_dir: Path) -> dict[str, str]:
    """Hash every SDRF on disk.

    Args:
        sdrf_dir: The dataset's `sdrf/` directory.

    Returns:
        {workspace-relative path: sha256} over `sdrf/*.sdrf.tsv`.
    """
    if not sdrf_dir.is_dir():
        return {}
    return {
        f"sdrf/{path.name}": sha256_file(path)
        for path in sorted(sdrf_dir.glob("*.sdrf.tsv"))
        if path.is_file()
    }


def compare_paths(declared: Iterable[str], on_disk: dict[str, str]) -> str | None:
    """Assert that the declared artifact paths are exactly the files on disk.

    This is all that is asked of the creator: a producer hashing its own output
    proves nothing, so the host hashes the disk itself.

    Args:
        declared: The creator's `artifacts` list of workspace-relative paths.
        on_disk: Output of `hash_artifacts`.

    Returns:
        None when the sets agree, otherwise the first discrepancy.
    """
    declared_set = set(declared)
    missing = sorted(declared_set - set(on_disk))
    if missing:
        return f"declared artifact(s) not on disk: {', '.join(missing)}"
    undeclared = sorted(set(on_disk) - declared_set)
    if undeclared:
        return f"artifact(s) on disk but not declared: {', '.join(undeclared)}"
    return None


def compare_artifacts(
    declared: Sequence[dict[str, str]], on_disk: dict[str, str]
) -> str | None:
    """Assert that the reviewer judged exactly the bytes that are on disk.

    This hash binding replaces `review_gate.py`, which cannot work here: it
    discovers changed artifacts from git against a merge base, and a bare
    mounted `sdrf/` is not a repository. A mismatch means the verdict describes
    content other than the artifact, so the verdict is discarded.

    Args:
        declared: The reviewer's `artifacts` list of {path, sha256}.
        on_disk: Output of `hash_artifacts`.

    Returns:
        None when every path and hash agrees, otherwise the first discrepancy.
    """
    declared_map = {entry["path"]: entry["sha256"] for entry in declared}
    if path_error := compare_paths(declared_map, on_disk):
        return path_error
    for path, digest in declared_map.items():
        if digest != on_disk[path]:
            return (
                f"sha256 mismatch for {path}: "
                f"reviewer {digest[:12]}..., disk {on_disk[path][:12]}..."
            )
    return None


def check_agent_artifacts(
    step: Step, payload: dict[str, Any], on_disk: dict[str, str]
) -> tuple[str | None, list[str]]:
    """Cross-examine an agent's artifact claims against the disk.

    The two roles get different treatment because the declaration means
    different things. For the reviewer it is load-bearing -- the hashes are the
    only proof of what was judged, so a mismatch voids the verdict. For the
    creator it is only a cross-check: the host can see the deliverable itself,
    so a bookkeeping slip must not throw away a good SDRF and a long run.

    Args:
        step: Which agent produced `payload`.
        payload: The schema-valid agent output.
        on_disk: Output of `hash_artifacts`.

    Returns:
        (error, notes). `error` is fatal; `notes` are recorded discrepancies
        that do not invalidate the run.
    """
    if step is Step.REVIEWER:
        return compare_artifacts(payload.get("artifacts", []), on_disk), []

    declared = payload.get("sdrf_files", [])
    if payload.get("outcome") == "completed" and not on_disk:
        return "outcome is 'completed' but no SDRF was written to sdrf/", []
    return None, _declaration_notes(declared, on_disk)


def _declaration_notes(declared: Iterable[str], on_disk: dict[str, str]) -> list[str]:
    """Describe any disagreement between what was declared and what is on disk."""
    notes: list[str] = []
    declared_set = set(declared)
    if missing := sorted(declared_set - set(on_disk)):
        notes.append(f"declared but not on disk: {', '.join(missing)}")
    if undeclared := sorted(set(on_disk) - declared_set):
        notes.append(f"on disk but not declared: {', '.join(undeclared)}")
    return notes
