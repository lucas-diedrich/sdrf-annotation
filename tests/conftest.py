"""Shared fixtures for the pipeline tests.

Every test here runs against a fake agent: `runner.run_agent` is the only seam
that touches the outside world, so replacing it exercises the state machine,
the contract layer and the hash binding without a container.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from annotate.models import DatasetPaths, RunConfig, RunResult, Step


@pytest.fixture
def work(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    root.mkdir()
    return root


@pytest.fixture
def seed(tmp_path: Path) -> Path:
    path = tmp_path / "datasets.csv"
    path.write_text(
        '"pride_accession","title","annotated","sandbox_draft"\n'
        '"PXD000001","A test dataset",false,false\n'
        '"PXD000002","Already annotated",true,false\n'
    )
    return path


@pytest.fixture
def config(work: Path, seed: Path) -> RunConfig:
    return RunConfig(work=work, seed=seed, concurrency=1, timeout_s=60)


@pytest.fixture
def sha256_of():
    def digest(text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()

    return digest


class FakeAgent:
    """Stand-in for `run_agent` that writes artifacts and returns canned JSON.

    Each entry in `script` is a (step, callable) pair consumed in order. The
    callable receives the dataset's `DatasetPaths` and returns the text the
    agent would have printed; writing to `sdrf/` from inside it is how a test
    simulates a creator producing or corrupting an artifact.
    """

    def __init__(self, script, exit_code: int = 0, timed_out: bool = False):
        self.script = list(script)
        self.calls: list[Step] = []
        self.exit_code = exit_code
        self.timed_out = timed_out
        self.over_budget = ""

    def __call__(
        self, step: Step, paths: DatasetPaths, prompt: str, config: RunConfig
    ) -> RunResult:
        self.calls.append(step)
        expected_step, producer = self.script.pop(0)
        assert step == expected_step, f"expected a {expected_step} run, got {step}"
        return RunResult(
            exit_code=self.exit_code,
            timed_out=self.timed_out,
            duration_s=1.0,
            session_id="00000000-0000-0000-0000-000000000000",
            result_event={
                "type": "result",
                "num_turns": 3,
                "total_cost_usd": 0.1,
                "usage": {"input_tokens": 10, "output_tokens": 20},
            },
            final_text=producer(paths),
            over_budget=self.over_budget,
        )


@pytest.fixture
def fake_agent(monkeypatch):
    """Install a scripted FakeAgent in place of the real container runner."""

    def install(script, **kwargs):
        agent = FakeAgent(script, **kwargs)
        # Patched on the `runner` module, which is how `pipeline` reaches it.
        monkeypatch.setattr("annotate.runner.run_agent", agent)
        return agent

    return install
