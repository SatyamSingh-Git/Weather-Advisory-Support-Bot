"""pytest front-end for the same cases, so the suite can run in CI: pytest evals -v"""

import pytest

from evals.suite import CASES


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_case(case):
    outcome = case.run()
    assert outcome.passed, f"{case.passes_when} -- observed: {outcome.notes}"
