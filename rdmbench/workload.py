"""The requests of a benchmark round: the workload file's endpoints and how rounds are drawn from them."""

import sys
import tomllib
from collections import Counter
from dataclasses import dataclass
from functools import cached_property

from rdmbench.instance import BENCH_DIR
from rdmbench.state import WORDS, record_data

DEFAULT_WORKLOAD = BENCH_DIR / "workload.toml"
ANONYMOUS = "anonymous"
KINDS = ("search", "read", "create", "update", "delete")
ACTORS = (ANONYMOUS, "owner", "other", "curator", "user", "admin")
POOLS = ("public", "restricted", "with_files", "curated", "drafts", "requests", "communities", "owners")


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
    """A request that picks an ``item`` from ``pool`` and formats ``url`` with it and its ``community``.

    workload.toml describes the fields.
    """

    label: str
    kind: str
    weight: int
    actor: str
    url: str
    pool: str | None = None
    method: str = "GET"

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


def apportion(total, weights):
    """Split ``total`` in proportion to ``weights``, by largest remainders."""
    whole = sum(weights.values())
    quotas = {key: total * weight / whole for key, weight in weights.items()}
    counts = {key: int(quota) for key, quota in quotas.items()}
    by_remainder = sorted(quotas, key=lambda key: counts[key] - quotas[key])
    for key in by_remainder[: total - sum(counts.values())]:
        counts[key] += 1
    return counts


def describe_requests(requests):
    kinds = ", ".join(f"{kind} {count}" for kind, count in requests.items() if count)
    return f"{sum(requests.values())} requests per round ({kinds})"


@dataclass
class Workload:
    """The endpoints of a workload file and how many requests a round sends to them by default."""

    requests_per_round: int
    endpoints: list[Endpoint]

    @classmethod
    def load(cls, path=DEFAULT_WORKLOAD):
        """Read the workload file ``path``; exits listing the problems of an invalid one."""
        with open(path, "rb") as file:
            data = tomllib.load(file)
        problems, endpoints = [], []
        for entry in data.get("endpoints", []):
            try:
                endpoints.append(Endpoint(**entry))
            except TypeError as error:
                problems.append(f"{entry.get('label', entry)}: {error}")
        workload = cls(data.get("requests_per_round"), endpoints)
        problems += workload._problems()
        if problems:
            raise SystemExit(f"invalid workload {path}:\n  " + "\n  ".join(problems))
        return workload

    def _problems(self):
        if not isinstance(self.requests_per_round, int) or self.requests_per_round < 0:
            yield "requests_per_round must be a whole number"
        if not self.endpoints:
            yield "there are no endpoints"
        for label, count in Counter(e.label for e in self.endpoints).items():
            if count > 1:
                yield f"{label}: {count} endpoints have this label"
        for e in self.endpoints:
            for name, allowed in (("kind", KINDS), ("actor", ACTORS), ("pool", (None, *POOLS))):
                if getattr(e, name) not in allowed:
                    yield f"{e.label}: {name} must be one of {', '.join(filter(None, allowed))}"
            if not isinstance(e.weight, int) or e.weight < 1:
                yield f"{e.label}: weight must be a whole number of at least 1"

    @cached_property
    def by_label(self):
        return {e.label: e for e in self.endpoints}

    def default_requests(self):
        """Requests per type: their endpoints' share of ``requests_per_round`` by weight."""
        shares = apportion(self.requests_per_round, {e.label: e.weight for e in self.endpoints})
        return {kind: sum(shares[e.label] for e in self.endpoints if e.kind == kind) for kind in KINDS}

    def plan_round(self, requests, pool):
        """Requests per endpoint in every round: the count of each type split over its endpoints by weight."""
        plan = {}
        for kind, count in requests.items():
            endpoints = [e for e in self.endpoints if e.kind == kind]
            for e in endpoints:
                if not e.available(pool):
                    print(f"skipping {e.label}: the benchmark database has no {e.pool}", file=sys.stderr)
            plan |= apportion(count, {e.label: e.weight for e in endpoints if e.available(pool)})
        return plan

    def build_round(self, pool, plan, rng):
        """The steps of one round in random order; the same seed gives the same round."""
        steps = [self.by_label[label].step(pool, rng, i) for label, count in plan.items() for i in range(count)]
        rng.shuffle(steps)
        return steps
