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
) -> str | None:
    """Apply the artifact check appropriate to the agent that ran.

    Args:
        step: Which agent produced `payload`.
        payload: The schema-valid agent output.
        on_disk: Output of `hash_artifacts`.

    Returns:
        None when the declaration matches disk, otherwise the discrepancy.
    """
    declared = payload.get("artifacts", [])
    if step is Step.REVIEWER:
        return compare_artifacts(declared, on_disk)
    # A blocked creator legitimately produces nothing.
    if payload.get("outcome") == "completed" or declared:
        return compare_paths(declared, on_disk)
    return None
