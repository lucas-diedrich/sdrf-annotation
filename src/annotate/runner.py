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
from pathlib import Path
from typing import Any

from annotate.models import DatasetPaths, RunConfig, RunResult, Step
from annotate.utils import dir_size_bytes


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
    quietly becoming a producer. `logs/` is mounted nowhere, so an agent has no
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
    config_dir = paths.config_dir(step)
    config_dir.mkdir(parents=True, exist_ok=True)
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
        "-v", f"{config_dir.resolve()}:/.claude",
        config.image,
        "claude", "-p",
        "--output-format", "stream-json", "--verbose",
        "--session-id", session_id,
        "--permission-mode", config.permission_mode,
        prompt,
    ]  # fmt: skip


def _consume_stream(process: subprocess.Popen, session_log: Any) -> tuple[dict, str]:
    """Tee the agent's stream-json stdout to disk and pull out its final text.

    Returns:
        (result_event, final_text). The `result` event is the stream's last
        line; the assistant fallback covers a run that produced text but no
        result event, which would otherwise look like silence.
    """
    result_event: dict[str, Any] = {}
    final_text = ""
    for line in process.stdout:
        session_log.write(line)
        session_log.flush()
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "result":
            result_event = event
            final_text = event.get("result") or ""
        elif event.get("type") == "assistant" and not final_text:
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    final_text = block.get("text", "")
    return result_event, final_text


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
            env=dict(os.environ),
        )
        # The timeout kills the container rather than abandoning the pipe: a
        # surviving run would hold the config-dir mount that pruning removes.
        killer = threading.Timer(config.timeout_s, process.kill)
        killer.start()
        watchdog = RawBudgetWatchdog(paths.raw, config.raw_budget_gb, process)
        watchdog.start()
        try:
            result_event, final_text = _consume_stream(process, session_log)
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
    )


def prune_config_dir(paths: DatasetPaths, step: Step) -> None:
    """Delete the per-run config dir once the container has exited.

    A fresh config dir is seeded from the image on every container start, so at
    134 datasets x 2 agents retaining them would cost ~28 GB of plugin cache.
    Nothing in it needs keeping: the native transcript under `projects/` is the
    same conversation `session.jsonl` already captured from stdout.
    """
    shutil.rmtree(paths.config_dir(step), ignore_errors=True)


def rotate_step_dir(paths: DatasetPaths, step: Step, attempt: int) -> None:
    """Archive the previous run's files under `attempt-<n>/` before overwriting.

    Only the per-run status is authoritative, so history must survive a repair.
    """
    step_dir = paths.step_dir(step)
    previous = step_dir / "status.json"
    if not previous.exists():
        return
    try:
        previous_attempt = json.loads(previous.read_text()).get("attempt", attempt - 1)
    except (OSError, json.JSONDecodeError):
        previous_attempt = attempt - 1
    archive = step_dir / f"attempt-{previous_attempt}"
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
