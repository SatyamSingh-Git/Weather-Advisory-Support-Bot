"""Post-generation check that the model reported only numbers it was actually given.

The prompt asks for this; this module is what enforces it. Anything the check rejects is
replaced by a deterministic answer built from the policy text, so a hallucinated number can
never reach the user.
"""

import re

NUMBER = re.compile(r"\d+(?:\.\d+)?")
TOLERANCE = 0.5


def _numbers_in(text: str) -> list[float]:
    return [float(m) for m in NUMBER.findall(text)]


def _threshold_values(conditions) -> list[float]:
    found = []
    for node in conditions:
        if "any_of" in node or "all_of" in node:
            found += _threshold_values(node.get("any_of") or node["all_of"])
            continue
        value = node.get("value")
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, (int, float)) and not isinstance(item, bool):
                found.append(float(item))
    return found


def allowed_numbers(facts: dict, sops: list) -> set[float]:
    """Numbers the reply may contain: real API values, policy thresholds, and policy prose."""
    allowed = set()
    for value in facts.values():
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            allowed.add(float(value))
        elif isinstance(value, str):
            allowed.update(_numbers_in(value))
    for sop in sops:
        allowed.update(_threshold_values(sop.when))
        allowed.update(_numbers_in(sop.guidance))
    return allowed


def check(text: str, facts: dict, sops: list) -> tuple[bool, list[float]]:
    allowed = allowed_numbers(facts, sops)
    ungrounded = [
        n for n in _numbers_in(text)
        if not any(abs(n - a) <= TOLERANCE for a in allowed)
    ]
    return not ungrounded, ungrounded
