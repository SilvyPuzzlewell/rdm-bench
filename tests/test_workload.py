import random

import pytest

from rdmbench.workload import Endpoint, Pool, Workload, apportion

WORKLOAD = Workload.load()
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


def test_default_workload_splits_300_requests_over_all_endpoints_by_weight():
    expected = apportion(300, {e.label: e.weight for e in WORKLOAD.endpoints})

    assert WORKLOAD.default_requests() == {"search": 115, "read": 161, "create": 8, "update": 8, "delete": 8}
    assert WORKLOAD.plan_round(WORKLOAD.default_requests(), Pool(STATE)) == expected


def test_a_workload_file_sets_the_endpoints_and_the_requests_per_round(tmp_path):
    path = tmp_path / "workload.toml"
    path.write_text(
        "requests_per_round = 4\n"
        "endpoints = [\n"
        '  { label = "frontpage", kind = "read", weight = 3, actor = "anonymous", url = "/" },\n'
        '  { label = "search", kind = "search", weight = 1, actor = "user", url = "/search?q={word}" },\n'
        "]\n"
    )
    workload = Workload.load(path)

    assert workload.endpoints == [
        Endpoint("frontpage", "read", 3, "anonymous", "/"),
        Endpoint("search", "search", 1, "user", "/search?q={word}"),
    ]
    assert workload.default_requests() == {"search": 1, "read": 3, "create": 0, "update": 0, "delete": 0}


@pytest.mark.parametrize(
    "endpoints, problem",
    [
        ('{ label = "x", kind = "browse", weight = 1, actor = "user", url = "/" }', "x: kind must be one of search"),
        ('{ label = "x", kind = "read", weight = 1, actor = "curater", url = "/" }', "x: actor must be one of anon"),
        ('{ label = "x", kind = "read", weight = 1, actor = "user", url = "/", pool = "record" }', "x: pool must be"),
        ('{ label = "x", kind = "read", weight = 0, actor = "user", url = "/" }', "x: weight must be a whole number"),
        ('{ label = "x", kind = "read", wieght = 1, actor = "user", url = "/" }', "x: .* keyword argument 'wieght'"),
        (
            (
                '{ label = "x", kind = "read", weight = 1, actor = "user", url = "/" }, '
                '{ label = "x", kind = "search", weight = 1, actor = "user", url = "/search" }'
            ),
            "x: 2 endpoints have this label",
        ),
    ],
)
def test_invalid_workloads_are_rejected(tmp_path, endpoints, problem):
    path = tmp_path / "workload.toml"
    path.write_text(f"requests_per_round = 10\nendpoints = [{endpoints}]\n")

    with pytest.raises(SystemExit, match=problem):
        Workload.load(path)


def test_endpoints_the_database_cannot_serve_are_skipped_with_a_warning(capsys):
    plan = WORKLOAD.plan_round({"read": 56}, Pool({**STATE, "drafts": []}))

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
    steps = WORKLOAD.build_round(Pool(STATE), {label: 20}, random.Random(0))

    assert {s.actor for s in steps} <= actors
    assert {s.url for s in steps} == {url}


def test_the_same_seed_gives_the_same_round():
    pool = Pool(STATE)
    plan = WORKLOAD.plan_round(WORKLOAD.default_requests(), pool)

    assert WORKLOAD.build_round(pool, plan, random.Random(1)) == WORKLOAD.build_round(pool, plan, random.Random(1))
    assert WORKLOAD.build_round(pool, plan, random.Random(1)) != WORKLOAD.build_round(pool, plan, random.Random(2))


def test_updates_of_one_actor_share_a_draft_and_every_delete_has_its_own():
    steps = WORKLOAD.build_round(Pool(STATE), {"api:draft-update": 10, "api:draft-delete": 5}, random.Random(0))
    updates = [s for s in steps if s.label == "api:draft-update"]
    deletes = [s for s in steps if s.label == "api:draft-delete"]

    for update in updates:
        assert {s.draft for s in updates if s.actor == update.actor} == {update.draft}
    assert len({s.draft for s in updates}) == len({s.actor for s in updates})
    assert len({s.draft for s in deletes}) == 5
