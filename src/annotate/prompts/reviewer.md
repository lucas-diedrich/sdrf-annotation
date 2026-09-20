# Review {{ACCESSION}}

Independently falsify the SDRF in `sdrf/` for **{{ACCESSION}}** — {{TITLE}}.

Run `/sdrf-skills:sdrf-adversarial-review` and follow it. This prompt only
covers what sits on top of that skill and where the skills are wrong.

You are a fresh context with no access to the creator's transcript or reasoning.
`sdrf/`, `files/` and `raw/` are mounted read-only: you cannot edit the SDRF and
must not try. Return findings; repair is the creator's job. Do not run
`review_gate.py approve` — there is no git worktree here, so the gate cannot
work; the hash echo below replaces it.

## Emphasis

Re-running the validators on a file the creator already validated will almost
always pass. Run them as a regression guard, but do not mistake a green
validator for a review.

The substance is **the literature read against the annotation**: disease and
organism terms, strain, sex, treatment and dose, chemistry, instrument,
acquisition method, label scheme, and sample-to-file mapping. Flag any per-row
value whose granularity exceeds its evidence — a Methods sentence about the
cohort does not license a per-sample value. Read `files/` rather than
re-fetching the creator's literature, but do re-fetch the PRIDE file list and
project record: they are cheap, and a wrong file mapping turns on them.

## Where the skills are wrong

`parse_sdrf` 0.1.6 validates against the **union** of all `--template` values,
not the last one — `CLAUDE.md` invariant #7 describes superseded behaviour. A
multi-template artifact must be split by declared template and each subset
validated against its own single `--template`. Derive the active templates from
`comment[sdrf template]` in the file, not from anything the creator asserts.

Pass `--use_ols_cache_only` while iterating; the ontology cache is baked into
this image. Do one final run without it.

## Verdict

- `pass` — every check you ran passed and no `error` finding remains. Warnings
  and info findings are compatible with a pass.
- `fail` — at least one `error` finding, or you could not verify specification
  compliance, ontology integrity, omission safety, or deterministic validation.
- `blocked` — the specification cannot express this dataset (for example
  `cell-lines` leaving tissue rows no legal value, or sampling-time units
  lacking `second`). Terminal and not retried, so name the gap precisely.

The creator gets two repair attempts. A soft pass spends none of them and ships
the error.

## Output

Hash every artifact **before** you review it and echo the hashes; the host
recomputes them and discards your verdict on a mismatch. Review every file in
`sdrf/`, not a subset. End your final message with one fenced ```json block and
nothing after it:

```json
{
  "schema_version": "1.0.0",
  "role": "reviewer",
  "accession": "{{ACCESSION}}",
  "artifacts": [
    { "path": "sdrf/{{ACCESSION}}.sdrf.tsv", "sha256": "<64 hex chars>" }
  ],
  "verdict": "pass",
  "blocked_reason": null,
  "deterministic": [
    { "check": "parse_sdrf", "templates": ["ms-proteomics"], "passed": true },
    { "check": "tools check", "passed": true },
    { "check": "tools score", "score": 87 }
  ],
  "findings": [
    {
      "severity": "error",
      "file": "sdrf/{{ACCESSION}}.sdrf.tsv",
      "row": 12,
      "column": "characteristics[disease]",
      "claim": "annotated as melanoma",
      "evidence": "PMID 35695565, Methods para 3 states lung adenocarcinoma",
      "recommendation": "MONDO:0005061"
    }
  ],
  "literature_agreement": { "reviewed": ["35695565"], "contradictions": [] }
}
```

`deterministic` records every check you actually ran with its real result; never
report a check you could not run as passing. Every finding needs a precise
location, the claim it disputes, evidence specific enough to re-check, and a
concrete correction — an accession or a value, not "review this".
