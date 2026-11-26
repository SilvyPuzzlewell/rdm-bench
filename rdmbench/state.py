"""What the benchmark database contains: the ``setup`` parameters and the state.json they produce."""

import json
from dataclasses import asdict, dataclass, field, fields

from rdmbench.instance import BENCH_DIR

STATE = BENCH_DIR / "state.json"
WORDS = (
    "climate river protein galaxy neural soil archive glacier enzyme quantum "
    "forest coral lattice plasma genome harbor meteor polymer sensor tundra"
).split()
SURNAMES = "Novak Svoboda Dvorak Cerny Prochazka Kucera Vesely Horak Nemec Pokorny".split()
GIVEN_NAMES = "Jana Petr Eva Tomas Lucie Martin Anna Jakub Tereza Ondrej".split()
RESOURCE_TYPES = ["publication-article", "dataset", "image-photo", "software"]


def _param(default, doc):
    return field(default=default, metadata={"doc": doc})


@dataclass
class StateSpec:
    """Contents of the benchmark database; each field is a ``setup`` option."""

    users: int = _param(40, "users besides the administrator")
    communities: int = _param(6, "public communities")
    restricted_communities: int = _param(2, "restricted communities")
    curators: int = _param(2, "curators of each community")
    readers: int = _param(3, "readers of each community")
    records: int = _param(300, "published records")
    community_records: int = _param(140, "of the published records, those a curator accepted into a community")
    drafts: int = _param(60, "unpublished drafts")
    submissions: int = _param(40, "drafts submitted to a community, with an open review request")
    inclusions: int = _param(12, "published records with an open request to join a community")
    restricted_share: float = _param(0.2, "share of restricted records, drafts and submissions")
    files_share: float = _param(0.15, "share of records, drafts and submissions with an uploaded file")
    seed: int = _param(1, "random seed of the generated data")

    @classmethod
    def add_arguments(cls, parser):
        for f in fields(cls):
            parser.add_argument(
                f"--{f.name.replace('_', '-')}",
                type=type(f.default),
                default=f.default,
                help=f"{f.metadata['doc']} (default: {f.default})",
            )

    @classmethod
    def from_args(cls, args):
        return cls(**{f.name: getattr(args, f.name) for f in fields(cls)})

    def check(self):
        problems = list(self._problems())
        if problems:
            raise SystemExit("invalid database parameters:\n  " + "\n  ".join(problems))

    def _problems(self):
        if any(value < 0 for name, value in asdict(self).items() if name != "seed"):
            yield "counts cannot be negative"
        if not (0 <= self.restricted_share <= 1 and 0 <= self.files_share <= 1):
            yield "shares must be within 0..1"
        if self.users < 1:
            yield "--users must be at least 1"
        standalone = self.records - self.community_records
        if standalone < 0:
            yield "--community-records cannot exceed --records"
        if self.communities < 1 and self.community_records + self.submissions + self.inclusions:
            yield "community records, submissions and inclusions need a public community"
        if self.communities + self.restricted_communities:
            if self.curators < 1:
                yield "communities need at least one curator"
            if self.users - 1 < self.curators + self.readers:
                yield "--users must exceed --curators plus --readers, whom the community owner invites"
        public_standalone = max(0, standalone - round(standalone * self.restricted_share))
        if self.inclusions > public_standalone:
            yield f"--inclusions cannot exceed the {public_standalone} public records outside communities"

    def describe(self):
        return (
            f"{self.users} users, {self.communities} public + {self.restricted_communities} restricted communities "
            f"({self.curators} curators, {self.readers} readers each), {self.records} records "
            f"({self.community_records} in communities), {self.drafts} drafts, {self.submissions} submissions, "
            f"{self.inclusions} inclusion requests, {self.restricted_share:.0%} restricted, "
            f"{self.files_share:.0%} with files, seed {self.seed}"
        )


def load_state():
    if not STATE.exists():
        raise SystemExit(f"{STATE} does not exist; run setup first")
    return json.loads(STATE.read_text())


def save_state(state):
    STATE.write_text(json.dumps(state, indent=2))


def record_data(rng, index, restricted=False, with_files=False):
    access = "restricted" if restricted else "public"
    return {
        "access": {"record": access, "files": access},
        "files": {"enabled": with_files},
        "metadata": {
            "title": f"{rng.choice(WORDS).title()} {rng.choice(WORDS)} study {index}",
            "resource_type": {"id": rng.choice(RESOURCE_TYPES)},
            "publication_date": f"{rng.randint(2000, 2025)}-0{rng.randint(1, 9)}-1{rng.randint(0, 9)}",
            "creators": [
                {
                    "person_or_org": {
                        "type": "personal",
                        "family_name": rng.choice(SURNAMES),
                        "given_name": rng.choice(GIVEN_NAMES),
                    }
                }
            ],
            "publisher": "InvenioRDM Reference Repo",
        },
    }
