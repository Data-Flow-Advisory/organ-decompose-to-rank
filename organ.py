#!/usr/bin/env python3
"""
Decompose-to-Rank (D2R) Organ — extracted decision logic from discovery-engine.

A pure decider for the roadmap "decompose" front-end. APES ranks STRUCTURED
queues by reading atoms off DB columns. The roadmap is FUZZY — each strategic
initiative is a stream doc in natural language. This organ is the DECOMPOSE
step: it OBSERVES each stream doc (rules-based regex proposer), reduces every
fuzzy ``c4_*`` term to a CLEAR measured atom, computes the two corpus-relative
atoms (``c4_unblocks_count`` / ``c4_blocked_hard``) that need the whole set,
and returns the rankable atom maps the C4 adapter would then rank.

"Reasoner proposes, engine disposes." The proposer here is RULES-BASED: it
OBSERVES the doc's Status / Complexity / "Blocks on" lines via regex and
REPORTS what it finds — ``None`` for anything absent, NEVER a guess. The
ranking itself (roadmap_adapter.triage) is the engine — out of scope for this
organ, which produces the decomposition the ranker consumes.

Contract:
  INPUT state: {
    "streams": [
      {
        "body": str,            # the stream doc markdown text (required)
        "stream_id": str|null,  # e.g. "stream_36"; else derived from filename
        "filename": str|null,   # e.g. "36-native-pa-app.md"; gives the number +
                                #   enables companion-merge (sub-docs share NN)
        "title": str|null       # else the first H1 of body, else the filename
      }, ...
    ],
    "config": {                 # all optional; defaults shown
      "working_days_per_week": 5.0
    } | null
  }

  OUTPUT: {
    "output": {
      "streams": [
        {
          "id": str, "title": str,
          "atoms": {
            "c4_live": bool, "c4_shipped": bool,
            "c4_blocked_hard": bool, "c4_unblocks_count": int,
            "c4_effort_weeks": float|null, "c4_impact_measurable": bool,
            "c4_blocked_soft": bool
          },
          "blocked_by": [str, ...],       # open (un-shipped) hard predecessors
          "blocked_soft_by": [str, ...],  # soft predecessors
          "clear": bool                   # all 7 c4_* atoms declared (verified)
        }, ...
      ],
      "count": int
    },
    "rationale": "...",
    "self_metric": {
      "confidence": float,   # data completeness: fraction of streams with
                             #   an observed effort estimate (the atom most
                             #   often absent). 1.0 when no streams.
      "signals": {...}       # measured corpus stats
    }
  }

The organ is pure:
  - Takes all stream doc bodies via JSON (no file I/O, no env, no DB)
  - The cartographer "verify CLEAR" guarantee is folded into _verify_clear
    (pure stdlib) — no external ambiguity-engine dependency
  - No side effects; returns only computed advice
  - Never raises on bad input (fail-open)
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Dict, List, Optional, Tuple


# --- Stream filename shape: NN-some-slug.md (NN = the stream number) -----------
_STREAM_FILE_RE = re.compile(r"^(\d{2,3})-(.+)\.md$")

# --- Status / shipped detection ------------------------------------------------
_STATUS_LINE_RE = re.compile(r"^\s*\*\*Status[:.]\*\*\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_SHIPPED_LINE_RE = re.compile(r"^\s*\*\*Shipped\*\*[:.]?\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_TERMINAL_SHIPPED = ("shipped", "landed", "merged", "done", "complete", "completed", "live")
_TERMINAL_CANCELLED = ("cancelled", "canceled", "superseded", "abandoned", "dropped", "obsolete")

# --- Effort / complexity -------------------------------------------------------
_COMPLEXITY_RE = re.compile(r"^\s*\*\*Complexity[:.]\*\*\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_DAYS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:-\s*(\d+(?:\.\d+)?)\s*)?day", re.IGNORECASE)
_WEEKS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:-\s*(\d+(?:\.\d+)?)\s*)?week", re.IGNORECASE)
_DEFAULT_WORKING_DAYS_PER_WEEK = 5.0

# --- Dependencies --------------------------------------------------------------
_BLOCKS_ON_RE = re.compile(r"^\s*\*\*(?:Blocks on|Depends on|Blocked by)[:.]\*\*\s*(.+?)\s*$",
                           re.IGNORECASE | re.MULTILINE)
_COMPANION_RE = re.compile(r"^\s*\*\*(?:Companion to|Ties to)[:.]\*\*\s*(.+?)\s*$",
                           re.IGNORECASE | re.MULTILINE)
_STREAM_REF_RE = re.compile(r"Stream\s+(\d{2,3})", re.IGNORECASE)
_HARD_MARKERS = ("must land first", "must be live", "must complete", "must have shipped",
                 "must precede", "land first", "do that first", "needs this", "before you")
_SOFT_MARKERS = ("can be built", "could be", "can start", "benefits from", "should land first",
                 "for the cleanest path", "migrated later", "wait for", "informs", "can build")

# --- Measurable impact ---------------------------------------------------------
_IMPACT_RE = re.compile(
    r"(£\s*[\d,]+|\$\s*[\d,]+|\b\d+\s*tenants?\b|\b\d+\s*users?\s*(?:blocked|affected)\b"
    r"|\brevenue\b|\bfirst\s*£|\bblocks?\s+all\s+revenue\b|\bARR\b|\bMRR\b)",
    re.IGNORECASE,
)

# The full c4_* atom set. A produced stream atom map is "CLEAR" iff every name
# here is present (declared) — the cartographer's decompose->verify->recurse
# guarantee, folded into a pure presence check (no external engine).
_C4_ATOMS: Tuple[str, ...] = (
    "c4_live",
    "c4_shipped",
    "c4_blocked_hard",
    "c4_unblocks_count",
    "c4_effort_weeks",
    "c4_impact_measurable",
    "c4_blocked_soft",
)


# ---------------------------------------------------------------------------
# Rules-based proposer — OBSERVE the atoms in one stream doc (pure regex).
# ---------------------------------------------------------------------------

def _parse_effort_weeks(complexity_text: str, working_days_per_week: float) -> Optional[float]:
    """Weeks of effort from a complexity string, or None when absent.

    Honours an explicit week range directly; converts a day range to weeks at
    ``working_days_per_week``. "small (hours)" carries no day/week number -> None.
    A range uses its UPPER bound (conservative). NEVER guesses a default.
    """
    wk = _WEEKS_RE.search(complexity_text)
    if wk:
        lo = float(wk.group(1))
        hi = float(wk.group(2)) if wk.group(2) else lo
        return max(lo, hi)
    dy = _DAYS_RE.search(complexity_text)
    if dy:
        lo = float(dy.group(1))
        hi = float(dy.group(2)) if dy.group(2) else lo
        denom = working_days_per_week if working_days_per_week else _DEFAULT_WORKING_DAYS_PER_WEEK
        return round(max(lo, hi) / denom, 2)
    return None


def _classify_dep_line(line: str, stream_id: str) -> Tuple[List[str], List[str]]:
    """Split a 'Blocks on:' line's Stream refs into (hard, soft) id lists.

    No Stream ref (incl. an explicit "none") -> ([], []). A soft marker present
    with no hard marker makes the refs SOFT; otherwise a bare "Blocks on:
    Stream X" is HARD (the default reading — it cannot start until X). A
    deterministic ``in`` check, not interpretation.
    """
    lower = line.lower()
    refs = [f"stream_{int(m.group(1)):02d}" for m in _STREAM_REF_RE.finditer(line)]
    if not refs:
        return [], []
    is_soft = any(m in lower for m in _SOFT_MARKERS) and not any(m in lower for m in _HARD_MARKERS)
    if is_soft:
        return [], refs
    return refs, []


def _read_title(body: str, fallback: str) -> str:
    """First markdown H1 as the title, else the fallback."""
    for line in (body or "").splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
    return fallback


def observe_stream(stream_id: str, title: str, body: str,
                   working_days_per_week: float = _DEFAULT_WORKING_DAYS_PER_WEEK) -> dict:
    """OBSERVE the atoms in one stream doc — pure regex over the doc text.

    Returns a facts dict with ``None`` for every atom the doc does not state
    (never a guess). This is the rules-based proposer core.
    """
    body = body or ""

    # --- shipped / cancelled (status line + bold-Shipped marker + keywords) ---
    shipped = False
    cancelled = False
    status_texts = [m.group(1) for m in _STATUS_LINE_RE.finditer(body)]
    if _SHIPPED_LINE_RE.search(body):
        shipped = True
    for st in status_texts:
        low = st.lower()
        if any(k in low for k in _TERMINAL_CANCELLED):
            cancelled = True
        # Only terminal-shipped if the status WORD is terminal, not merely
        # mentioned (e.g. "not started" / "scoping" stay live).
        if any(low.startswith(k) or low == k for k in _TERMINAL_SHIPPED):
            shipped = True

    # --- effort weeks (from Complexity line) ---
    effort_weeks: Optional[float] = None
    cm = _COMPLEXITY_RE.search(body)
    if cm:
        effort_weeks = _parse_effort_weeks(cm.group(1), working_days_per_week)

    # --- dependencies (Blocks on / Depends on -> hard|soft; Companion -> soft) ---
    hard: List[str] = []
    soft: List[str] = []
    for m in _BLOCKS_ON_RE.finditer(body):
        h, s = _classify_dep_line(m.group(1), stream_id)
        hard.extend(h)
        soft.extend(s)
    for m in _COMPANION_RE.finditer(body):
        soft.extend(f"stream_{int(r.group(1)):02d}" for r in _STREAM_REF_RE.finditer(m.group(1)))
    hard_set = {d for d in hard if d != stream_id}
    soft_set = {d for d in soft if d != stream_id and d not in hard_set}

    # --- measurable impact (presence of a quantified signal) ---
    impact_measurable = bool(_IMPACT_RE.search(body))

    return {
        "stream_id": stream_id,
        "title": title,
        "shipped": shipped,
        "cancelled": cancelled,
        "effort_weeks": effort_weeks,
        "hard_deps": tuple(sorted(hard_set)),
        "soft_deps": tuple(sorted(soft_set)),
        "impact_measurable": impact_measurable,
    }


# ---------------------------------------------------------------------------
# Companion-merge — fold sub-docs sharing the parent's NN into one facts dict.
# ---------------------------------------------------------------------------

def _merge_group(group: List[Tuple[str, dict]]) -> dict:
    """Fold a parent + companion sub-docs (shared NN) into one facts dict.

    Shortest filename = parent (supplies id + title). shipped/cancelled if any
    doc says so; the shortest non-None effort wins; deps merged.
    """
    group_sorted = sorted(group, key=lambda nf: len(nf[0]))
    _parent_name, parent = group_sorted[0]
    stream_id = parent["stream_id"]
    title = parent["title"]
    shipped = any(f["shipped"] for _, f in group_sorted)
    cancelled = any(f["cancelled"] for _, f in group_sorted)
    effort = parent["effort_weeks"]
    if effort is None:
        for _, f in group_sorted[1:]:
            if f["effort_weeks"] is not None:
                effort = f["effort_weeks"]
                break
    hard: set = set()
    soft: set = set()
    impact = False
    for _, f in group_sorted:
        hard.update(f["hard_deps"])
        soft.update(f["soft_deps"])
        impact = impact or f["impact_measurable"]
    hard.discard(stream_id)
    soft = {d for d in soft if d != stream_id and d not in hard}
    return {
        "stream_id": stream_id,
        "title": title,
        "shipped": shipped,
        "cancelled": cancelled,
        "effort_weeks": effort,
        "hard_deps": tuple(sorted(hard)),
        "soft_deps": tuple(sorted(soft)),
        "impact_measurable": impact,
    }


# ---------------------------------------------------------------------------
# Decompose -> CLEAR atom map + corpus-relative atoms.
# ---------------------------------------------------------------------------

def _verify_clear(atoms: dict) -> bool:
    """Every c4_* atom declared (present) -> CLEAR.

    Folds the ambiguity-cartographer's decompose->verify->recurse guarantee
    into a pure presence check: the produced map is CLEAR iff every fuzzy
    ``c4_*`` term was reduced to a measured atom key. ``None`` is a measured
    value ("the doc didn't say"), so presence — not non-None — is the test.
    """
    return all(name in atoms for name in _C4_ATOMS)


def _decompose_atoms(facts: dict) -> dict:
    """Per-stream (non-corpus) c4_* atoms from observed facts.

    ``c4_blocked_hard`` / ``c4_unblocks_count`` are corpus-relative — filled by
    ``_build_inputs`` once every stream's deps are known. Here ``c4_unblocks_count``
    seeds to 0 (a measurement: nothing observed yet depends on this stream) and
    ``c4_blocked_hard`` seeds to False, so the per-stream map is already CLEAR.
    """
    live = not (facts["shipped"] or facts["cancelled"])
    return {
        "c4_live": live,
        "c4_shipped": facts["shipped"],
        "c4_blocked_hard": False,
        "c4_unblocks_count": 0,
        "c4_effort_weeks": facts["effort_weeks"],
        "c4_impact_measurable": facts["impact_measurable"],
        "c4_blocked_soft": bool(facts["soft_deps"]),
    }


def _build_inputs(facts_list: List[dict]) -> List[dict]:
    """Turn observed facts into rankable stream-input dicts.

    Computes the two CORPUS-RELATIVE atoms that need the whole set:
      - ``c4_unblocks_count`` — how many OTHER LIVE streams HARD-depend on this
        one (the inverse of each stream's own hard_deps). The PRIMARY structural
        signal.
      - ``c4_blocked_hard`` — True iff this stream HARD-depends on a stream that
        is NOT shipped (an open predecessor). A dep on a shipped stream does not
        block.
    """
    facts_by_id = {f["stream_id"]: f for f in facts_list}
    live_ids = {f["stream_id"] for f in facts_list if not (f["shipped"] or f["cancelled"])}
    shipped_ids = {f["stream_id"] for f in facts_list if f["shipped"]}

    unblocks: Dict[str, set] = {f["stream_id"]: set() for f in facts_list}
    for f in facts_list:
        if f["stream_id"] not in live_ids:
            continue  # a dead stream's outgoing deps don't unblock anything
        for dep in f["hard_deps"]:
            if dep in unblocks:
                unblocks[dep].add(f["stream_id"])

    inputs: List[dict] = []
    for f in facts_list:
        atoms = _decompose_atoms(f)
        atoms["c4_unblocks_count"] = len(unblocks.get(f["stream_id"], set()))
        open_hard = [d for d in f["hard_deps"] if d not in shipped_ids and d in facts_by_id]
        atoms["c4_blocked_hard"] = bool(open_hard)
        inputs.append(
            {
                "id": f["stream_id"],
                "title": f["title"],
                "atoms": atoms,
                "blocked_by": open_hard,
                "blocked_soft_by": list(f["soft_deps"]),
                "clear": _verify_clear(atoms),
            }
        )
    inputs.sort(key=lambda d: d["id"])
    return inputs


# ---------------------------------------------------------------------------
# The orchestrator decider.
# ---------------------------------------------------------------------------

def decide(state: dict, context: dict | None = None) -> dict:
    """Decompose a roadmap stream corpus into rankable c4_* atom maps.

    Args:
        state: {"streams": [{"body", "stream_id"?, "filename"?, "title"?}, ...],
                "config": {"working_days_per_week"?} | null}
        context: unused, present for orchestrator compatibility.

    Returns:
        {"output": {"streams": [...], "count": int},
         "rationale": "...",
         "self_metric": {"confidence": float, "signals": {...}}}
    """
    context = context or {}
    try:
        raw_streams = state.get("streams") or []
        config = state.get("config") or {}
        wdpw = config.get("working_days_per_week", _DEFAULT_WORKING_DAYS_PER_WEEK)
        try:
            wdpw = float(wdpw)
        except (TypeError, ValueError):
            wdpw = _DEFAULT_WORKING_DAYS_PER_WEEK
        if wdpw <= 0:
            wdpw = _DEFAULT_WORKING_DAYS_PER_WEEK

        # Observe each stream; group companions by number when a filename gives one.
        # name -> [(filename_for_sort, facts)]
        by_number: Dict[str, List[Tuple[str, dict]]] = {}
        standalone: List[dict] = []
        skipped = 0
        for entry in raw_streams:
            if not isinstance(entry, dict):
                skipped += 1
                continue
            body = entry.get("body") or ""
            filename = entry.get("filename")
            stream_id = entry.get("stream_id")
            number = None
            sort_name = filename or (stream_id or "")
            if filename:
                m = _STREAM_FILE_RE.match(filename)
                if m:
                    number = m.group(1)
                    if not stream_id:
                        stream_id = f"stream_{int(number):02d}"
            if not stream_id:
                skipped += 1
                continue
            title = entry.get("title") or _read_title(body, stream_id)
            facts = observe_stream(stream_id, title, body, wdpw)
            if number is not None:
                by_number.setdefault(number, []).append((sort_name, facts))
            else:
                standalone.append(facts)

        facts_list: List[dict] = list(standalone)
        for _number, group in by_number.items():
            facts_list.append(_merge_group(group))

        inputs = _build_inputs(facts_list)

        n = len(inputs)
        live_count = sum(1 for d in inputs if d["atoms"]["c4_live"])
        shipped_count = sum(1 for d in inputs if d["atoms"]["c4_shipped"])
        blocked_count = sum(1 for d in inputs if d["atoms"]["c4_blocked_hard"])
        with_effort = sum(1 for d in inputs if d["atoms"]["c4_effort_weeks"] is not None)
        with_impact = sum(1 for d in inputs if d["atoms"]["c4_impact_measurable"])
        all_clear = all(d["clear"] for d in inputs)

        # Confidence = data completeness: fraction with an observed effort
        # estimate (the atom most often absent). 1.0 when no streams.
        confidence = 1.0 if n == 0 else round(with_effort / n, 3)

        signals = {
            "stream_count": n,
            "live_count": live_count,
            "shipped_count": shipped_count,
            "blocked_hard_count": blocked_count,
            "with_effort_estimate": with_effort,
            "with_measurable_impact": with_impact,
            "merged_companion_groups": sum(1 for g in by_number.values() if len(g) > 1),
            "skipped_entries": skipped,
            "all_clear": all_clear,
        }

        rationale = (
            f"Decomposed {n} stream doc(s) into CLEAR c4_* atom maps "
            f"({live_count} live, {shipped_count} shipped, {blocked_count} hard-blocked). "
            f"{with_effort}/{n} stated an effort estimate. "
            f"Rules-based proposer observed only what the docs state; absent atoms "
            f"are reported (None / 0 / False), never guessed. Ready for C4 ranking."
        ) if n else "No stream docs supplied — nothing to decompose."

        return {
            "output": {"streams": inputs, "count": n},
            "rationale": rationale,
            "self_metric": {"confidence": confidence, "signals": signals},
        }

    except Exception as e:  # fail-open: never raise on bad input
        return {
            "output": {"streams": [], "count": 0},
            "rationale": f"Decision logic error (fail-open): {e}",
            "self_metric": {"confidence": 0.0, "signals": {}},
        }


def main() -> int:
    path = os.environ.get("ORGAN_INPUT")
    raw = open(path).read() if path else sys.stdin.read()
    try:
        payload = json.loads(raw)
        state = payload["state"]
    except Exception as e:
        print(json.dumps({"error": f"invalid input: {e}"}), file=sys.stderr)
        return 1
    print(json.dumps(decide(state, payload.get("context")), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
