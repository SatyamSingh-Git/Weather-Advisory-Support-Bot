"""Policy loading and matching.

SOPs are YAML files, one per policy, in sops/. Conditions are declarative, so adding or
changing a policy never touches this file, the weather client, or the graph.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

SOP_DIR = Path(os.getenv("SOP_DIR", Path(__file__).resolve().parent.parent / "sops"))
SEVERITY_ORDER = ["info", "low", "moderate", "high", "critical"]


class SopError(ValueError):
    """A policy file is malformed. Surfaced to whoever edited it, never swallowed."""


@dataclass
class Sop:
    id: str
    title: str
    category: str
    severity: str
    guidance: str
    when: list
    # The decision itself, in the policy author's words. The model may phrase the explanation but
    # never the call, so this is data like every other part of a rule.
    verdict: str = ""
    requires_facts: list = field(default_factory=list)
    priority: int = 0
    override: bool = False
    source_file: str = ""

    @property
    def severity_rank(self) -> int:
        return SEVERITY_ORDER.index(self.severity)


OPS = {
    "gte": lambda a, b: a >= b,
    "gt": lambda a, b: a > b,
    "lte": lambda a, b: a <= b,
    "lt": lambda a, b: a < b,
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "in": lambda a, b: a in b,
    "includes_any": lambda a, b: bool(set(a) & set(b)),
    "between": lambda a, b: b[0] <= a <= b[1],
    "is_true": lambda a, b: bool(a) is bool(b),
}

REQUIRED_KEYS = {"id", "title", "category", "severity", "guidance", "when"}


def _validate(raw: dict, path: Path) -> Sop:
    missing = REQUIRED_KEYS - raw.keys()
    if missing:
        raise SopError(f"{path.name}: missing {sorted(missing)}")
    if raw["severity"] not in SEVERITY_ORDER:
        raise SopError(f"{path.name}: severity must be one of {SEVERITY_ORDER}")
    known = {f.name for f in Sop.__dataclass_fields__.values()} - {"source_file"}
    unknown = raw.keys() - known
    if unknown:
        raise SopError(f"{path.name}: unknown keys {sorted(unknown)}")
    sop = Sop(**raw, source_file=path.name)
    _check_conditions(sop.when, path)
    return sop


def _check_conditions(conditions, path):
    for node in conditions:
        if "any_of" in node or "all_of" in node:
            _check_conditions(node.get("any_of") or node["all_of"], path)
        elif node.get("op") not in OPS:
            raise SopError(f"{path.name}: unknown op {node.get('op')!r}, expected one of {sorted(OPS)}")


_cache: dict = {"stamp": None, "sops": []}


def load_sops(force: bool = False) -> list[Sop]:
    """Reload from disk whenever a policy file changes, so a new SOP is live on the next message."""
    files = sorted(SOP_DIR.glob("*.yaml"))
    stamp = tuple((f.name, f.stat().st_mtime) for f in files)
    if not force and stamp == _cache["stamp"]:
        return _cache["sops"]
    sops = [_validate(yaml.safe_load(f.read_text(encoding="utf-8")), f) for f in files]
    ids = [s.id for s in sops]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise SopError(f"duplicate SOP ids: {sorted(duplicates)}")
    _cache.update(stamp=stamp, sops=sops)
    return sops


def _evaluate_node(node: dict, facts: dict) -> dict:
    if "any_of" in node:
        children = [_evaluate_node(c, facts) for c in node["any_of"]]
        return {"kind": "any_of", "children": children, "ok": any(c["ok"] for c in children)}
    if "all_of" in node:
        children = [_evaluate_node(c, facts) for c in node["all_of"]]
        return {"kind": "all_of", "children": children, "ok": all(c["ok"] for c in children)}

    name, op, expected = node["fact"], node["op"], node["value"]
    actual = facts.get(name)
    if actual is None:
        return {"kind": "leaf", "fact": name, "op": op, "expected": expected, "actual": None, "ok": False,
                "note": "fact unavailable"}
    try:
        ok = OPS[op](actual, expected)
    except TypeError:
        ok = False
    return {"kind": "leaf", "fact": name, "op": op, "expected": expected, "actual": actual, "ok": ok}


def evaluate(sop: Sop, facts: dict) -> dict:
    """Evaluate one SOP against the fact table, keeping the per-condition result for the audit trail."""
    missing = [f for f in sop.requires_facts if facts.get(f) is None]
    conditions = [_evaluate_node(node, facts) for node in sop.when]
    matched = not missing and all(c["ok"] for c in conditions)
    return {"sop": sop, "matched": matched, "conditions": conditions, "missing_facts": missing}


def match_all(facts: dict, sops: list[Sop] | None = None) -> list[dict]:
    return [evaluate(sop, facts) for sop in (sops if sops is not None else load_sops())]


def rank(results: list[dict]) -> list[Sop]:
    """Order the SOPs that matched.

    Conflict policy, decided deliberately: an SOP flagged `override: true` always leads, because
    a situational risk (an active rain system) can outrank a threshold that looks calm in isolation.
    Otherwise: severity, then explicit priority, then specificity (more conditions = more specific).
    """
    matched = [r["sop"] for r in results if r["matched"]]
    return sorted(
        matched,
        key=lambda s: (s.override, s.severity_rank, s.priority, len(s.when)),
        reverse=True,
    )


LIST_OPS = {"in", "includes_any"}


def _walk(nodes, anded=True):
    """Yield (leaf, is_hard_requirement). A leaf under any_of is optional; an ANDed leaf is not."""
    for node in nodes:
        if "any_of" in node:
            yield from _walk(node["any_of"], False)
        elif "all_of" in node:
            yield from _walk(node["all_of"], anded)
        else:
            yield node, anded


def lint(sop: Sop) -> list[str]:
    """Problems a policy author would otherwise only discover as a rule that never fires.

    A missing fact makes a condition false rather than unevaluable, so a typo, or an ANDed fact the
    API sometimes omits, silently removes the policy from the rule set instead of raising.
    """
    from .facts import POLICY_FACTS, QUESTION_FACTS

    problems = []
    for leaf, hard in _walk(sop.when):
        fact, op, value = leaf["fact"], leaf["op"], leaf["value"]
        if fact not in POLICY_FACTS:
            problems.append(f"unknown fact {fact!r}; a condition on it can never be true")
            continue
        if hard and fact not in sop.requires_facts and fact not in QUESTION_FACTS:
            problems.append(
                f"{fact!r} is required for this policy to match but is not in requires_facts, so a "
                f"missing reading would silently stop it firing instead of reporting it"
            )
        if op == "between" and not (isinstance(value, list) and len(value) == 2):
            problems.append(f"'between' on {fact!r} needs a two-item [low, high] list")
        if op in LIST_OPS and not isinstance(value, list):
            problems.append(f"{op!r} on {fact!r} needs a list of values")
    for fact in sop.requires_facts:
        if fact not in POLICY_FACTS:
            problems.append(f"requires_facts names unknown fact {fact!r}")
    if len(sop.guidance.strip()) < 40:
        problems.append("guidance is too short to be actionable advice")
    if not sop.verdict.strip():
        problems.append("no verdict, so the user gets an explanation with no decision attached")
    elif len(sop.verdict) > 40:
        problems.append("verdict should be a short decision, not a sentence")
    return problems


def lint_all(sops: list[Sop] | None = None) -> dict[str, list[str]]:
    return {s.id: problems for s in (sops if sops is not None else load_sops()) if (problems := lint(s))}
