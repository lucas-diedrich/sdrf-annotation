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

Optionally, open an interactive session in the image to poke at the skills. This
is for exploration only; pipeline runs do not use the config dir it leaves
behind.

```bash
rm -rf claude_credentials_setup && mkdir claude_credentials_setup
source .env # You need to provide your Anthropic API key
docker run --rm -it \
  -v "$(pwd)/claude_credentials_setup:/.claude" \
  -e CLAUDE_CONFIG_DIR=/.claude \
  -e "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY" \
  sdrf-annotation claude
```

### Credentials

Agents authenticate with an API key which you can configure in the [Claude console](https://platform.claude.com). API keys are resolved in the following priority order:

1. `ANTHROPIC_API_KEY` environment variable, set in the current shell
2. A `.env` in the current working directory variable `ANTHROPIC_API_KEY=sdk_...` set
3. An explicitly set `--env-file PATH` with the variable `ANTHROPIC_API_KEY=sdk_...` set.

The key is injected into the `docker run` subprocess environment and reaches the container through docker's passthrough `-e ANTHROPIC_API_KEY` form, so it never enters the argv and the `command.txt` kept for reproducibility holds no secret.

```bash
annotate doctor   # image present? credential accepted by the container?
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
annotate run --seed data/datasets.csv \
  --work sdrf-annotations \
  --concurrency 2 \ # Concurrency defaults to 2: `parse_sdrf` caps there, and so do the OLS and PRIDE rate limits.
  --timeout-s 3600 \
  --env-file .env
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
# Confirm image and credential before committing to hours of runs
annotate doctor

# Dry run: confirms the selection and renders every prompt
annotate run --work sdrf-annotations/ --dry-run

# Run the batch, detached, with the trace on disk
nohup annotate run --work sdrf-annotations/ --concurrency 2 > sdrf-annotations/run.log 2>&1 &

# Monitor status
watch -n 60 "annotate status --work sdrf-annotations/"

# Afterwards: retry infrastructure failures, then contract failures
annotate retry --work sdrf-annotations/ --state failed_infra
annotate retry --work sdrf-annotations/ --state failed_contract

# Then analyse the batch: what failed, what it cost, what the format could not express
annotate report --work sdrf-annotations/
annotate costs  --work sdrf-annotations/
```

### Contribute the passing datasets

One pull request per accession, pushed to your fork of the dataset repo.

```bash
# Dry run: select, validate, render. Pushes nothing. This is the default.
annotate contribute --work sdrf-annotations/ --repo ../sdrf-annotated-datasets

# Submit a bounded first batch, then everything
annotate contribute --work sdrf-annotations/ --limit 5 --no-dry-run
annotate contribute --work sdrf-annotations/ --no-dry-run

# Target: your fork (the default), or bigbio via the checkout's `upstream` remote
annotate contribute --work sdrf-annotations/ --base-repo lucas-diedrich/sdrf-annotated-datasets --no-dry-run
annotate contribute --work sdrf-annotations/ --base-repo bigbio/sdrf-annotated-datasets --no-dry-run
```

### Results layout

```
sdrf-annotations/
  status.json               # workflow rollup (derived, regenerable)
  sandbox/PXD012345/        # blocked datasets + BLOCKED.md
  PXD012345/
    sdrf/                   # the deliverable
    files/                  # literature + sources.json
    review/                 # the reviewer's report; the only dir it can write
      PXD012345.review.json
      attempt-1/            # superseded reports, rotated on repair
    raw/                    # raw MS files, purged at a terminal state
    logs/                   # never mounted into any container
      status.json           # dataset rollup + state machine (derived)
      events.jsonl          # append-only event log; never rewritten
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

## References

> sdrf-skills repository. https://github.com/bigbio/sdrf-skills.git

> The original claude code docker container setup was provided by Magnus Schwörer @mschwoer
