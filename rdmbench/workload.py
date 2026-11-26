"""The requests of a benchmark round: the endpoint catalog and how rounds are drawn from it."""

import argparse
import sys
from collections import Counter
from dataclasses import dataclass

from rdmbench.state import WORDS, record_data

ANONYMOUS = "anonymous"
KINDS = ("search", "read", "create", "update", "delete")
DEFAULT_REQUESTS = [("mix", 300)]
POOLS = {
    "public": "public records",
    "restricted": "restricted records",
    "with_files": "public records with files",
    "curated": "restricted records in a community",
    "drafts": "drafts",
    "requests": "open requests",
    "communities": "public communities",
    "owners": "users owning records or drafts",
}
ACTORS = (
    "actor: owner = owner of the picked item, other = a user who does not own it, "
    "curator = a curator of its community, user = any user"
)


class Pool:
    """The items of the benchmark database that requests pick their targets from."""

    def __init__(self, state):
        records = state["records"]
        self.public = [r for r in records if not r["restricted"]]
        self.restricted = [r for r in records if r["restricted"]]
        self.with_files = [r for r in self.public if r["files"]]
        self.curated = [r for r in self.restricted if r["community"]]
        self.drafts = state["drafts"]
        self.requests = state["requests"]
        self.communities = [c for c in state["communities"] if c["visibility"] == "public"]
        owners = {r["owner"] for r in records} | {d["owner"] for d in self.drafts}
        self.owners = [{"owner": email} for email in sorted(owners)]
        self.by_id = {c["id"]: c for c in state["communities"]}
        self.users = [u["email"] for u in state["users"]]
        self.admin = state["admin"]["email"]


@dataclass
class Step:
    actor: str
    label: str
    method: str
    url: str
    body: dict | None = None
    # Key of the draft created before the round for this update or delete; fills {draft} in url.
    draft: str | None = None


@dataclass(frozen=True)
class Endpoint:
    """A request that picks an ``item`` from ``pool`` and formats ``url`` with it and its ``community``."""

    label: str
    kind: str
    weight: int
    actor: str
    url: str
    pool: str | None = None
    method: str = "GET"

    @property
    def needs(self):
        return POOLS.get(self.pool)

    def available(self, pool):
        return not self.pool or bool(getattr(pool, self.pool))

    def step(self, pool, rng, index):
        item = rng.choice(getattr(pool, self.pool)) if self.pool else None
        community = item if self.pool == "communities" else pool.by_id.get((item or {}).get("community"))
        match self.actor:
            case "owner":
                actor = item["owner"]
            case "other":
                actor = rng.choice([u for u in pool.users if u != item["owner"]] or pool.users)
            case "curator":
                actor = rng.choice(community["curators"])
            case "user":
                actor = rng.choice(pool.users)
            case "admin":
                actor = pool.admin
            case _:
                actor = ANONYMOUS
        # Updates of one actor share a draft; every delete needs its own.
        draft = {"update": f"update-{actor}", "delete": f"delete-{index}"}.get(self.kind)
        url = self.url if draft else self.url.format(item=item, community=community, word=rng.choice(WORDS))
        body = record_data(rng, index) if self.method in ("POST", "PUT") else None
        return Step(actor, self.label, self.method, url, body, draft)


# Weights set each endpoint's share of ``mix`` and of its type: a production-like mix of
# 55% anonymous, 30% owner, 12% curator and 3% administrator requests.
ENDPOINTS = [
    # anonymous visitors
    Endpoint("ui:record-landing", "read", 14, "anonymous", "/records/{item[id]}", "public"),
    Endpoint("api:record-read", "read", 8, "anonymous", "/api/records/{item[id]}", "public"),
    Endpoint("api:search-records", "search", 8, "anonymous", "/api/records?q=&sort=newest&size=10"),
    Endpoint("api:search-records-query", "search", 4, "anonymous", "/api/records?q={word}&size=10"),
    Endpoint("ui:frontpage", "read", 3, "anonymous", "/"),
    Endpoint("ui:search", "search", 3, "anonymous", "/search?q={word}"),
    Endpoint("ui:record-landing-denied", "read", 3, "anonymous", "/records/{item[id]}", "restricted"),
    Endpoint("api:record-files", "read", 3, "anonymous", "/api/records/{item[id]}/files", "with_files"),
    Endpoint("api:search-communities", "search", 3, "anonymous", "/api/communities?q=&size=10"),
    Endpoint("ui:community-records", "search", 3, "anonymous", "/communities/{item[slug]}/records", "communities"),
    Endpoint("api:community-records", "search", 3, "anonymous", "/api/communities/{item[id]}/records", "communities"),
    # record owners
    Endpoint("api:user-records", "search", 5, "owner", "/api/user/records?q=&sort=updated-desc", "owners"),
    Endpoint("ui:dashboard-uploads", "search", 3, "owner", "/me/uploads", "owners"),
    Endpoint("ui:record-landing-owner", "read", 5, "owner", "/records/{item[id]}", "public"),
    Endpoint("ui:deposit-edit", "read", 3, "owner", "/uploads/{item[id]}", "drafts"),
    Endpoint("api:draft-read", "read", 3, "owner", "/api/records/{item[id]}/draft", "drafts"),
    Endpoint("api:user-requests", "search", 3, "owner", "/api/user/requests?q=", "owners"),
    Endpoint("ui:record-landing-other", "read", 3, "other", "/records/{item[id]}", "public"),
    Endpoint("ui:deposit-new", "read", 2, "user", "/uploads/new"),
    Endpoint("api:draft-create", "create", 3, "user", "/api/records", method="POST"),
    Endpoint("api:draft-update", "update", 3, "user", "/api/records/{draft}/draft", method="PUT"),
    Endpoint("api:draft-delete", "delete", 3, "user", "/api/records/{draft}/draft", method="DELETE"),
    # community curators
    Endpoint("api:community-requests", "search", 3, "curator", "/api/communities/{community[id]}/requests?is_open=true", "requests"),
    Endpoint("ui:community-requests", "search", 2, "curator", "/communities/{community[slug]}/requests", "requests"),
    Endpoint("api:request-read", "read", 2, "curator", "/api/requests/{item[id]}", "requests"),
    Endpoint("api:request-timeline", "read", 2, "curator", "/api/requests/{item[id]}/timeline", "requests"),
    Endpoint("ui:request-detail", "read", 2, "curator", "/communities/{community[slug]}/requests/{item[id]}", "requests"),
    Endpoint("ui:record-landing-curator", "read", 1, "curator", "/records/{item[id]}", "curated"),
    # administrator
    Endpoint("ui:record-landing-admin", "read", 1, "admin", "/records/{item[id]}", "public"),
    Endpoint("api:record-read-admin", "read", 1, "admin", "/api/records/{item[id]}", "restricted"),
    Endpoint("api:search-records-admin", "search", 1, "admin", "/api/records?q=&size=10"),
]
BY_LABEL = {e.label: e for e in ENDPOINTS}


def kind_of(label):
    return BY_LABEL[label].kind if label in BY_LABEL else "?"


def parse_request(text):
    """argparse type of ``--requests`` items: ``NAME=COUNT``."""
    name, _, count = text.partition("=")
    if name not in ("mix", *KINDS, *BY_LABEL) or not count.isdigit():
        raise argparse.ArgumentTypeError(
            f"{text!r} is not TYPE=COUNT (types: mix, {', '.join(KINDS)}) or ENDPOINT=COUNT (see endpoints)"
        )
    return name, int(count)


def apportion(total, weights):
    """Split ``total`` in proportion to ``weights``, by largest remainders."""
    whole = sum(weights.values())
    quotas = {key: total * weight / whole for key, weight in weights.items()}
    counts = {key: int(quota) for key, quota in quotas.items()}
    by_remainder = sorted(quotas, key=lambda key: counts[key] - quotas[key])
    for key in by_remainder[: total - sum(counts.values())]:
        counts[key] += 1
    return counts


def plan_round(requests, pool):
    """Requests per endpoint in every round, from ``(name, count)`` pairs of ``--requests``."""
    counts = Counter()
    skipped = []
    for name, count in requests:
        if name in BY_LABEL:
            if not BY_LABEL[name].available(pool):
                raise SystemExit(f"{name}: the benchmark database has no {BY_LABEL[name].needs}")
            counts[name] += count
            continue
        group = [e for e in ENDPOINTS if name in ("mix", e.kind)]
        usable = [e for e in group if e.available(pool)]
        skipped += [e for e in group if e not in usable and e not in skipped]
        if count and not usable:
            raise SystemExit(f"{name}: the benchmark database has nothing its endpoints need")
        counts.update(apportion(count, {e.label: e.weight for e in usable}))
    for e in skipped:
        print(f"skipping {e.label}: the benchmark database has no {e.needs}", file=sys.stderr)
    plan = {label: counts[label] for label in BY_LABEL if counts[label]}
    if not plan:
        raise SystemExit("the workload has no requests")
    return plan


def describe_plan(plan):
    per_kind = Counter()
    for label, count in plan.items():
        per_kind[kind_of(label)] += count
    kinds = ", ".join(f"{kind} {per_kind[kind]}" for kind in KINDS if per_kind[kind])
    return f"{sum(plan.values())} requests per round ({kinds})"


def build_round(pool, plan, rng):
    """The steps of one round in random order; the same seed gives the same round."""
    steps = [BY_LABEL[label].step(pool, rng, i) for label, count in plan.items() for i in range(count)]
    rng.shuffle(steps)
    return steps


def describe_endpoints(pool=None):
    """The endpoint catalog; with ``pool``, also how many items each endpoint picks from."""
    lines = [f"{'type':7} {'endpoint':27} {'weight':>6}  {'actor':10}{'picks from':40}request"]
    for e in ENDPOINTS:
        picks = POOLS.get(e.pool, "")
        if pool and e.pool:
            picks += f" ({len(getattr(pool, e.pool))})"
        lines.append(f"{e.kind:7} {e.label:27} {e.weight:6}  {e.actor:10}{picks:40}{e.method} {e.url}")
    lines += ["", "A type or mix count is split over its endpoints in proportion to their weights.", ACTORS]
    return "\n".join(lines)
