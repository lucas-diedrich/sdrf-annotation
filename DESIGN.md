## Design notes

## Structure

Each agent is a Claude Code run in its own `sdrf-annotation` container, driven by
the [`bigbio/sdrf-skills`](https://github.com/bigbio/sdrf-skills) plugin. The
host — the `annotate` package — owns everything the agents cannot be trusted
with: artifact hashing, contract validation, the state machine, disk
enforcement, and resumability. `logs/` is never mounted, so the only channel
from an agent back to the host is the JSON block it prints at the end of its
run.

```
src/annotate/
  models.py     Data models and the dataset state machine.
  utils.py      Filesystem, JSON and text helpers with no pipeline knowledge.
  contracts.py  Schema validation and artifact hash binding.
  prompts.py    Prompt rendering and the repair brief.
  runner.py     Docker invocation, output streaming, disk watchdog.
  pipeline.py   Rollups, the per-dataset loop, batch execution.
  cli.py        The `annotate` command (typer).
  prompts/      creator.md, reviewer.md   (packaged, overridable)
  schemas/      creator + reviewer output contracts
```

`runner` is the only module that touches the outside world, which is what lets
every other module be tested without docker.

**Mounts.** The creator gets `sdrf/`, `files/` and `raw/` read-write; the
reviewer gets the same three read-only. A read-only `sdrf/` is what stops the
reviewer quietly becoming a producer. `logs/` is mounted nowhere.

**Hash binding.** The reviewer echoes the SHA-256 of every artifact it judged;
the host recomputes them and discards the verdict on any mismatch, since a
mismatch means the review describes content other than what is on disk. This
replaces `review_gate.py`, which cannot work here — it discovers changed
artifacts from git against a merge base, and a bare mounted `sdrf/` is not a
repository. The creator declares artifact _paths_ only: a producer hashing its
own output proves nothing, so the host hashes the disk itself.

**Validation gate.** The host validates every SDRF after the creator, offline
and per file: each row group is checked against only the templates it declares.
A failing file never reaches the reviewer. The dataset moves to `reviewed_fail`
(`validation_fail` event), the errors are written to `logs/validation.json`, and
the next creator run receives them as its repair brief, counted against the
same repair cap. The reviewer is also asked to run `parse_sdrf`, but nothing
enforces that, and it passed files that did not validate. `annotate repair`
reuses the same path for a live-OLS failure found after review. It is the one
transition out of the terminal `reviewed_pass`.

**Disk is enforced by the host, not the prompt.** `raw/` is polled during the
run and the container is killed on a breach (`--raw-budget-gb`, default 20). The
container's own scratch space is a size-capped tmpfs (`--scratch-gb`, default 2),
so a runaway write fails with `ENOSPC` instead of filling the host.
`--storage-opt size=` is not usable for either: Docker Desktop accepts it and
silently does not enforce it, and it would never cover a bind mount anyway.

**Per-run config dirs.** Every container start rewrites `settings.json`,
`.claude.json` and `plugins/installed_plugins.json`, so a shared config dir
under concurrency is an unsynchronised read-modify-write on shared JSON. Each
run gets its own, seeded from the image (no network needed), and it is deleted
immediately afterwards — the native transcript in it is the same conversation
`session.jsonl` already captured.

**Permission mode.** Runs default to `--permission-mode bypassPermissions`. The
plan specified `acceptEdits`, but that leaves Bash gated, and a gated Bash call
under `claude -p` is denied outright — there is nobody to answer the prompt, so
the agent cannot run `parse_sdrf` at all. The container is the isolation
boundary here: the agent sees three bind mounts and has no path to `logs/` or
the host. Override with `--permission-mode` for an attended debug run.

**Prompts.** `src/annotate/prompts/creator.md` and `reviewer.md` are deliberately
thin. The `sdrf-skills` plugin is the single source of truth for how to
annotate; the prompts cover only what sits on top of it (the output contract,
the deliverable layout) and where the skills are wrong for this environment
(notably `parse_sdrf --template`, which is a union in 0.1.6 — upstream
`CLAUDE.md` invariant #7 describes superseded behaviour). Placeholders
`{{ACCESSION}}`, `{{TITLE}}` and `{{REPAIR_SECTION}}` are substituted per run;
an unsubstituted placeholder raises rather than silently shipping a prompt that
tells an agent to annotate `{{ACCESSION}}`.

## Container

Beyond the base Claude Code image, this one carries the `sdrf-skills` plugin and
its MCP servers, `parse_sdrf`, `techsdrf`, ThermoRawFileParser, and:

- **`poppler-utils`** — `pdftoppm`, which the Read tool shells out to for PDFs.
  Without it Read advertises PDF support and fails on every PDF.
- **The sdrf-pipelines ontology cache** (18 parquet files, 39 MB), baked into
  the package's local-ontology directory. `--use_ols_cache_only` _raises_
  without it rather than falling back, so every validation would go to live OLS
  at 2–3 minutes a run instead of about 10 seconds. It cannot live in the pooch
  user cache: that resolves under `$HOME`, which belongs to a different UID once
  the entrypoint drops privileges.
- **A writable `$HOME` and `/workspace`.** `gosu` does not reset `HOME`, and the
  node base image hardcodes `HOME=/home/node` (uid 1000), so any other runtime
  UID had an unwritable home and a root-owned `/workspace`.

## Tests

```bash
~/mamba/envs/sdrf/bin/python -m pytest
~/mamba/envs/sdrf/bin/python -m ruff check src tests
```

125 tests, no network and no containers: `runner.run_agent` is the only seam
that touches the outside world, so replacing it exercises the state machine,
the contract layer, the hash binding, the repair loop and resume behaviour
directly.
