"""Post-generation check that the model reported only numbers it was given, as what they are.

The composer prompt asks for this; this module is what enforces it. Anything the check rejects is
replaced by a deterministic answer, so an ungrounded number cannot reach the user.

Two layers, because they catch different lies:

  * every number in the reply must trace to a real reading, a policy threshold or policy prose;
  * every number the model attributes to a reading must actually equal that reading. The first
    layer alone passes a reply that quotes today's rainfall total and calls it a 24-hour figure,
    since both numbers are real.
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


def _numeric(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def background_numbers(facts: dict, sops: list) -> set[float]:
    """Numbers a reply may contain without attributing them to a reading.

    Policy thresholds and anything written into the guidance text ("SPF 30", "30 minutes"), plus
    numbers inside string facts such as the observation timestamp.
    """
    allowed = set()
    for value in facts.values():
        if isinstance(value, str):
            allowed.update(_numbers_in(value))
    for sop in sops:
        allowed.update(_threshold_values(sop.when))
        allowed.update(_numbers_in(sop.guidance))
    return allowed


def check(text: str, facts: dict, sops: list, claims: list | None = None) -> tuple[bool, list[str]]:
    """Claims are the model's own {value, fact} attributions. None means only the first layer runs."""
    problems = []
    attributed = set()

    for claim in claims or []:
        fact = claim.get("fact") if isinstance(claim, dict) else None
        value = _numeric(claim.get("value")) if isinstance(claim, dict) else None
        if fact is None or value is None:
            problems.append(f"malformed attribution {claim!r}")
            continue
        actual = _numeric(facts.get(fact))
        if actual is None:
            problems.append(f"reply attributes {value} to {fact!r}, which is not a reading we hold")
        elif abs(value - actual) > TOLERANCE:
            problems.append(f"reply reports {fact} as {value}, but the API returned {actual}")
        else:
            attributed.add(value)

    if claims is None:
        attributed = {v for value in facts.values() if (v := _numeric(value)) is not None}

    allowed = attributed | background_numbers(facts, sops)
    for number in _numbers_in(text):
        if not any(abs(number - a) <= TOLERANCE for a in allowed):
            problems.append(f"{number} appears in the reply but traces to no reading or policy")

    return not problems, problems
