"""Running one agent container: the docker command, the stream, the disk cap.

This is the only module that touches the outside world, which is what makes the
rest of the pipeline testable without docker.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from functools import cache
from itertools import chain
from pathlib import Path
from typing import Any

from annotate.models import DatasetPaths, RunConfig, RunResult, Step
from annotate.utils import dir_size_bytes, load_env_file, tail_text

API_KEY_VAR = "ANTHROPIC_API_KEY"

# How much of a failing run's stderr is worth keeping. Enough for a stack trace
# or a docker refusal, short enough to sit inside a status file.
STDERR_TAIL_CHARS = 2048

# The specification validator is bounded separately from the agent: it is a
# single offline `parse_sdrf` call that takes ~12 s, so anything near this
# means it is stuck rather than slow.
VALIDATION_TIMEOUT_S = 300

# Read from the image rather than asserted by the host: a batch spanning days
# cannot be partitioned by code version afterwards unless each run says which
# image and which skills produced it, and neither is backfillable.
_PROVENANCE_SCRIPT = """
git -C "$SDRF_SKILLS_HOME" rev-parse HEAD 2>/dev/null || echo unknown
sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\\([^"]*\\)".*/\\1/p' \
    "$SDRF_SKILLS_HOME/.claude-plugin/plugin.json" 2>/dev/null | head -1
"""

# `claude -p` reports an unusable credential as ordinary output rather than a
# distinct exit code, so it has to be recognised from the stream.
_AUTH_FAILURE_MARKERS = (
    "not logged in",
    "please run /login",
    "invalid api key",
    "authentication_error",
    "authentication failed",
    "oauth token has expired",
)


class MissingCredentials(RuntimeError):
    """No API key could be resolved for the agent containers."""


class AuthenticationError(RuntimeError):
    """The agent container rejected the credential it was given.

    Fatal for a whole batch, not just one dataset: every remaining run would
    fail the same way, in milliseconds, and be recorded as an infrastructure
    failure that tells the operator nothing.
    """


def resolve_api_key(config: RunConfig) -> str:
    """Find the API key for the agent containers.

    The environment wins over the env file, so an explicit export can
    override a stale checked-out `.env`.

    Args:
        config: Run configuration, carrying the optional env-file path.

    Returns:
        The key.

    Raises:
        MissingCredentials: Neither source supplied one.
    """
    if key := os.environ.get(API_KEY_VAR, "").strip():
        return key
    if config.env_file and (
        key := load_env_file(config.env_file).get(API_KEY_VAR, "").strip()
    ):
        return key
    searched = f" or {config.env_file}" if config.env_file else ""
    raise MissingCredentials(
        f"no {API_KEY_VAR} in the environment{searched}. "
        f"Export it, or set it in an env file (--env-file), before running."
    )


def agent_env(config: RunConfig) -> dict[str, str]:
    """The subprocess environment for a `docker run`.

    The key is injected here rather than baked into the argv as
    `-e KEY=value`: the argv is written to `command.txt` for reproducibility,
    and a credential must not land on disk. Docker's passthrough `-e KEY`
    form reads it from this environment instead.
    """
    return {**os.environ, API_KEY_VAR: resolve_api_key(config)}


def looks_like_auth_failure(result_event: dict[str, Any], final_text: str) -> bool:
    """Recognise a credential rejection in an agent's output."""
    if result_event.get("terminal_reason") == "authentication_error":
        return True
    haystack = f"{final_text} {result_event.get('result', '')}".lower()
    return any(marker in haystack for marker in _AUTH_FAILURE_MARKERS)


class RawBudgetWatchdog:
    """Kill a run whose `raw/` download crosses the per-dataset cap.

    The cap is enforced here rather than asked of the agent. Docker cannot do
    it: a bind mount sits outside the container's writable layer, so no
    `--storage-opt` quota applies to it -- and on Docker Desktop that flag is
    accepted and silently ignored anyway. A tmpfs would have to be RAM. Polling
    the mount from the host is the one mechanism that is both deterministic and
    portable.
    """

    POLL_S = 10.0

    def __init__(self, raw_dir: Path, budget_gb: float, process: subprocess.Popen):
        self.raw_dir = raw_dir
        self.budget_gb = budget_gb
        self.budget_bytes = int(budget_gb * 1024**3)
        self.process = process
        self.breach = ""
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._watch, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=self.POLL_S * 2)

    def _watch(self) -> None:
        while not self._stop.wait(self.POLL_S):
            used = dir_size_bytes(self.raw_dir)
            if used > self.budget_bytes:
                self.breach = (
                    f"raw download budget exceeded: {used / 1024**3:.1f} GB in raw/ "
                    f"against a cap of {self.budget_gb:g} GB; run killed"
                )
                self.process.kill()
                return


def build_docker_command(
    step: Step,
    paths: DatasetPaths,
    prompt: str,
    session_id: str,
    config: RunConfig,
) -> list[str]:
    """Assemble the `docker run` argv for one agent.

    The reviewer gets the three data mounts read-only, which is what stops it
    quietly becoming a producer. `review/` inverts that: the reviewer writes
    its report there and the creator may only read it, so neither agent can
    edit the other's output. `logs/` is mounted nowhere, so an agent has no
    path to the host's records of it.

    Args:
        step: Which agent to run.
        paths: Dataset paths.
        prompt: The rendered prompt, passed as the final argv element.
        session_id: UUID for `claude --session-id`.
        config: Run configuration.

    Returns:
        The full docker argv.
    """
    read_only = ":ro" if step is Step.REVIEWER else ""
    review_only = "" if step is Step.REVIEWER else ":ro"
    config_dir = paths.config_dir(step)
    config_dir.mkdir(parents=True, exist_ok=True)
    paths.review.mkdir(parents=True, exist_ok=True)
    scratch_bytes = int(config.scratch_gb * 1024**3)
    return [
        "docker", "run", "--rm",
        # A size-capped tmpfs for the agent's scratch space, so a runaway write
        # fails with ENOSPC instead of filling the host's disk.
        "--mount",
        f"type=tmpfs,destination=/workspace/scratchpad,tmpfs-size={scratch_bytes}",
        "-e", f"USER_UID={os.getuid()}",
        "-e", f"USER_GID={os.getgid()}",
        "-e", "CLAUDE_CONFIG_DIR=/.claude",
        "-e", "ANTHROPIC_API_KEY",
        "-v", f"{paths.sdrf.resolve()}:/workspace/sdrf{read_only}",
        "-v", f"{paths.files.resolve()}:/workspace/files{read_only}",
        "-v", f"{paths.raw.resolve()}:/workspace/raw{read_only}",
        "-v", f"{paths.review.resolve()}:/workspace/review{review_only}",
        "-v", f"{config_dir.resolve()}:/.claude",
        config.image,
        "claude", "-p",
        "--output-format", "stream-json", "--verbose",
        "--session-id", session_id,
        "--permission-mode", config.permission_mode,
        prompt,
    ]  # fmt: skip


_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def accumulate_usage(
    totals: dict[str, Any], message: dict[str, Any], seen: set[str]
) -> None:
    """Fold one assistant message's usage into a running total, by model.

    One message arrives as several stream events -- thinking, then text, then
    each tool call -- and every one of them repeats the same `usage` object.
    Summing events rather than messages therefore double-counts; measured
    against a complete run it reported 13.7M cache-read tokens for 7.4M
    actually billed.

    Args:
        totals: The accumulator, mutated in place.
        message: The `message` object of a stream-json assistant event.
        seen: Message ids already counted, mutated in place.
    """
    usage = message.get("usage") or {}
    message_id = message.get("id") or ""
    if not usage or message_id in seen:
        return
    seen.add(message_id)
    for field in _TOKEN_FIELDS:
        totals[field] = totals.get(field, 0) + usage.get(field, 0)
    totals["num_turns"] = totals.get("num_turns", 0) + 1
    # The usage on a message event is emitted as the message starts, so its
    # output count is whatever had been produced by then -- a floor, and a low
    # one. Input and cache figures are final and match the result event exactly.
    totals["output_tokens_partial"] = True
    per_model = totals.setdefault("model_usage", {})
    model = per_model.setdefault(message.get("model", "unknown"), {})
    for field in _TOKEN_FIELDS:
        model[field] = model.get(field, 0) + usage.get(field, 0)


def _consume_stream(
    process: subprocess.Popen, session_log: Any
) -> tuple[dict[str, Any], str, bool, dict[str, Any]]:
    """Tee the agent's stream-json stdout to disk and pull out its final text.

    Returns:
        (result_event, final_text, auth_failed, streamed_usage). The `result`
        event is the stream's last line; the assistant fallback covers a run
        that produced text but no result event, which would otherwise look like
        silence. `auth_failed` comes from the per-message `error` field, which
        names a credential rejection precisely where the result text only hints
        at it. `streamed_usage` is accumulated as the stream arrives so a run
        killed before its result event is still costed.
    """
    result_event: dict[str, Any] = {}
    final_text = ""
    auth_failed = False
    streamed_usage: dict[str, Any] = {}
    counted: set[str] = set()
    for line in process.stdout:
        session_log.write(line)
        session_log.flush()
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("error") == "authentication_failed":
            auth_failed = True
        if event.get("type") == "result":
            result_event = event
            final_text = event.get("result") or ""
        elif event.get("type") == "assistant":
            accumulate_usage(streamed_usage, event.get("message", {}), counted)
            if not final_text:
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "text":
                        final_text = block.get("text", "")
    return result_event, final_text, auth_failed, streamed_usage


def run_agent(
    step: Step, paths: DatasetPaths, prompt: str, config: RunConfig
) -> RunResult:
    """Run one agent container, streaming its output to `logs/{step}/session.jsonl`.

    Args:
        step: Which agent to run.
        paths: Dataset paths.
        prompt: The fully rendered prompt.
        config: Run configuration.

    Returns:
        A RunResult carrying the exit code, the stream's final `result` event,
        the agent's final text, and any disk-budget breach.
    """
    step_dir = paths.step_dir(step)
    step_dir.mkdir(parents=True, exist_ok=True)
    session_id = str(uuid.uuid4())
    command = build_docker_command(step, paths, prompt, session_id, config)
    (step_dir / "command.txt").write_text(" ".join(command[:-1]) + " <prompt>\n")

    if config.dry_run:
        return RunResult(exit_code=0, session_id=session_id)

    started = time.monotonic()
    with (
        (step_dir / "session.jsonl").open("w") as session_log,
        (step_dir / "stderr.log").open("w") as stderr_log,
    ):
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=stderr_log,
            text=True,
            bufsize=1,
            env=agent_env(config),
        )
        # The timeout kills the container rather than abandoning the pipe: a
        # surviving run would hold the config-dir mount that pruning removes.
        killer = threading.Timer(config.timeout_s, process.kill)
        killer.start()
        watchdog = RawBudgetWatchdog(paths.raw, config.raw_budget_gb, process)
        watchdog.start()
        try:
            result_event, final_text, auth_failed, streamed = _consume_stream(
                process, session_log
            )
            exit_code = process.wait()
        finally:
            timed_out = not killer.is_alive() and killer.finished.is_set()
            killer.cancel()
            watchdog.stop()

    # A killed process reports a negative code; call it a timeout rather than a
    # crash so the operator raises --timeout-s instead of hunting a container bug.
    if exit_code < 0 and not watchdog.breach:
        timed_out = True
    return RunResult(
        exit_code=exit_code,
        timed_out=timed_out,
        duration_s=time.monotonic() - started,
        session_id=session_id,
        result_event=result_event,
        final_text=final_text,
        over_budget=watchdog.breach,
        auth_failed=auth_failed or looks_like_auth_failure(result_event, final_text),
        streamed_usage=streamed,
        stderr_tail=tail_text(step_dir / "stderr.log", STDERR_TAIL_CHARS),
    )


@cache
def image_provenance(image: str) -> dict[str, str]:
    """Identify the image and the skills inside it.

    Cached per image: one container start per batch, and the answer cannot
    change while a batch is running.

    Args:
        image: The container image tag.

    Returns:
        {image, image_id, skills_commit, skills_version}. A field that could
        not be read is omitted rather than guessed.
    """
    provenance: dict[str, str] = {"image": image}
    try:
        ids = subprocess.run(
            ["docker", "images", "--no-trunc", "-q", image],
            capture_output=True,
            text=True,
            check=False,
        )
        probe = subprocess.run(
            ["docker", "run", "--rm", "--network", "none", "--entrypoint", "sh",
             image, "-c", _PROVENANCE_SCRIPT],
            capture_output=True,
            text=True,
            timeout=VALIDATION_TIMEOUT_S,
            check=False,
        )  # fmt: skip
    except (subprocess.TimeoutExpired, OSError):
        return provenance
    if image_id := ids.stdout.strip().splitlines():
        provenance["image_id"] = image_id[0]
    lines = probe.stdout.split()
    if lines and lines[0] != "unknown":
        provenance["skills_commit"] = lines[0]
    if len(lines) > 1:
        provenance["skills_version"] = lines[1]
    return provenance


def validate_command(
    sdrf_file: Path, templates: list[str], image: str, use_ols_cache_only: bool = True
) -> list[str]:
    """Assemble the `docker run` argv for one specification validation.

    Args:
        sdrf_file: The file to validate. Its parent is mounted read-only.
        templates: The `--template` values to validate against.
        image: The image carrying `parse_sdrf`.
        use_ols_cache_only: Validate offline against the baked ontology cache.
            False drops both the cache flag and the network isolation, so every
            term is resolved against live OLS.

    Returns:
        The full docker argv.
    """
    return [
        "docker", "run", "--rm",
        *(("--network", "none") if use_ols_cache_only else ()),
        "-v", f"{sdrf_file.resolve().parent}:/check:ro",
        image,
        "parse_sdrf", "validate-sdrf", "-s", f"/check/{sdrf_file.name}",
        *chain.from_iterable(("-t", name) for name in templates),
        *(("--use_ols_cache_only",) if use_ols_cache_only else ()),
    ]  # fmt: skip


def validate_sdrf(
    sdrf_file: Path,
    templates: list[str],
    config: RunConfig,
    use_ols_cache_only: bool = True,
) -> dict[str, Any]:
    """Run the specification validator over one SDRF in the image.

    The host has no `parse_sdrf` of its own, so the check runs in the same
    image the agent used -- which also means it is the same validator version.
    The default is `--network none` with the baked ontology cache, which keeps
    the pipeline's own bookkeeping deterministic and offline.

    Args:
        sdrf_file: The file to validate. Its parent is mounted read-only.
        templates: The `--template` values to validate against.
        config: Run configuration, for the image.
        use_ols_cache_only: Validate against the baked cache with the network
            off. False resolves every term against live OLS instead, which is
            what a contribution's "verified against live OLS4" claim needs and
            the cached run cannot support.

    Returns:
        {templates, ran, passed, errors, warnings, detail}. `ran` is False when
        the validator could not be started at all, which is not a verdict on
        the file.
    """
    result: dict[str, Any] = {"templates": templates, "ran": False, "passed": None}
    command = validate_command(sdrf_file, templates, config.image, use_ols_cache_only)
    try:
        probe = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=VALIDATION_TIMEOUT_S,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as error:
        result["detail"] = f"validator could not be run: {error}"
        return result

    lines = (probe.stdout + probe.stderr).splitlines()
    errors = [line for line in lines if line.startswith("ERROR")]
    result.update(
        ran=True,
        passed=probe.returncode == 0,
        errors=len(errors),
        warnings=sum(1 for line in lines if line.startswith("WARNING")),
        detail=errors[0][:300] if errors else "",
    )
    return result


def prune_config_dir(paths: DatasetPaths, step: Step) -> None:
    """Delete the per-run config dir once the container has exited.

    A fresh config dir is seeded from the image on every container start, so at
    134 datasets x 2 agents retaining them would cost ~28 GB of plugin cache.
    Nothing in it needs keeping: the native transcript under `projects/` is the
    same conversation `session.jsonl` already captured from stdout.
    """
    shutil.rmtree(paths.config_dir(step), ignore_errors=True)


def previous_attempt(paths: DatasetPaths, step: Step, attempt: int) -> int:
    """The attempt number of the run currently on disk for `step`."""
    try:
        return json.loads(paths.status(step).read_text()).get("attempt", attempt - 1)
    except (OSError, json.JSONDecodeError):
        return attempt - 1


def rotate_review(paths: DatasetPaths, attempt: int) -> None:
    """Archive a previous review report before the reviewer overwrites it.

    `review/` is a data mount, so `rotate_step_dir` does not reach it, and a
    repair would otherwise leave only the last review of a dataset that was
    rejected twice for different reasons.
    """
    reports = [path for path in paths.review.glob("*") if path.is_file()]
    if not reports:
        return
    archive = paths.review / f"attempt-{previous_attempt(paths, Step.REVIEWER, attempt)}"
    archive.mkdir(parents=True, exist_ok=True)
    for report in reports:
        shutil.move(str(report), str(archive / report.name))


def rotate_step_dir(paths: DatasetPaths, step: Step, attempt: int) -> None:
    """Archive the previous run's files under `attempt-<n>/` before overwriting.

    Only the per-run status is authoritative, so history must survive a repair.
    """
    step_dir = paths.step_dir(step)
    if not (step_dir / "status.json").exists():
        return
    archive = step_dir / f"attempt-{previous_attempt(paths, step, attempt)}"
    archive.mkdir(parents=True, exist_ok=True)
    for name in (
        "status.json",
        "session.jsonl",
        "stderr.log",
        "prompt.md",
        "command.txt",
    ):
        source = step_dir / name
        if source.exists():
            shutil.move(str(source), str(archive / name))


def check_image(config: RunConfig) -> str | None:
    """Return a reason when the container image is not available locally.

    Presence is tested with `docker images -q` rather than `docker image
    inspect`: under the containerd image store, inspect fails on the
    multi-platform manifest list that `docker build` produces here, so it
    reports a perfectly good image as missing.
    """
    probe = subprocess.run(
        ["docker", "images", "-q", config.image],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode == 0 and probe.stdout.strip():
        return None
    if probe.returncode != 0:
        return f"could not query docker: {probe.stderr.strip()[:200]}"
    return (
        f"container image {config.image!r} not found. "
        f"Build it first: docker build -t {config.image} ."
    )


def check_auth(config: RunConfig, timeout_s: int = 60) -> str | None:
    """Verify the credential end to end in a real container.

    One short call before a batch that may run for hours. Without it a bad
    credential is only discovered per dataset, where it looks like an
    infrastructure failure and is retried.

    Args:
        config: Run configuration.
        timeout_s: Wall clock limit for the probe. A rejected key makes the
            agent CLI retry rather than exit, so this bound is load-bearing.

    Returns:
        None when the agent authenticated, otherwise the reason.
    """
    try:
        env = agent_env(config)
    except MissingCredentials as error:
        return str(error)

    # Named so the container can be removed if the probe has to be killed:
    # killing the docker client does not stop the container it started.
    name = f"annotate-auth-probe-{uuid.uuid4().hex[:12]}"
    try:
        probe = subprocess.run(
            ["docker", "run", "--rm", "--name", name, "-e", API_KEY_VAR, config.image,
             "claude", "-p", "--output-format", "json", "--max-turns", "1",
             "Reply with the single word OK."],
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout_s,
            check=False,
        )  # fmt: skip
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
        return (
            f"auth probe timed out after {timeout_s}s. The agent CLI retries a "
            f"rejected credential rather than exiting, so this usually means "
            f"{API_KEY_VAR} is invalid or the network is blocked."
        )

    try:
        event = json.loads(probe.stdout)
    except json.JSONDecodeError:
        event = {}
    text = event.get("result", "") or probe.stdout
    if looks_like_auth_failure(event, text):
        return (
            f"the container rejected {API_KEY_VAR}: {text.strip()[:200]}. "
            "Check that the key is current and has API access."
        )
    if probe.returncode != 0:
        detail = (probe.stderr or text).strip()[:200]
        return f"auth probe exited {probe.returncode}: {detail}"
    return None


def preflight(config: RunConfig) -> list[str]:
    """Check everything that would otherwise fail identically on every dataset.

    Returns:
        A list of problems; empty means the batch is safe to start.
    """
    if problem := check_image(config):
        return [problem]
    try:
        resolve_api_key(config)
    except MissingCredentials as error:
        return [str(error)]
    return [problem] if (problem := check_auth(config)) else []
