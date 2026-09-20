"""Filesystem, JSON and text helpers with no pipeline knowledge."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any] | None:
    """Read a JSON object, returning None for a missing or corrupt file.

    Corruption is not an error here: every JSON file this pipeline reads back is
    either regenerable or optional, and a batch must not die on one bad file.
    """
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def write_json(path: Path, payload: Any) -> None:
    """Write JSON atomically, so a crash mid-write cannot corrupt a status."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")
    tmp.replace(path)


def append_jsonl(path: Path, payload: Any) -> None:
    """Append one JSON record to a line-delimited log.

    Opened in append mode on every call rather than held open: the log has to
    survive a crash mid-batch, and several datasets write their own log
    concurrently.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(payload, sort_keys=False) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a line-delimited JSON log, skipping any line that does not parse.

    A truncated final line is expected after a kill and must not lose the
    records before it.
    """
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return records
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def tail_text(path: Path, limit: int) -> str:
    """Return at most the last `limit` characters of a text file."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return ""
    return text[-limit:].strip()


def dir_size_bytes(path: Path) -> int:
    total = 0
    for root, _, names in os.walk(path):
        for name in names:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def empty_dir(path: Path) -> int:
    """Delete everything inside `path`, keeping the directory itself.

    Returns:
        Bytes reclaimed.
    """
    if not path.is_dir():
        return 0
    reclaimed = dir_size_bytes(path)
    for entry in path.iterdir():
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)
    return reclaimed


def fenced_blocks(text: str) -> list[tuple[str, str]]:
    """Split `text` into its fenced code blocks as (language, body) pairs.

    Scanned line by line rather than by regex: a regex over ```...``` pairs
    mispairs as soon as a non-JSON block precedes the JSON one, because the
    closing fence of the earlier block reads as an opening fence and the prose
    after it is captured as a block.
    """
    blocks: list[tuple[str, str]] = []
    language: str | None = None
    body: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if language is None:
            if stripped.startswith("```"):
                language = stripped[3:].strip().lower()
                body = []
        elif stripped.startswith("```"):
            blocks.append((language, "\n".join(body)))
            language = None
        else:
            body.append(line)
    return blocks


def extract_last_json_object(text: str) -> tuple[dict[str, Any] | None, str | None]:
    """Parse the last fenced JSON object in `text`.

    The last block wins so that incidental prose or an illustrative snippet
    earlier in the transcript cannot break parsing.

    Args:
        text: Agent output to scan.

    Returns:
        (payload, error). Exactly one is non-None.
    """
    if not text:
        return None, "agent produced no output text"
    blocks = [body for language, body in fenced_blocks(text) if language in ("json", "")]
    for block in reversed(blocks):
        try:
            payload = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload, None
    if not blocks:
        return None, "no fenced JSON block in agent output"
    return None, "no fenced JSON block parsed as a JSON object"


def load_env_file(path: Path) -> dict[str, str]:
    """Parse a `.env` file into a mapping.

    Deliberately minimal -- `KEY=value`, `#` comments, optional `export` prefix
    and optional surrounding quotes. No interpolation, no multi-line values: a
    credential file that needs more than this should be exported by the shell.

    Args:
        path: The file to read. A missing file yields an empty mapping.

    Returns:
        {name: value} for every assignment found.
    """
    values: dict[str, str] = {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.removeprefix("export ").partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name.strip()] = value
    return values
