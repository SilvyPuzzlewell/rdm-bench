"""Sends benchmark rounds through the Flask test client, timing every request and its ``allows()`` calls."""

import functools
import random
import sys
import time

from rdmbench.instance import BASE_URL
from rdmbench.state import record_data
from rdmbench.workload import ANONYMOUS


class AllowsTimer:
    """Counts and times the outermost ``allows()`` calls."""

    def __init__(self):
        self.calls = 0
        self.seconds = 0.0
        self._active = False

    def install(self):
        from invenio_access.permissions import Permission
        from invenio_records_permissions.policies.base import BasePermissionPolicy

        for cls in (Permission, BasePermissionPolicy):
            if "allows" in cls.__dict__:
                cls.allows = self._wrap(cls.__dict__["allows"])

    def _wrap(self, allows):
        @functools.wraps(allows)
        def timed_allows(permission, identity):
            # An allows() calling another one, e.g. through super(), is part of the outer call.
            if self._active:
                return allows(permission, identity)
            self._active = True
            start = time.perf_counter()
            try:
                return allows(permission, identity)
            finally:
                self.seconds += time.perf_counter() - start
                self.calls += 1
                self._active = False

        return timed_allows


class Driver:
    """Sends steps like real clients do: reads in a browser session, writes to the REST API with a token."""

    def __init__(self, app, state, timer):
        self.app = app
        self.timer = timer
        self.tokens = {u["email"]: u["token"] for u in [state["admin"], *state["users"]]}
        self.browsers = {}
        self.api_client = app.test_client(use_cookies=False)
        self.drafts = {}  # Step.draft -> id of the draft created for it
        self.created = {}  # id of every draft created since create_drafts -> its owner

    def run_round(self, steps):
        """Send ``steps``; drafts for updates and deletes are created before and deleted after, unmeasured."""
        self.create_drafts(steps)
        samples = [self.send_step(step) for step in steps]
        self.delete_drafts()
        return samples

    def send_step(self, step):
        if step.method == "GET":
            _, sample = self.send(self.browser(step.actor), step.label, step.method, step.url)
            return sample
        draft_id = self.drafts.get(step.draft)
        url = step.url.format(draft=draft_id)
        response, sample = self.send_api(step.actor, step.label, step.method, url, step.body)
        if step.method == "DELETE" and response.status_code == 204:
            del self.created[draft_id]
        return sample

    def send(self, client, label, method, url, **kwargs):
        calls, seconds = self.timer.calls, self.timer.seconds
        start = time.perf_counter()
        response = client.open(url, method=method, base_url=BASE_URL, **kwargs)
        response.get_data()
        elapsed = time.perf_counter() - start
        response.close()
        sample = {
            "label": label,
            "status": response.status_code,
            "seconds": elapsed,
            "allows_seconds": self.timer.seconds - seconds,
            "allows_calls": self.timer.calls - calls,
        }
        return response, sample

    def send_api(self, actor, label, method, url, body=None):
        headers = {"Authorization": f"Bearer {self.tokens[actor]}"}
        response, sample = self.send(self.api_client, label, method, url, json=body, headers=headers)
        if method == "POST" and response.status_code == 201:
            self.created[response.json["id"]] = actor
        return response, sample

    def browser(self, actor):
        """The actor's test client, logged in through the session cookie unless anonymous."""
        if actor not in self.browsers:
            from invenio_accounts.proxies import current_datastore

            client = self.app.test_client()
            if actor != ANONYMOUS:
                # Like invenio_accounts.testutils.login_user_via_session, but for the
                # benchmark host; otherwise the cookie is set for "localhost".
                with self.app.app_context():
                    user_id = current_datastore.find_user(email=actor).get_id()
                with client.session_transaction(base_url=BASE_URL) as session:
                    session["_user_id"] = user_id
            self.browsers[actor] = client
        return self.browsers[actor]

    def check_logins(self, steps):
        for actor in sorted({s.actor for s in steps if s.method == "GET" and s.actor != ANONYMOUS}):
            response, _ = self.send(self.browser(actor), "check", "GET", "/api/user/records")
            if response.status_code != 200:
                raise SystemExit(f"login of {actor} failed: GET /api/user/records -> {response.status_code}")

    def create_drafts(self, steps):
        for step in steps:
            if step.draft and step.draft not in self.drafts:
                body = record_data(random.Random(0), 0)
                response, _ = self.send_api(step.actor, "prepare", "POST", "/api/records", body)
                if response.status_code != 201:
                    raise SystemExit(f"creating a draft for {step.label} failed with {response.status_code}")
                self.drafts[step.draft] = response.json["id"]
        if self.drafts:
            self.refresh_search()

    def delete_drafts(self):
        for draft_id, actor in self.created.items():
            response, _ = self.send_api(actor, "cleanup", "DELETE", f"/api/records/{draft_id}/draft")
            if response.status_code != 204:
                print(f"deleting draft {draft_id} failed with {response.status_code}", file=sys.stderr)
        if self.created:
            self.refresh_search()
        self.created.clear()
        self.drafts.clear()

    def refresh_search(self):
        """Make the drafts just created or deleted visible to searches, whatever the timing."""
        from invenio_search import current_search

        with self.app.app_context():
            current_search.flush_and_refresh("*")
