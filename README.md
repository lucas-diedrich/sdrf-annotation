# SDRF-annotation workflow

Parallelizable auto-annotation workflow of proteomics datasets with SDRF files. Implemented as a containerized, creator/reviewer two-agent pipeline.

> [!Warning]
> This workflow was generated with Claude Opus 5. See DESIGN.md for an overview over the package.

## Setup

### Docker build

Build the image:

```bash
docker build -t sdrf-annotation .
```

Authenticate once. The resulting config dir is only used for interactive workpipeline runs get a fresh, per-run config dir seeded from the image.

```bash
rm -rf claude_credentials_setup && mkdir claude_credentials_setup
source .env # You need to provide your Anthropic API key
docker run --rm -it \
  -v "$(pwd)/claude_credentials_setup:/.claude" \
  -e CLAUDE_CONFIG_DIR=/.claude \
  -e "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY" \
  sdrf-annotation claude
```

### Orchestrator installation

```bash
mamba create -y -n sdrf python=3.13
~/mamba/envs/sdrf/bin/pip install -e ".[dev]"
```

Run with

```shell
annotate --help
```

The `annotate` package only provides an opinionated orchestrator for the docker-based workflow. Everything that talks to PRIDE, OLS or PubMed runs inside the container.

## Execute the workflow

```bash
annotate run    --seed data/datasets.csv --work sdrf-annotations/ --concurrency 2 --timeout-s 3600 # Concurrency defaults to 2: `parse_sdrf` caps there, and so do the OLS and PRIDE rate limits.
annotate status --work sdrf-annotations/ -v
annotate retry  --work sdrf-annotations/ --state failed_infra
annotate rollup --work sdrf-annotations/
annotate purge  --work sdrf-annotations/ --raw
```

### Run on a subset of datasets

These commands run the workflow on a user-provided list of accessions:

```bash
# 1. five datasets, two at a time
annotate run --work sdrf-annotations/ --accession PXD009348 --accession PXD038699 --accession PXD062231 --concurrency 2

# Inspect: in-flight datasets sit at `creating` or `reviewing`
annotate status --work sdrf-annotations/ -v

# Resume interrupted secions. Terminal datasets are skipped; an interrupted step restarts.
annotate run --work sdrf-annotations/ --limit 5 --concurrency 2

# If a rollup is ever corrupted, rebuild it from the per-run status files
annotate rollup --work sdrf-annotations/
```

### Run on a csv file of datasets

```bash
# Dry run: confirms the selection and renders every prompt
annotate run --work sdrf-annotations/ --dry-run

# Run the batch, detached, with the trace on disk
nohup annotate run --work sdrf-annotations/ --concurrency 2 > sdrf-annotations/run.log 2>&1 &

# Monitor status
watch -n 60 "annotate status --work sdrf-annotations/"

# Afterwards: retry infrastructure failures, then contract failures
annotate retry --work sdrf-annotations/ --state failed_infra
annotate retry --work sdrf-annotations/ --state failed_contract
```

Budget before starting. Peak disk is `concurrency × (raw budget + 106 MB plugin
cache)`; steady-state disk is the SDRFs, the literature in `files/`, and the
session traces. Blocked datasets are staged under `sdrf-annotations/sandbox/<ACC>/` with a
`BLOCKED.md`, matching the upstream CI-exempt convention.

### Results layout

```
sdrf-annotations/
  status.json               # workflow rollup (derived, regenerable)
  sandbox/PXD012345/        # blocked datasets + BLOCKED.md
  PXD012345/
    sdrf/                   # the deliverable
    files/                  # literature + sources.json
    raw/                    # raw MS files, purged at a terminal state
    logs/                   # never mounted into any container
      status.json           # dataset rollup + state machine
      creator/
        session.jsonl       # the full agent trace
        status.json         # host-written run record (authoritative)
        prompt.md           # the exact prompt this run received
        command.txt         # the exact docker command
        stderr.log
        attempt-1/          # previous attempts, rotated on repair
      reviewer/             # same shape
        session.jsonl       # the full agent trace
        status.json         # host-written run record (authoritative)
        prompt.md           # the exact prompt this run received
        command.txt         # the exact docker command
        stderr.log
        attempt-1/          # previous attempts, rotated on repair
```

Only the per-run `status.json` files are authoritative. Both rollups are derived
from them, so a corrupted rollup is always regenerable with `annotate rollup`.

## References

> sdrf-skills repository. https://github.com/bigbio/sdrf-skills.git

> The original claude code docker container setup was provided by Magnus Schwörer @mschwoer
