"""Two-agent (creator -> reviewer) SDRF annotation pipeline over PRIDE accessions.

Each agent runs in its own `sdrf-annotation` container against per-dataset bind
mounts. The host owns everything the agents cannot be trusted with: artifact
hashing, contract validation, the state machine, and disk enforcement. `logs/`
is never mounted, so the only channel from an agent to the host is the JSON
block it prints at the end of its run.

Modules:
    models     Data models and the dataset state machine.
    utils      Filesystem, JSON and text helpers with no pipeline knowledge.
    contracts  Schema validation and artifact hash binding.
    artifacts  Host-side inspection of the SDRF files on disk.
    prompts    Prompt rendering and the repair brief.
    runner     Docker invocation, output streaming, disk watchdog.
    pipeline   Rollups, the per-dataset loop, batch execution.
    analysis   Post-hoc aggregation of a finished batch.
    cli        The `annotate` command.
"""

from importlib.metadata import version

from annotate.models import (
    DatasetPaths,
    DatasetRollup,
    Event,
    RunConfig,
    RunResult,
    RunStatus,
    SeedRow,
    State,
    Step,
    TransitionError,
    transition,
)

__version__ = version("annotate")

__all__ = [
    "DatasetPaths",
    "DatasetRollup",
    "Event",
    "RunConfig",
    "RunResult",
    "RunStatus",
    "SeedRow",
    "State",
    "Step",
    "TransitionError",
    "__version__",
    "transition",
]
