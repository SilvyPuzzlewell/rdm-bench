import random
from collections import Counter

from rdmbench.generate import plan_entries
from rdmbench.state import StateSpec


def test_entries_have_exactly_the_requested_counts_and_shares():
    spec = StateSpec(records=10, community_records=4, drafts=5, submissions=3, restricted_share=0.2, files_share=0.4)
    entries = plan_entries(random.Random(1), spec)

    assert Counter(kind for kind, _, _ in entries) == {"published": 6, "community": 4, "draft": 5, "submission": 3}
    assert Counter(kind for kind, restricted, _ in entries if restricted) == {
        "published": 1,
        "community": 1,
        "draft": 1,
        "submission": 1,
    }
    assert Counter(kind for kind, _, with_files in entries if with_files) == {
        "published": 2,
        "community": 2,
        "draft": 2,
        "submission": 1,
    }
