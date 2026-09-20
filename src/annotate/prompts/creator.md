# Annotate {{ACCESSION}}

Produce a specification-valid, evidence-backed SDRF for **{{ACCESSION}}** —
{{TITLE}}.

Run `/sdrf-skills:sdrf-annotate {{ACCESSION}}` and follow it. The skills are the
specification for how to annotate.
{{REPAIR_SECTION}}

## Deliverables

| Path     | Contents                                                                                                             |
| -------- | -------------------------------------------------------------------------------------------------------------------- |
| `sdrf/`  | The SDRF only, as `{{ACCESSION}}.sdrf.tsv`, or `{{ACCESSION}}-<template>.sdrf.tsv` per template when the rows split. |
| `files/` | Evidence you relied on, plus `sources.json` (shape below). The reviewer reads this instead of re-fetching.           |
| `raw/`   | Raw MS files, if you need any.                                                                                       |

Everything else is transient and discarded when the container exits. Do not run
`/sdrf-skills:sdrf-contribute`, `git`, or `gh` — publication is the host's
decision.

`sources.json` is read across every dataset in the batch, so it has a fixed
core. Add whatever else you find useful around it:

```json
{
  "accession": "{{ACCESSION}}",
  "sources": [
    {
      "id": "pride-project",
      "url": "https://www.ebi.ac.uk/pride/ws/archive/v3/projects/{{ACCESSION}}",
      "used_for": ["instrument", "organism"]
    }
  ]
}
```

Every entry needs `id`, `used_for`, and one of `url`, `urls` or `local`.

`raw/` is disk-capped by the host, which kills the run if you exceed the cap, so
prefer metadata to bytes. Range-reading a ZIP central directory is often enough:
on PXD001792, ~2 KB pulled from a 170 MB archive yielded the complete 201-run
sample map. Download raw files only when an annotation cannot be resolved
otherwise, and only one per instrument or acquisition method.

## Where the skills are wrong

`sdrf-annotate` tells you to set `comment[sdrf annotation tool]` to
`manual curation`. That is false here — this file is produced by an agent, not
by hand. The column is pattern-validated free text with no controlled
vocabulary, so name the tool instead: `NT=sdrf-skills;VV=v<plugin version>`,
taking the version from the installed plugin rather than guessing it.

`parse_sdrf` 0.1.6 validates against the **union** of all `--template` values.
That is what you want when every row declares the same templates — pass them
all in one call. Split the rows and validate each subset separately only when
the file genuinely mixes row kinds, where the union would impose one group's
constraints on the other's rows.

Pass `--use_ols_cache_only` while iterating. The ontology cache is baked into
this image, so a cached run takes seconds against 2–3 minutes live. Do one final
run without the flag before you finish.

## Blocked

Two specification gaps are known and are not your fault. Emit
`outcome: "blocked"` naming the gap rather than writing a value you know to be
wrong:

1. Mixed tissue + cell-line datasets: `cell-lines` requires
   `characteristics[cell line]` while forbidding both reserved words, so tissue
   rows have no legal value.
2. Sampling-time units lack `second`, report non-integer floating point values in the unit `minute` if you need to report smaller values

Anything you genuinely cannot determine is `blocked` too. Never invent a
sample-to-file map, a channel map, demographics, or runs.

## Output

End your final message with one fenced ```json block and nothing after it:

```json
{
  "schema_version": "1.0.0",
  "role": "creator",
  "accession": "{{ACCESSION}}",
  "outcome": "completed",
  "blocked_reason": null,
  "assumptions": [
    "label-free inferred from absence of TMT reagents in Methods"
  ],
  "unresolved": [
    {
      "column": "characteristics[age]",
      "detail": "reported only as per-group means, no per-subject values"
    }
  ],
  "spec_gaps": [
    {
      "column": "comment[proteomics data acquisition method]",
      "detail": "no PRIDE term for BoxCar; recorded as PRIDE:0000627 (DDA)"
    }
  ],
  "sdrf_files": ["sdrf/{{ACCESSION}}.sdrf.tsv"]
}
```

`outcome`

- `completed`
- `blocked`
- `failed`

`blocked` needs a `blocked_reason`.

`sdrf_files` lists the SDRF files you wrote and **nothing else** — evidence,
scripts and intermediate tables under `files/` do not belong there.

`assumptions` are inferences the evidence supports but does not state.

`unresolved` and `spec_gaps` both take a real SDRF column name and one
sentence. They are counted by column across the whole batch, so name the
column exactly as it appears in your header — a paraphrase is not countable.

- `unresolved` — the sources never stated the value. Nobody can fix this.
- `spec_gaps` — the specification cannot express what the sources do state:
  no ontology term exists, the unit is unavailable, the column takes one
  scalar where the data varies. These go to the SDRF maintainers, so report
  one even when you found a defensible workaround, and say what you wrote.
