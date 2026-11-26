"""Fills a freshly created instance with the users, communities, records and requests of a ``StateSpec``."""

import random
from dataclasses import asdict
from datetime import datetime, timezone
from io import BytesIO

from flask_mail import email_dispatched
from flask_security.utils import hash_password
from invenio_access.permissions import any_user, authenticated_user
from invenio_access.utils import get_identity
from invenio_accounts.proxies import current_datastore
from invenio_communities.proxies import current_communities
from invenio_communities.utils import load_community_needs
from invenio_db import db
from invenio_mail.ext import print_email
from invenio_oauth2server.models import Token
from invenio_rdm_records.proxies import current_rdm_records, current_rdm_records_service
from invenio_records_resources.tasks import manage_indexer_queues
from invenio_requests.customizations.event_types import CommentEventType
from invenio_requests.proxies import current_events_service, current_requests_service
from invenio_search import current_search

from rdmbench.instance import BASE_URL, create_app, invenio_cli
from rdmbench.state import WORDS, record_data

PASSWORD = "benchmark-password"


def generate(spec):
    """Create what ``spec`` describes; return the state that lets the workload find it."""
    app = create_app()
    email_dispatched.disconnect(print_email)
    # load_community_needs reads the session, so the services run in a request context.
    with app.test_request_context(base_url=BASE_URL):
        state = Generator(spec).run()
    seed_statistics(app, state)
    return {**state, "spec": asdict(spec)}


def flags(rng, n, share):
    """``n`` booleans in random order, exactly ``round(n * share)`` of them true."""
    k = round(n * share)
    values = [True] * k + [False] * (n - k)
    rng.shuffle(values)
    return values


def plan_entries(rng, spec):
    """``(kind, restricted, with_files)`` of every draft to create, in creation order."""
    entries = []
    for kind, n in (
        ("published", spec.records - spec.community_records),
        ("community", spec.community_records),
        ("draft", spec.drafts),
        ("submission", spec.submissions),
    ):
        entries += zip([kind] * n, flags(rng, n, spec.restricted_share), flags(rng, n, spec.files_share))
    rng.shuffle(entries)
    return entries


class Generator:
    """Creates the database contents through the services, the way the site's users would."""

    def __init__(self, spec):
        self.spec = spec
        self.rng = random.Random(spec.seed)
        # Hashing is slow on purpose, and the benchmark logs in with sessions and tokens anyway.
        self.password = hash_password(PASSWORD)
        self.users = {}
        self.emails = []
        self.public_communities = []
        self.state = {"users": [], "communities": [], "records": [], "drafts": [], "requests": []}

    def run(self):
        spec, state = self.spec, self.state
        print("users", flush=True)
        state["admin"] = self.create_user("admin@bench.example.org", roles=["admin"])
        state["users"] = [self.create_user(f"user{i}@bench.example.org") for i in range(spec.users)]
        self.emails = [u["email"] for u in state["users"]]

        print("communities", flush=True)
        visibilities = ["public"] * spec.communities + ["restricted"] * spec.restricted_communities
        state["communities"] = [self.create_community(i, visibility) for i, visibility in enumerate(visibilities)]
        self.public_communities = [c for c in state["communities"] if c["visibility"] == "public"]

        entries = plan_entries(self.rng, spec)
        for i, (kind, restricted, with_files) in enumerate(entries):
            if i % 50 == 0:
                print(f"records and drafts ({i}/{len(entries)})", flush=True)
            self.create_entry(i, kind, restricted, with_files)

        print("inclusion requests", flush=True)
        standalone = [r for r in state["records"] if r["community"] is None and not r["restricted"]]
        for record in self.rng.sample(standalone, spec.inclusions):
            self.request_inclusion(record)

        # On a live site celery beat runs this; with eager tasks it drains the queues inline.
        manage_indexer_queues()
        current_search.flush_and_refresh("*")
        return state

    def create_user(self, email, roles=()):
        user = current_datastore.create_user(
            email=email,
            password=self.password,
            active=True,
            confirmed_at=datetime.now(timezone.utc),
        )
        for role in roles:
            current_datastore.add_role_to_user(user, role)
        db.session.commit()
        token = Token.create_personal("benchmark", user.id, is_internal=True)
        db.session.commit()
        self.users[email] = user
        return {"email": email, "id": user.id, "token": token.access_token}

    def identity(self, email):
        identity = get_identity(self.users[email])
        identity.provides.update({any_user, authenticated_user})
        load_community_needs(identity)
        return identity

    def create_community(self, index, visibility):
        """Create a community and have the curators and readers its owner invites join it."""
        owner = self.emails[index % len(self.emails)]
        slug = f"community-{index}"
        data = {
            "slug": slug,
            "access": {"visibility": visibility},
            "metadata": {"title": f"{self.rng.choice(WORDS).title()} community {index}"},
        }
        community = current_communities.service.create(self.identity(owner), data)
        others = [e for e in self.emails if e != owner]
        curators = self.rng.sample(others, self.spec.curators)
        readers = self.rng.sample([e for e in others if e not in curators], self.spec.readers)
        for role, members in (("curator", curators), ("reader", readers)):
            if members:
                invitation = {
                    "members": [{"type": "user", "id": str(self.users[e].id)} for e in members],
                    "role": role,
                    "message": "Join our community.",
                }
                current_communities.service.members.invite(self.identity(owner), community.id, invitation)
        current_search.flush_and_refresh("*")
        for email in curators + readers:
            self.accept_invitations(email)
        return {"id": community.id, "slug": slug, "visibility": visibility, "owner": owner, "curators": curators}

    def accept_invitations(self, email):
        invitations = current_requests_service.search_user_requests(
            self.identity(email), params={"q": 'type:"community-invitation" AND status:submitted'}
        )
        for invitation in invitations.hits:
            current_requests_service.execute_action(self.identity(email), invitation["id"], "accept")

    def create_entry(self, index, kind, restricted, with_files):
        """Create a draft and take it as far as ``kind`` says (see ``plan_entries``)."""
        service = current_rdm_records_service
        owner = self.rng.choice(self.emails)
        draft = service.create(self.identity(owner), record_data(self.rng, index, restricted, with_files))
        if with_files:
            self.upload_file(owner, draft.id)
        entry = {"id": draft.id, "owner": owner, "restricted": restricted, "files": with_files}
        if kind == "draft":
            self.state["drafts"].append(entry)
            return
        if kind == "published":
            service.publish(self.identity(owner), draft.id)
            self.state["records"].append({**entry, "community": None})
            return
        community = self.rng.choice(self.public_communities)
        curator = community["curators"][0]
        request = self.submit_review(owner, draft.id, community)
        if kind == "community":
            current_requests_service.execute_action(self.identity(curator), request.id, "accept")
            self.state["records"].append({**entry, "community": community["id"]})
        else:
            comment = {"payload": {"content": "Could you add a description?", "format": "html"}}
            current_events_service.create(self.identity(curator), request.id, comment, CommentEventType)
            self.state["requests"].append({"id": request.id, "community": community["id"], "type": "submission"})

    def upload_file(self, email, draft_id):
        files = current_rdm_records_service.draft_files
        files.init_files(self.identity(email), draft_id, data=[{"key": "data.csv"}])
        files.set_file_content(self.identity(email), draft_id, "data.csv", BytesIO(b"x,y\n1,2\n3,4\n"))
        files.commit_file(self.identity(email), draft_id, "data.csv")

    def submit_review(self, email, draft_id, community):
        review = current_rdm_records_service.review
        receiver = {"receiver": {"community": community["id"]}, "type": "community-submission"}
        review.update(self.identity(email), draft_id, receiver)
        payload = {"payload": {"content": "Please review my upload.", "format": "html"}}
        return review.submit(self.identity(email), draft_id, data=payload)

    def request_inclusion(self, record):
        community = self.rng.choice(self.public_communities)
        data = {"communities": [{"id": community["id"]}]}
        service = current_rdm_records.record_communities_service
        processed, _ = service.add(self.identity(record["owner"]), record["id"], data)
        for item in processed:
            self.state["requests"].append({"id": item["request_id"], "community": community["id"], "type": "inclusion"})


def seed_statistics(app, state):
    """Create the aggregated stats indices that record reads query, as on a live site."""
    public = [r for r in state["records"] if not r["restricted"]]
    if not public:
        return
    viewed = next((r for r in public if r["files"]), public[0])
    client = app.test_client()
    client.get(f"/records/{viewed['id']}", base_url=BASE_URL)
    if viewed["files"]:
        client.get(f"/records/{viewed['id']}/files/data.csv?download=1", base_url=BASE_URL)
    invenio_cli("stats", "events", "process", "--eager")
    with app.app_context():
        current_search.flush_and_refresh("*")
    invenio_cli("stats", "aggregations", "process", "--eager")
