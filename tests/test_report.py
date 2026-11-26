from dataclasses import asdict

import pytest

from rdmbench.report import summarize
from rdmbench.state import StateSpec


def result(variant, rounds, status=200):
    """A run with the allows plugin; each round is a search and a read taking the given seconds."""
    return {
        "variant": variant,
        "source": f"{variant} code",
        "state": asdict(StateSpec()),
        "requests": {"search": 1, "read": 1},
        "plugins": ["allows"],
        "rounds": [
            [
                {"label": label, "status": status, "seconds": s, "allows_seconds": s / 2, "allows_calls": 10}
                for label, s in zip(("api:search-records", "api:record-read"), seconds)
            ]
            for seconds in rounds
        ],
    }


def test_summary_compares_the_median_round_of_both_variants():
    baseline = result("baseline", [(0.5, 0.5), (1.0, 3.0), (1.0, 1.0)])
    patched = result("patched", [(0.5, 0.5), (0.25, 0.25), (1.5, 1.5)])
    summary = summarize({"baseline": [baseline], "patched": [patched]})
    lines = [line.split() for line in summary.splitlines()]
    rows = {fields[0]: fields[1:] for fields in lines if fields and fields[0] in ("all", "search", "read")}

    # requests, then baseline, patched and change of the time, allows() time and allows() calls
    assert rows == {
        "all": ["2", "2.00", "1.00", "-50.0%", "1.00", "0.50", "-50.0%", "20", "20", "+0.0%"],
        "search": ["1", "1.00", "0.50", "-50.0%", "0.50", "0.25", "-50.0%", "10", "10", "+0.0%"],
        "read": ["1", "1.00", "0.50", "-50.0%", "0.50", "0.25", "-50.0%", "10", "10", "+0.0%"],
    }


@pytest.mark.parametrize(
    "patched_status, verdict",
    [
        (200, "status codes identical across all runs and variants (200: 2 in round 1)"),
        (500, "  patched   api:search-records             expected 200 got 500  x1"),
    ],
)
def test_summary_checks_that_both_variants_answer_alike(patched_status, verdict):
    baseline = result("baseline", [(1.0, 1.0)])
    patched = result("patched", [(1.0, 1.0)], patched_status)

    assert verdict in summarize({"baseline": [baseline], "patched": [patched]}).splitlines()
