# Annotate {{ACCESSION}}

Produce a specification-valid, evidence-backed SDRF for **{{ACCESSION}}** —
{{TITLE}}.

Run `/sdrf-skills:sdrf-annotate {{ACCESSION}}` and follow it. The skills are the
specification for how to annotate.
{{REPAIR_SECTION}}

## Deliverables

| Path     | Contents                                                                                                                                                                |
| -------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `sdrf/`  | The SDRF only, as `{{ACCESSION}}.sdrf.tsv`, or `{{ACCESSION}}-<template>.sdrf.tsv` per template when the rows split.                                                    |
| `files/` | Evidence you relied on, plus `sources.json`: every source you opened, its URL or identifier, and what you took from it. The reviewer reads this instead of re-fetching. |
| `raw/`   | Raw MS files, if you need any.                                                                                                                                          |

Everything else is transient and discarded when the container exits. Do not run
`/sdrf-skills:sdrf-contribute`, `git`, or `gh` — publication is the host's
decision.

`raw/` is disk-capped by the host, which kills the run if you exceed the cap, so
prefer metadata to bytes. Range-reading a ZIP central directory is often enough:
on PXD001792, ~2 KB pulled from a 170 MB archive yielded the complete 201-run
sample map. Download raw files only when an annotation cannot be resolved
otherwise, and only one per instrument or acquisition method.

## Where the skills are wrong

`parse_sdrf` 0.1.6 validates against the **union** of all `--template` values, so a mixed
dataset must be split by declared template and each subset validated against its
own single `--template`.

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
  "unresolved": ["sample->file mapping for 4 runs not determinable"],
  "artifacts": ["sdrf/{{ACCESSION}}.sdrf.tsv"]
}
```

`outcome`

- `completed`
- `blocked`
- `failed`

`blocked` needs a `blocked_reason`.

`assumptions` are inferences the evidence supports but does not state
`unresolved` is what you could not determine.
