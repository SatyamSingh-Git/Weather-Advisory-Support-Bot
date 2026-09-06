"""Tests for the parts that never call a model, so CI can run them without a key.

The suite in `suite.py` exercises the whole graph and therefore needs OPENROUTER_API_KEY. Everything
checked here is pure: the condition engine, the window arithmetic, ranking, grounding and the lint.
That is deliberately most of the logic this system is trusted for.
"""

import pytest

from app import grounding
from app.facts import POLICY_FACTS, build_facts, comfort_score, timeline
from app.sops import OPS, Sop, evaluate, lint, lint_all, load_sops, match_all, rank
from evals.fixtures import SCENARIOS


def question_facts(payload, window="now", **overrides):
    facts, _ = build_facts(payload, window)
    facts.update(activity_category=["outdoor_exercise"], audience=["general"],
                 is_outdoor_question=True, location="Testville")
    facts.update(overrides)
    return facts


# --- the condition engine -------------------------------------------------------------------

@pytest.mark.parametrize("op,actual,expected,want", [
    ("gte", 50, 50, True), ("gte", 49.9, 50, False),
    ("gt", 50, 50, False), ("lte", 5, 5, True), ("lt", 5, 5, False),
    ("between", 40, [40, 72], True), ("between", 72.1, [40, 72], False),
    ("includes_any", ["children"], ["children", "elderly"], True),
    ("includes_any", ["general"], ["children"], False),
    ("in", "evening", ["evening", "night"], True),
    ("is_true", True, True, True), ("is_true", False, True, False),
])
def test_operators(op, actual, expected, want):
    assert OPS[op](actual, expected) is want or OPS[op](actual, expected) == want


def test_missing_fact_never_matches_on_a_guess():
    """A reading we do not have must make the condition false, not raise and not be assumed."""
    sop = Sop(id="t", title="t", category="c", severity="high", guidance="x" * 50,
              when=[{"fact": "gust_kmh", "op": "gte", "value": 50}], requires_facts=["gust_kmh"])
    result = evaluate(sop, {"gust_kmh": None})
    assert result["matched"] is False
    assert result["missing_facts"] == ["gust_kmh"]


def test_any_of_needs_only_one_branch():
    sop = Sop(id="t", title="t", category="c", severity="high", guidance="x" * 50,
              when=[{"any_of": [{"fact": "a", "op": "gte", "value": 10},
                                {"fact": "b", "op": "gte", "value": 10}]}])
    assert evaluate(sop, {"a": 1, "b": 99})["matched"] is True
    assert evaluate(sop, {"a": 1, "b": 1})["matched"] is False


# --- ranking, which is the conflict policy ---------------------------------------------------

def test_override_beats_a_higher_priority_non_override():
    facts = question_facts(SCENARIOS["thunderstorm"])
    ordered = rank(match_all(facts))
    assert ordered[0].override is True
    assert len(ordered) > 1, "expected a genuine conflict in this scenario"


def test_ranking_is_a_total_order_not_a_file_listing():
    import random
    facts = question_facts(SCENARIOS["hot_for_children"], audience=["children"])
    baseline = [s.id for s in rank(match_all(facts))]
    for _ in range(8):
        pool = load_sops()[:]
        random.shuffle(pool)
        assert [s.id for s in rank(match_all(facts, pool))] == baseline


# --- window arithmetic ------------------------------------------------------------------------

@pytest.mark.parametrize("window", ["now", "morning", "afternoon", "evening", "night", "today", "tomorrow"])
def test_every_window_resolves_with_no_missing_readings(window):
    facts, provenance = build_facts(SCENARIOS["pleasant"], window)
    for key in ("temp_c", "wind_kmh", "gust_kmh", "precip_prob_pct", "comfort_score"):
        assert facts[key] is not None, f"{key} unavailable for window {window}"
    assert provenance["temp_c"]


def test_a_window_already_past_rolls_forward_rather_than_answering_about_the_past():
    from evals.fixtures import payload
    late = payload(now_hour=23)
    facts, _ = build_facts(late, "morning")
    assert facts["window_date"] > late["current"]["time"][:10]


def test_timeline_window_matches_the_window_the_facts_used():
    series = timeline(SCENARIOS["pleasant"], "evening")
    assert len(series["hours"]) == 24
    assert any(series["in_window"]), "the shaded band must cover something"


def test_comfort_score_stays_in_range_and_moves_the_right_way():
    perfect = comfort_score(24, 0, 5, 3, 50)
    grim = comfort_score(41, 95, 60, 11, 95)
    assert 0 <= grim < perfect <= 100


# --- grounding --------------------------------------------------------------------------------

def test_a_number_from_nowhere_is_rejected():
    ok, problems = grounding.check("Winds are 61.0 km/h.", {"wind_kmh": 12.0}, [], claims=[])
    assert not ok and problems


def test_a_real_number_under_the_wrong_reading_is_rejected():
    """The case provenance alone cannot catch: 38.0 is real, but it is the gust, not the wind."""
    facts = {"wind_kmh": 22.0, "gust_kmh": 38.0}
    ok, problems = grounding.check("Winds are steady at 38.0 km/h.", facts, [],
                                   claims=[{"value": 38.0, "fact": "wind_kmh"}])
    assert not ok
    assert "wind_kmh" in problems[0] and "22.0" in problems[0]


def test_a_correctly_attributed_number_passes():
    facts = {"wind_kmh": 22.0, "gust_kmh": 38.0}
    ok, problems = grounding.check("Gusts reach 38.0 km/h.", facts, [],
                                   claims=[{"value": 38.0, "fact": "gust_kmh"}])
    assert ok, problems


# --- the policy set itself ----------------------------------------------------------------------

def test_shipped_policies_pass_their_own_lint():
    assert lint_all() == {}


def test_policy_set_meets_the_shape_the_brief_asks_for():
    sops = load_sops()
    assert len(sops) >= 10
    assert len({s.category for s in sops}) >= 3
    assert len({s.severity for s in sops}) >= 3
    assert all(s.verdict.strip() for s in sops), "every policy needs a decision, not just an explanation"


def test_lint_catches_a_fact_that_does_not_exist():
    broken = Sop(id="t", title="t", category="c", severity="low", guidance="x" * 50,
                 when=[{"fact": "uv_indx", "op": "gte", "value": 3}])
    assert any("unknown fact" in p for p in lint(broken))


def test_lint_catches_a_hard_requirement_that_was_not_declared():
    broken = Sop(id="t", title="t", category="c", severity="low", guidance="x" * 50, verdict="Go",
                 when=[{"fact": "uv_index", "op": "gte", "value": 3}])
    assert any("requires_facts" in p for p in lint(broken))


def test_every_policy_condition_uses_a_known_fact():
    for sop in load_sops():
        for problem in lint(sop):
            assert "unknown fact" not in problem
    assert "comfort_score" in POLICY_FACTS
