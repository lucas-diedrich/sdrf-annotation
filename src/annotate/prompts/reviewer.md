# Review {{ACCESSION}}

Independently falsify the SDRF files in `sdrf/` for **{{ACCESSION}}** — {{TITLE}}.

Run `/sdrf-skills:sdrf-adversarial-review` and follow it. This prompt only
covers what sits on top of that skill.

Return findings; repair is the creator's job. Do not run
`review_gate.py approve` — there is no git worktree here, so the gate cannot
work; the hash echo below replaces it.

## Project Structure

`review/` is the only directory you can write to. Everything else is read-only:
do not try to edit it.

```
sdrf/
  SDRF files
files/
  Text files that are associated with the metadata annotations in the SDRF files
raw/
  Raw mass spectrometry files that are associated with the metadata annotations in the SDRF files
review/
  Yours. Write the review contract's report JSON to review/{{ACCESSION}}.review.json
  -- that exact path -- and its evidence manifest beside it.
```

Write the report on **every** verdict, not only a pass: a rejection is the
report worth keeping. The JSON block at the end of this prompt is the host's
channel and is separate from the report; neither replaces the other.

## Where the skills are wrong

`sdrf-annotate` tells the creator to set `comment[sdrf annotation tool]` to
`manual curation`. That is false for an agent-produced file, so the expected
value here is `NT=sdrf-skills;VV=v<plugin version>`. Do not raise a finding
against it, and do not recommend reverting to `manual curation`. The column is
pattern-validated free text with no controlled vocabulary — there is no
ontology term for an agent or LLM to prefer instead.

`parse_sdrf` 0.1.6 validates against the **union** of all `--template` values,
not the last one — `CLAUDE.md` invariant #7 describes superseded behaviour. For
a file whose rows all declare the same templates that union is correct: pass
them together. Split only where rows declare different template sets. Derive
the active templates from the file's `comment[sdrf template]` columns — the
column is repeated, once per template — not from anything the creator asserts.

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

Hash every SDRF in `sdrf/` **before** you review it and echo the hashes; the
host recomputes them and discards your verdict on a mismatch. Review every file
in `sdrf/`, not a subset. End your final message with one fenced ```json block
and nothing after it:

```json
{
  "schema_version": "1.0.0",
  "role": "reviewer",
  "accession": "{{ACCESSION}}",
  "sdrf_files": [
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

`sdrf_files` lists the files in `sdrf/` and **nothing else** — evidence, scripts and intermediate tables under `files/` do not belong there, but use these file for review. Every SDRF in `sdrf/` must appear, carrying the hash you reviewed it at.

`deterministic` records every check you actually ran with its real result; never
report a check you could not run as passing. Every finding needs a precise
location, the claim it disputes, evidence specific enough to re-check, and a
concrete correction — an accession or a value, not "review this".
