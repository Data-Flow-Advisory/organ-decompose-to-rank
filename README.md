# organ-decompose-to-rank

A **pure decision organ** extracted from discovery-engine
(`app/services/decompose_to_rank.py`). It is the **DECOMPOSE** front-end for the
fuzzy roadmap: it turns natural-language strategic *stream docs* into measured
`c4_*` atom maps that a downstream ranker (the C4 roadmap adapter) can rank.

> APES ranks **structured** queues by reading atoms off DB columns. The roadmap
> is **fuzzy** — each initiative is a markdown stream doc. This organ observes
> each doc with a rules-based regex proposer, reduces every fuzzy `c4_*` term to
> a CLEAR measured atom, computes the two corpus-relative atoms, and emits the
> rankable atom maps. *Reasoner proposes, engine disposes* — the ranking itself
> is the engine, out of scope here.

## Contract

`decide(state, context) -> {output, rationale, self_metric}`

### Input `state`

```json
{
  "streams": [
    {
      "body": "markdown stream doc text (required)",
      "stream_id": "stream_36 (optional; else derived from filename)",
      "filename": "36-native-pa-app.md (optional; gives the NN + enables companion-merge)",
      "title": "optional; else the first H1 of body, else the id"
    }
  ],
  "config": { "working_days_per_week": 5.0 }
}
```

The organ does **no file I/O** — the caller pre-loads each stream doc's `body`.
A `filename` of the shape `NN-slug.md` yields the stream id `stream_NN` and
lets sub-docs that share the same `NN` (e.g. `36-native-pa-app-alex.md`) be
folded into the parent (shortest filename = parent).

### Output

```json
{
  "output": {
    "streams": [
      {
        "id": "stream_05",
        "title": "Discovery interview",
        "atoms": {
          "c4_live": true,
          "c4_shipped": false,
          "c4_blocked_hard": false,
          "c4_unblocks_count": 1,
          "c4_effort_weeks": 3.0,
          "c4_impact_measurable": true,
          "c4_blocked_soft": false
        },
        "blocked_by": [],
        "blocked_soft_by": [],
        "clear": true
      }
    ],
    "count": 1
  },
  "rationale": "Decomposed 1 stream doc(s) into CLEAR c4_* atom maps ...",
  "self_metric": {
    "confidence": 1.0,
    "signals": { "stream_count": 1, "live_count": 1, "...": "..." }
  }
}
```

## The `c4_*` atoms

| atom | meaning | source |
|------|---------|--------|
| `c4_live` | not shipped and not cancelled | status |
| `c4_shipped` | terminal-shipped status / `**Shipped**` marker | status |
| `c4_blocked_hard` | hard-depends on an **un-shipped** predecessor | corpus-relative |
| `c4_unblocks_count` | how many **live** streams hard-depend on this one | corpus-relative |
| `c4_effort_weeks` | weeks of effort from the `**Complexity:**` line, else `null` | regex |
| `c4_impact_measurable` | doc states a revenue / tenant / users-blocked signal | regex |
| `c4_blocked_soft` | has any soft (`benefits from` / `Companion to`) predecessor | regex |

Every produced map is verified **CLEAR**: each of the 7 atoms is *declared*
(present). `null` / `0` / `false` are measured values ("the doc didn't say"),
never guesses — so an absent atom is a measurement, not ambiguity.

## Design properties

- **Pure** — JSON in, JSON out. No DB, no Flask, no env, no file reads.
- **Fail-open** — any internal error returns an empty decomposition with
  `confidence: 0.0`; the decider never raises.
- **Self-contained CLEAR check** — the upstream ambiguity-cartographer's
  decompose→verify→recurse guarantee is folded into a pure presence check
  (`_verify_clear`); no external ambiguity-engine dependency.
- **`confidence`** reports data completeness: the fraction of streams that
  stated an effort estimate (the most-often-absent atom). `1.0` when empty.

## Run it

```bash
# one sample
ORGAN_INPUT=samples/simple_chain.json python3 organ.py

# from stdin
echo '{"state":{"streams":[{"stream_id":"stream_01","body":"# A\n**Status:** Scoping.\n"}]}}' | python3 organ.py

# tests
python -m pytest -v
```

CI (`.github/workflows/conformance.yml`) shadow-runs the organ on every sample,
reports each verdict + `self_metric` to the job summary, then runs the test
suite — report-only, no action taken on the output.

## Provenance

Extracted from `discovery-engine/app/services/decompose_to_rank.py`
(`correlation_id: decompose-to-rank-roadmap-2026-05-31`). The regex proposer
(`observe_stream`), effort parsing, hard/soft dependency classification,
companion-merge, and corpus-relative atom computation are ported verbatim in
behaviour. The file-loading (`load_streams`) and the ranking call
(`roadmap_adapter.triage`) are intentionally excluded — they are side effects /
the downstream engine.
