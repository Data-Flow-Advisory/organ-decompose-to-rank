"""
Test suite for the Decompose-to-Rank organ.

Covers:
- observe_stream: status/shipped/cancelled, effort parsing, dependency hard/soft
  classification, measurable-impact detection
- companion-merge (sub-docs sharing the NN prefix)
- corpus-relative atoms (c4_unblocks_count, c4_blocked_hard)
- CLEAR verification (all c4_* atoms declared)
- self_metric confidence + signals
- fail-open behaviour on malformed input
"""

import json

import pytest

from organ import (
    decide,
    observe_stream,
    _parse_effort_weeks,
    _classify_dep_line,
    _verify_clear,
    _C4_ATOMS,
    _DEFAULT_WORKING_DAYS_PER_WEEK,
)


# ---------------------------------------------------------------------------
# observe_stream — status / shipped / cancelled
# ---------------------------------------------------------------------------

class TestStatusDetection:
    def test_shipped_marker_line(self):
        body = "# Stream 10\n**Shipped**: 2026-05-29 in PR #123\n"
        f = observe_stream("stream_10", "Stream 10", body)
        assert f["shipped"] is True
        assert f["cancelled"] is False

    def test_status_terminal_shipped_word(self):
        body = "**Status.** Shipped. Everything live.\n"
        f = observe_stream("stream_11", "t", body)
        assert f["shipped"] is True

    def test_status_scoping_stays_live(self):
        body = "**Status:** Scoping. Not started yet.\n"
        f = observe_stream("stream_12", "t", body)
        assert f["shipped"] is False
        assert f["cancelled"] is False

    def test_status_cancelled(self):
        body = "**Status:** Superseded by Stream 40.\n"
        f = observe_stream("stream_13", "t", body)
        assert f["cancelled"] is True

    def test_not_started_not_shipped_despite_word(self):
        # "not started" must NOT be read as shipped even though it contains no
        # terminal word; guards the startswith/== gate.
        body = "**Status:** not started\n"
        f = observe_stream("stream_14", "t", body)
        assert f["shipped"] is False


# ---------------------------------------------------------------------------
# effort parsing
# ---------------------------------------------------------------------------

class TestEffortParsing:
    def test_week_range_upper_bound(self):
        assert _parse_effort_weeks("medium (2-3 weeks)", 5.0) == 3.0

    def test_single_week(self):
        assert _parse_effort_weeks("1 week", 5.0) == 1.0

    def test_day_range_converts_to_weeks(self):
        # 3-5 days -> upper 5 / 5 = 1.0 week
        assert _parse_effort_weeks("medium (3-5 days)", 5.0) == 1.0

    def test_single_day(self):
        assert _parse_effort_weeks("small (2 days)", 5.0) == 0.4

    def test_hours_is_none(self):
        assert _parse_effort_weeks("small (hours)", 5.0) is None

    def test_absent_is_none(self):
        assert _parse_effort_weeks("medium", 5.0) is None

    def test_weeks_preferred_over_days(self):
        assert _parse_effort_weeks("2 weeks, ~10 days", 5.0) == 2.0

    def test_custom_working_days_per_week(self):
        # 6-day week -> 6 days = 1.0 week
        assert _parse_effort_weeks("(6 days)", 6.0) == 1.0

    def test_observe_picks_up_complexity_line(self):
        body = "**Complexity:** medium (2-4 weeks)\n"
        f = observe_stream("stream_20", "t", body)
        assert f["effort_weeks"] == 4.0


# ---------------------------------------------------------------------------
# dependency classification
# ---------------------------------------------------------------------------

class TestDependencyClassification:
    def test_bare_blocks_on_is_hard(self):
        hard, soft = _classify_dep_line("Stream 00 must exist", "stream_05")
        assert hard == ["stream_00"]
        assert soft == []

    def test_soft_marker_makes_soft(self):
        hard, soft = _classify_dep_line("benefits from Stream 14", "stream_05")
        assert hard == []
        assert soft == ["stream_14"]

    def test_hard_marker_wins_over_soft(self):
        # both markers present -> hard wins
        hard, soft = _classify_dep_line("benefits from but must land first Stream 14", "stream_05")
        assert hard == ["stream_14"]
        assert soft == []

    def test_no_stream_ref_yields_nothing(self):
        hard, soft = _classify_dep_line("none", "stream_05")
        assert hard == []
        assert soft == []

    def test_observe_blocks_on_line_hard(self):
        body = "**Blocks on:** Stream 00 — the platform skeleton.\n"
        f = observe_stream("stream_30", "t", body)
        assert f["hard_deps"] == ("stream_00",)
        assert f["soft_deps"] == ()

    def test_observe_depends_on_soft(self):
        body = "**Depends on:** benefits from Stream 12 for the cleanest path.\n"
        f = observe_stream("stream_31", "t", body)
        assert f["soft_deps"] == ("stream_12",)
        assert f["hard_deps"] == ()

    def test_companion_line_is_soft(self):
        body = "**Companion to:** Stream 07.\n"
        f = observe_stream("stream_32", "t", body)
        assert f["soft_deps"] == ("stream_07",)

    def test_self_reference_dropped(self):
        body = "**Blocks on:** Stream 33 and Stream 01.\n"
        f = observe_stream("stream_33", "t", body)
        assert "stream_33" not in f["hard_deps"]
        assert f["hard_deps"] == ("stream_01",)

    def test_soft_demoted_when_also_hard(self):
        body = ("**Blocks on:** Stream 02 must land first.\n"
                "**Companion to:** Stream 02.\n")
        f = observe_stream("stream_34", "t", body)
        assert f["hard_deps"] == ("stream_02",)
        assert f["soft_deps"] == ()  # stream_02 removed from soft (already hard)


# ---------------------------------------------------------------------------
# measurable impact
# ---------------------------------------------------------------------------

class TestMeasurableImpact:
    @pytest.mark.parametrize("text", [
        "first £50,000 of revenue",
        "$10,000 ARR",
        "blocks all revenue",
        "affects 5 tenants",
        "12 users blocked",
        "MRR uplift",
    ])
    def test_impact_present(self, text):
        f = observe_stream("stream_40", "t", f"Some prose. {text}. More prose.")
        assert f["impact_measurable"] is True

    def test_impact_absent(self):
        f = observe_stream("stream_41", "t", "A nice feature with no numbers.")
        assert f["impact_measurable"] is False


# ---------------------------------------------------------------------------
# CLEAR verification
# ---------------------------------------------------------------------------

class TestVerifyClear:
    def test_full_atom_set_is_clear(self):
        atoms = {name: None for name in _C4_ATOMS}
        assert _verify_clear(atoms) is True

    def test_missing_atom_not_clear(self):
        atoms = {name: None for name in _C4_ATOMS if name != "c4_effort_weeks"}
        assert _verify_clear(atoms) is False

    def test_none_value_still_clear(self):
        # None is a measured value ("doc didn't say"), presence is the test.
        atoms = {name: None for name in _C4_ATOMS}
        atoms["c4_effort_weeks"] = None
        assert _verify_clear(atoms) is True


# ---------------------------------------------------------------------------
# decide — corpus-relative atoms + full output shape
# ---------------------------------------------------------------------------

class TestDecideCorpusRelative:
    def _streams(self):
        return {
            "streams": [
                {"stream_id": "stream_00", "title": "Platform skeleton",
                 "body": "# Platform skeleton\n**Status:** Shipped.\n"},
                {"stream_id": "stream_05", "title": "Feature A",
                 "body": "# Feature A\n**Blocks on:** Stream 00 — needs the skeleton.\n"
                         "**Complexity:** medium (2-3 weeks)\nfirst £50,000 revenue\n"},
                {"stream_id": "stream_06", "title": "Feature B",
                 "body": "# Feature B\n**Blocks on:** Stream 05 must land first.\n"},
            ]
        }

    def test_output_shape(self):
        r = decide(self._streams())
        assert set(r.keys()) == {"output", "rationale", "self_metric"}
        assert r["output"]["count"] == 3
        assert len(r["output"]["streams"]) == 3
        for s in r["output"]["streams"]:
            assert set(s["atoms"].keys()) == set(_C4_ATOMS)
            assert s["clear"] is True

    def test_shipped_predecessor_does_not_block(self):
        r = decide(self._streams())
        by_id = {s["id"]: s for s in r["output"]["streams"]}
        # stream_05 hard-depends on stream_00 which is SHIPPED -> not blocked
        assert by_id["stream_05"]["atoms"]["c4_blocked_hard"] is False
        assert by_id["stream_05"]["blocked_by"] == []

    def test_open_predecessor_blocks(self):
        r = decide(self._streams())
        by_id = {s["id"]: s for s in r["output"]["streams"]}
        # stream_06 hard-depends on stream_05 which is LIVE (open) -> blocked
        assert by_id["stream_06"]["atoms"]["c4_blocked_hard"] is True
        assert by_id["stream_06"]["blocked_by"] == ["stream_05"]

    def test_unblocks_count_inverse_dep(self):
        r = decide(self._streams())
        by_id = {s["id"]: s for s in r["output"]["streams"]}
        # stream_05 is hard-depended-on by stream_06 (live) -> unblocks 1
        assert by_id["stream_05"]["atoms"]["c4_unblocks_count"] == 1
        # stream_00 is shipped; stream_05 hard-deps it but unblocks counts the
        # number of LIVE streams that hard-depend on stream_00 = just stream_05
        assert by_id["stream_00"]["atoms"]["c4_unblocks_count"] == 1
        # stream_06 has nothing depending on it
        assert by_id["stream_06"]["atoms"]["c4_unblocks_count"] == 0

    def test_dead_stream_outgoing_dep_does_not_unblock(self):
        # a shipped stream's hard_deps should not contribute to unblocks counts
        state = {
            "streams": [
                {"stream_id": "stream_01", "title": "base", "body": "# base\nplain\n"},
                {"stream_id": "stream_02", "title": "done dependent",
                 "body": "# d\n**Status:** Shipped.\n**Blocks on:** Stream 01.\n"},
            ]
        }
        r = decide(state)
        by_id = {s["id"]: s for s in r["output"]["streams"]}
        # stream_02 is shipped, so its dep on stream_01 doesn't unblock stream_01
        assert by_id["stream_01"]["atoms"]["c4_unblocks_count"] == 0

    def test_live_and_shipped_flags(self):
        r = decide(self._streams())
        by_id = {s["id"]: s for s in r["output"]["streams"]}
        assert by_id["stream_00"]["atoms"]["c4_shipped"] is True
        assert by_id["stream_00"]["atoms"]["c4_live"] is False
        assert by_id["stream_05"]["atoms"]["c4_live"] is True


# ---------------------------------------------------------------------------
# decide — filename-derived ids + companion merge
# ---------------------------------------------------------------------------

class TestFilenameAndMerge:
    def test_id_derived_from_filename(self):
        state = {"streams": [{"filename": "36-native-pa-app.md", "body": "# PA app\nplain\n"}]}
        r = decide(state)
        assert r["output"]["streams"][0]["id"] == "stream_36"

    def test_companion_merge_folds_subdoc(self):
        state = {
            "streams": [
                {"filename": "10-base.md", "body": "# base\n**Status:** Scoping.\n"},
                {"filename": "36-native-pa-app.md",
                 "body": "# PA app\n**Status:** Scoping.\n"},
                {"filename": "36-native-pa-app-alex.md",
                 "body": "# PA app addendum\n**Blocks on:** Stream 10 must land first.\n"
                         "**Complexity:** (3 weeks)\n"},
            ]
        }
        r = decide(state)
        # the two stream_36 docs merge into ONE; stream_10 stays separate
        assert r["output"]["count"] == 2
        by_id = {s["id"]: s for s in r["output"]["streams"]}
        s = by_id["stream_36"]
        # parent (shortest filename) supplies the title
        assert s["title"] == "PA app"
        # dep + effort merged from the sub-doc; stream_10 is in-corpus + open
        assert s["atoms"]["c4_effort_weeks"] == 3.0
        assert "stream_10" in s["blocked_by"]
        assert r["self_metric"]["signals"]["merged_companion_groups"] == 1

    def test_blocked_by_excludes_out_of_corpus_dep(self):
        # source-faithful: a hard dep on a stream NOT in the corpus is not
        # counted as a blocker (can't reason about a stream we never saw).
        state = {
            "streams": [
                {"filename": "36-x.md",
                 "body": "# x\n**Blocks on:** Stream 99 must land first.\n"},
            ]
        }
        r = decide(state)
        s = r["output"]["streams"][0]
        assert s["blocked_by"] == []
        assert s["atoms"]["c4_blocked_hard"] is False

    def test_shipped_in_any_subdoc_wins(self):
        state = {
            "streams": [
                {"filename": "40-thing.md", "body": "# Thing\n**Status:** Scoping.\n"},
                {"filename": "40-thing-extra.md", "body": "# extra\n**Shipped**: today\n"},
            ]
        }
        r = decide(state)
        assert r["output"]["streams"][0]["atoms"]["c4_shipped"] is True


# ---------------------------------------------------------------------------
# decide — self_metric + edge cases
# ---------------------------------------------------------------------------

class TestSelfMetricAndEdges:
    def test_confidence_is_effort_completeness(self):
        state = {
            "streams": [
                {"stream_id": "stream_01", "title": "a",
                 "body": "# a\n**Complexity:** (1 week)\n"},
                {"stream_id": "stream_02", "title": "b", "body": "# b\nno effort\n"},
            ]
        }
        r = decide(state)
        assert r["self_metric"]["confidence"] == 0.5
        assert r["self_metric"]["signals"]["with_effort_estimate"] == 1
        assert r["self_metric"]["signals"]["stream_count"] == 2

    def test_empty_streams(self):
        r = decide({"streams": []})
        assert r["output"]["count"] == 0
        assert r["output"]["streams"] == []
        assert r["self_metric"]["confidence"] == 1.0

    def test_missing_streams_key(self):
        r = decide({})
        assert r["output"]["count"] == 0

    def test_entry_without_id_or_filename_skipped(self):
        state = {"streams": [{"body": "# no id\nplain\n"}, {"stream_id": "stream_09", "body": "# ok\nx\n"}]}
        r = decide(state)
        assert r["output"]["count"] == 1
        assert r["self_metric"]["signals"]["skipped_entries"] == 1

    def test_non_dict_entry_skipped(self):
        state = {"streams": ["garbage", {"stream_id": "stream_03", "body": "# ok\nx\n"}]}
        r = decide(state)
        assert r["output"]["count"] == 1
        assert r["self_metric"]["signals"]["skipped_entries"] == 1

    def test_title_from_h1_when_absent(self):
        state = {"streams": [{"stream_id": "stream_08", "body": "# The Real Title\nbody\n"}]}
        r = decide(state)
        assert r["output"]["streams"][0]["title"] == "The Real Title"

    def test_fail_open_on_bad_state(self):
        # passing a non-dict-shaped streams entry value that breaks iteration
        r = decide({"streams": 12345})
        # 12345 is truthy but not a list; `or []` keeps it, iteration over int
        # raises -> fail-open
        assert r["output"]["count"] == 0
        assert r["self_metric"]["confidence"] == 0.0
        assert "error" in r["rationale"].lower()

    def test_config_working_days_override(self):
        state = {
            "streams": [{"stream_id": "stream_01", "body": "# a\n**Complexity:** (6 days)\n"}],
            "config": {"working_days_per_week": 6.0},
        }
        r = decide(state)
        assert r["output"]["streams"][0]["atoms"]["c4_effort_weeks"] == 1.0

    def test_bad_config_falls_back_to_default(self):
        state = {
            "streams": [{"stream_id": "stream_01", "body": "# a\n**Complexity:** (5 days)\n"}],
            "config": {"working_days_per_week": "nonsense"},
        }
        r = decide(state)
        # falls back to default 5 -> 5 days = 1.0 week
        assert r["output"]["streams"][0]["atoms"]["c4_effort_weeks"] == 1.0
        assert _DEFAULT_WORKING_DAYS_PER_WEEK == 5.0

    def test_context_arg_ignored(self):
        r = decide({"streams": []}, context={"anything": 1})
        assert r["output"]["count"] == 0

    def test_output_json_serialisable(self):
        r = decide({"streams": [{"stream_id": "stream_01", "body": "# a\nx\n"}]})
        json.dumps(r)  # must not raise


# ---------------------------------------------------------------------------
# samples shadow-run (the committed samples must produce CLEAR output)
# ---------------------------------------------------------------------------

class TestSamples:
    def test_all_samples_run_clean(self):
        import glob
        import os
        here = os.path.dirname(os.path.abspath(__file__))
        for path in sorted(glob.glob(os.path.join(here, "samples", "*.json"))):
            payload = json.loads(open(path).read())
            r = decide(payload["state"], payload.get("context"))
            assert "output" in r and "self_metric" in r
            for s in r["output"]["streams"]:
                assert s["clear"] is True, f"{path}: {s['id']} not CLEAR"
