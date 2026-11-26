import argparse

import pytest

from rdmbench.state import StateSpec


def test_default_database_is_valid():
    StateSpec().check()


@pytest.mark.parametrize(
    "changes, problem",
    [
        ({"drafts": -1}, "counts cannot be negative"),
        ({"files_share": 1.5}, "shares must be within 0..1"),
        ({"community_records": 301}, "--community-records cannot exceed --records"),
        ({"communities": 0}, "community records, submissions and inclusions need a public community"),
        ({"curators": 0}, "communities need at least one curator"),
        ({"users": 5}, "--users must exceed --curators plus --readers"),
        ({"inclusions": 129}, "--inclusions cannot exceed the 128 public records outside communities"),
    ],
)
def test_impossible_databases_are_rejected(changes, problem):
    with pytest.raises(SystemExit, match=problem):
        StateSpec(**changes).check()


def test_setup_options_set_the_database_parameters():
    parser = argparse.ArgumentParser()
    StateSpec.add_arguments(parser)
    args = parser.parse_args(["--records", "10", "--community-records", "4", "--restricted-share", "0.5"])

    assert StateSpec.from_args(args) == StateSpec(records=10, community_records=4, restricted_share=0.5)
