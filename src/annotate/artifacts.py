"""Host-side inspection of the SDRF files an agent produced.

Everything here is computed by the host from the bytes on disk, so it holds
for a run whose reviewer never started and for an agent that misreported what
it did. Without it `created` records only that a file exists, not that it
validates.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

TEMPLATE_COLUMN = "comment[sdrf template]"
BASE_TEMPLATE = "ms-proteomics"

# `NT=dia-acquisition;VV=v1.1.0` -- only the name selects a parse_sdrf template.
_TEMPLATE_NAME = re.compile(r"NT=([^;]+)")

Validator = Callable[[Path, list[str]], dict[str, Any]]


def read_rows(path: Path) -> tuple[list[str], list[list[str]]]:
    """Read an SDRF as (header, rows).

    Rows stay as lists, not dicts: an SDRF repeats column names by design --
    several `comment[modification parameters]`, one per modification -- and
    keying by name silently drops all but the last of each. On a real file
    that lost 4 of 52 columns.

    Args:
        path: The `.sdrf.tsv` file.

    Returns:
        The header and the data rows, both in file order. An unreadable or
        empty file yields `([], [])` rather than raising: this is bookkeeping,
        and must never be the thing that fails a run.
    """
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.reader(handle, delimiter="\t"))
    except (OSError, csv.Error):
        return [], []
    return (rows[0], rows[1:]) if rows else ([], [])


def template_of(value: str) -> str:
    match = _TEMPLATE_NAME.search(value or "")
    return match.group(1).strip() if match else (value or "").strip()


def template_columns(header: list[str]) -> list[int]:
    """Every `comment[sdrf template]` column, in file order.

    There is normally more than one: the column is repeated once per template
    the file claims to satisfy, and all of them apply to every row.
    """
    return [index for index, name in enumerate(header) if name == TEMPLATE_COLUMN]


def cell(row: list[str], index: int) -> str:
    return row[index] if index < len(row) else ""


def row_templates(row: list[str], indices: list[int]) -> tuple[str, ...]:
    """The template set one row declares, deduplicated and in file order."""
    names: dict[str, None] = {}
    for index in indices:
        if name := template_of(cell(row, index)):
            names.setdefault(name, None)
    return tuple(names)


def with_base(names: tuple[str, ...]) -> list[str]:
    """Add the template every SDRF specialises, when it is not already named."""
    return list(names) if BASE_TEMPLATE in names else [BASE_TEMPLATE, *names]


def declared_templates(header: list[str], rows: list[list[str]]) -> list[str]:
    """Every template the file declares, in first-seen order.

    Taken from the file rather than from anything the agent asserted, since
    the file is what a downstream consumer reads.
    """
    indices = template_columns(header)
    seen: dict[str, None] = {}
    for row in rows:
        for name in row_templates(row, indices):
            seen.setdefault(name, None)
    return list(seen)


def template_groups(
    header: list[str], rows: list[list[str]]
) -> dict[tuple[str, ...], list[list[str]]]:
    """Group rows by the set of templates they declare.

    One group is the normal case, and it is validated whole: every row is
    subject to every template it names, which is exactly what `parse_sdrf`'s
    union of `--template` values enforces. More than one group means the file
    genuinely mixes row kinds, and then the union is wrong -- it would impose
    each group's constraints on the other's rows -- so the groups are checked
    separately.
    """
    indices = template_columns(header)
    groups: dict[tuple[str, ...], list[list[str]]] = {}
    for row in rows:
        groups.setdefault(row_templates(row, indices), []).append(row)
    return groups


def write_subset(target: Path, header: list[str], rows: list[list[str]]) -> None:
    """Write the rows declaring one template to their own file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def sdrf_columns(sdrf_dir: Path) -> set[str]:
    """Every column name appearing in any SDRF under `sdrf_dir`."""
    columns: set[str] = set()
    for path in sorted(sdrf_dir.glob("*.sdrf.tsv")):
        header, _ = read_rows(path)
        columns.update(header)
    return columns


def summarize(
    sdrf_dir: Path,
    validate: Validator | None = None,
    scratch: Path | None = None,
) -> list[dict[str, Any]]:
    """Describe every SDRF on disk: its shape, its templates, whether it validates.

    Args:
        sdrf_dir: The dataset's `sdrf/` directory.
        validate: Called with (file, templates) to run the specification
            validator. Omitted when validation is disabled, in which case the
            shape is still recorded.
        scratch: Writable directory for the row subsets a file that mixes
            template sets has to be split into before it can be validated.
            Required only for such a file.

    Returns:
        One entry per file, in path order.
    """
    summary: list[dict[str, Any]] = []
    for path in sorted(sdrf_dir.glob("*.sdrf.tsv")):
        header, rows = read_rows(path)
        templates = declared_templates(header, rows)
        entry: dict[str, Any] = {
            "path": f"sdrf/{path.name}",
            "rows": len(rows),
            "columns": len(header),
            "templates": templates,
            "validation": [],
        }
        if validate is not None:
            entry["validation"] = _validate_file(path, header, rows, validate, scratch)
        summary.append(entry)
    return summary


def _validate_file(
    path: Path,
    header: list[str],
    rows: list[list[str]],
    validate: Validator,
    scratch: Path | None,
) -> list[dict[str, Any]]:
    """Validate a file once per group of rows sharing a template set."""
    groups = template_groups(header, rows) or {(): rows}
    results: list[dict[str, Any]] = []
    for index, (names, group_rows) in enumerate(groups.items()):
        templates = with_base(names)
        target = path
        if len(groups) > 1:
            if scratch is None:
                results.append(
                    {
                        "templates": templates,
                        "ran": False,
                        "passed": None,
                        "detail": "file mixes template sets and needs a scratch "
                        "directory to split rows before validating",
                    }
                )
                continue
            target = scratch / f"group{index}-{path.name}"
            write_subset(target, header, group_rows)
        results.append(validate(target, templates))
    return results


def validation_failures(
    summary: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split a summary's validation results into failures and non-verdicts.

    Args:
        summary: The output of `summarize`.

    Returns:
        (failed, not_run). Each entry is one validation result carrying the
        `path` of the file it belongs to. `failed` holds checks that ran and
        rejected the file; `not_run` holds checks that never produced a verdict,
        which say nothing about the file and must not be repaired against.
    """
    failed: list[dict[str, Any]] = []
    not_run: list[dict[str, Any]] = []
    for entry in summary:
        for result in entry.get("validation") or []:
            located = {"path": entry.get("path", ""), **result}
            if not result.get("ran"):
                not_run.append(located)
            elif not result.get("passed"):
                failed.append(located)
    return failed, not_run
