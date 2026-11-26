import random

import pytest

from rdmbench.workload import DEFAULT_REQUESTS, ENDPOINTS, Pool, apportion, build_round, plan_round

ADMIN = "admin@example.org"
# One item for every pool: rec1 is public with a file, rec2 restricted in community com1.
STATE = {
    "admin": {"email": ADMIN},
    "users": [{"email": email} for email in ("a@example.org", "b@example.org", "c@example.org", "d@example.org")],
    "communities": [{"id": "com1", "slug": "com-1", "visibility": "public", "curators": ["c@example.org"]}],
    "records": [
        {"id": "rec1", "owner": "a@example.org", "restricted": False, "files": True, "community": None},
        {"id": "rec2", "owner": "a@example.org", "restricted": True, "files": False, "community": "com1"},
    ],
    "drafts": [{"id": "draft1", "owner": "b@example.org"}],
    "requests": [{"id": "req1", "community": "com1"}],
}


@pytest.mark.parametrize(
    "total, weights, expected",
    [
        (6, {"a": 2, "b": 1}, {"a": 4, "b": 2}),
        (5, {"a": 3, "b": 1}, {"a": 4, "b": 1}),
        (10, {"a": 1, "b": 1, "c": 1}, {"a": 4, "b": 3, "c": 3}),
    ],
)
def test_apportion_splits_by_weight_and_gives_leftovers_to_the_largest_remainders(total, weights, expected):
    assert apportion(total, weights) == expected


def test_default_requests_are_300_requests_split_over_all_endpoints_by_weight():
    expected = apportion(300, {e.label: e.weight for e in ENDPOINTS})
    assert plan_round(DEFAULT_REQUESTS, Pool(STATE)) == expected


def test_endpoints_the_database_cannot_serve_are_skipped_with_a_warning(capsys):
    plan = plan_round({"read": 56}, Pool({**STATE, "drafts": []}))

    assert "api:draft-read" not in plan
    assert "ui:deposit-edit" not in plan
    assert sum(plan.values()) == 56
    assert "skipping api:draft-read: the benchmark database has no drafts" in capsys.readouterr().err


@pytest.mark.parametrize(
    "label, actors, url",
    [
        ("ui:record-landing", {"anonymous"}, "/records/rec1"),
        ("ui:record-landing-owner", {"a@example.org"}, "/records/rec1"),
        ("ui:record-landing-other", {"b@example.org", "c@example.org", "d@example.org"}, "/records/rec1"),
        ("api:draft-read", {"b@example.org"}, "/api/records/draft1/draft"),
        ("api:user-records", {"a@example.org", "b@example.org"}, "/api/user/records?q=&sort=updated-desc"),
        ("ui:community-records", {"anonymous"}, "/communities/com-1/records"),
        ("api:community-requests", {"c@example.org"}, "/api/communities/com1/requests?is_open=true"),
        ("ui:request-detail", {"c@example.org"}, "/communities/com-1/requests/req1"),
        ("ui:record-landing-curator", {"c@example.org"}, "/records/rec2"),
        ("api:record-read-admin", {ADMIN}, "/api/records/rec2"),
    ],
)
def test_requests_are_sent_by_the_right_actor_to_the_right_url(label, actors, url):
    steps = build_round(Pool(STATE), {label: 20}, random.Random(0))

    assert {s.actor for s in steps} <= actors
    assert {s.url for s in steps} == {url}


def test_the_same_seed_gives_the_same_round():
    pool = Pool(STATE)
    plan = plan_round(DEFAULT_REQUESTS, pool)

    assert build_round(pool, plan, random.Random(1)) == build_round(pool, plan, random.Random(1))
    assert build_round(pool, plan, random.Random(1)) != build_round(pool, plan, random.Random(2))


def test_updates_of_one_actor_share_a_draft_and_every_delete_has_its_own():
    steps = build_round(Pool(STATE), {"api:draft-update": 10, "api:draft-delete": 5}, random.Random(0))
    updates = [s for s in steps if s.label == "api:draft-update"]
    deletes = [s for s in steps if s.label == "api:draft-delete"]

    for update in updates:
        assert {s.draft for s in updates if s.actor == update.actor} == {update.draft}
    assert len({s.draft for s in updates}) == len({s.actor for s in updates})
    assert len({s.draft for s in deletes}) == 5
