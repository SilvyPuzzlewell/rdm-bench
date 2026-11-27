"""End to end on a throwaway instance: ``setup``, then ``compare`` of both variants.

Deselected by default; run with ``pytest -m e2e``. Needs the services that prepare.sh starts
and takes a few minutes. The instance under test is a copy of the benchmark with its own
database, indices, state.json and results, all removed afterwards. Redis is shared with the
real instance and setup flushes it, so the tests flush it again when done; it holds only
caches and sessions.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from importlib import metadata
from pathlib import Path
from types import SimpleNamespace

import pytest
import redis

from rdmbench import instance
from rdmbench.workload import BY_LABEL, ENDPOINTS, KINDS

pytestmark = pytest.mark.e2e

NAME = "rdmbench-test"
PATCHED = instance.BENCH_DIR.parent / "invenio-records-permissions"
SETUP = (
    "--users 6 --communities 1 --restricted-communities 1 --curators 1 --readers 1 --records 8 "
    "--community-records 4 --drafts 2 --submissions 2 --inclusions 1 --restricted-share 0.5 --files-share 1"
).split()
# As many requests of each type as its endpoints weigh, so that every endpoint gets at least one.
REQUESTS = [arg for kind in KINDS for arg in (f"--{kind}", str(sum(e.weight for e in ENDPOINTS if e.kind == kind)))]
EXPECTED_STATUS = {"api:draft-create": 201, "api:draft-delete": 204, "ui:record-landing-denied": 302}
DELAY = 0.005
SLOWDOWN = f"""
import time as _time

_allows = Permission.allows


def _slow_allows(self, identity):
    _time.sleep({DELAY})
    return _allows(self, identity)


Permission.allows = _slow_allows
"""


class Throwaway:
    """A copy of the benchmark whose instance has its own database and indices."""

    def __init__(self, root):
        self.copy = root / "benchmark"
        shutil.copytree(
            instance.BENCH_DIR / "rdmbench", self.copy / "rdmbench", ignore=shutil.ignore_patterns("__pycache__")
        )
        shutil.copy(instance.BENCH_DIR / "rdm_permissions_bench.py", self.copy)
        repo = root / "inveniordm-reference-repo"
        repo.mkdir()
        for name in ("invenio.cfg", "app_data", "templates", "static"):
            (repo / name).symlink_to(instance.REPO / name)
        # Later assignments win.
        overrides = f'INVENIO_DATABASE_DBNAME="{NAME}"\nINVENIO_SEARCH_INDEX_PREFIX="{NAME}-"\n'
        (repo / "variables").write_text((instance.REPO / "variables").read_text() + "\n" + overrides)
        self.services = instance.read_variables(repo / "variables")

    def run(self, *args, env=None):
        """Run the copy's ``rdm_permissions_bench.py`` with ``env`` added to the environment."""
        command = [sys.executable, str(self.copy / "rdm_permissions_bench.py"), *args]
        return subprocess.run(command, capture_output=True, text=True, env=os.environ | (env or {}))

    def invenio(self, *args, check=True):
        """Run the ``invenio`` command on the copy's instance."""
        code = "from rdmbench import instance; instance.prepare_environment(); "
        code += f"instance.invenio_cli(*{args!r}, check={check})"
        subprocess.run([sys.executable, "-c", code], cwd=self.copy, check=check)

    def search(self, method, path):
        url = f"http://{self.services['INVENIO_OPENSEARCH_HOST']}:{self.services['INVENIO_OPENSEARCH_PORT']}/{path}"
        with urllib.request.urlopen(urllib.request.Request(url, method=method)) as response:
            return json.load(response)

    def count(self, index, query="*"):
        """How many documents of the instance's ``index`` match the query string ``query``."""
        return self.search("GET", f"{NAME}-{index}/_count?" + urllib.parse.urlencode({"q": query}))["count"]

    def remove(self):
        """Drop the database, indices and templates, and empty the shared Redis."""
        self.invenio("db", "destroy", "--yes-i-know", check=False)
        for stream in self.search("GET", f"_data_stream/{NAME}-*")["data_streams"]:
            self.search("DELETE", f"_data_stream/{stream['name']}")
        for index in self.search("GET", f"_cat/indices/{NAME}-*?format=json"):
            self.search("DELETE", index["index"])
        for template in self.search("GET", "_index_template")["index_templates"]:
            if template["name"].startswith(f"{NAME}-"):
                self.search("DELETE", f"_index_template/{template['name']}")
        for name in self.search("GET", "_template"):
            if name.startswith(f"{NAME}-"):
                self.search("DELETE", f"_template/{name}")
        redis.Redis(self.services["INVENIO_REDIS_HOST"], int(self.services["INVENIO_REDIS_PORT"])).flushall()


@pytest.fixture(scope="module")
def bench(tmp_path_factory):
    bench = Throwaway(tmp_path_factory.mktemp("e2e"))
    try:
        bench.invenio("db", "init")
        setup = bench.run("setup", *SETUP)
        assert setup.returncode == 0, setup.stdout + setup.stderr
        yield bench
    finally:
        bench.remove()


@pytest.fixture(scope="module")
def compared(bench):
    drafts = bench.count("rdmrecords-drafts")
    options = ["--patched-src", str(PATCHED), "--plugin", "allows", "--runs", "2", "--rounds", "1", *REQUESTS]
    compare = bench.run("compare", *options)
    assert compare.returncode == 0, compare.stdout + compare.stderr
    results = [json.loads(path.read_text()) for path in sorted((bench.copy / "results").glob("*.json"))]
    return SimpleNamespace(stdout=compare.stdout, stderr=compare.stderr, results=results, drafts_before=drafts)


def test_setup_creates_exactly_the_requested_database(bench):
    assert bench.count("rdmrecords-records") == 8
    assert bench.count("rdmrecords-records", "access.record:restricted") == 4
    assert bench.count("rdmrecords-records", "_exists_:parent.communities.ids") == 4
    assert bench.count("rdmrecords-records", "files.enabled:true") == 8
    assert bench.count("rdmrecords-drafts") == 2 + 2  # drafts and submissions
    assert bench.count("communities") == 2
    assert bench.count("communities", "access.visibility:restricted") == 1
    assert bench.count("requests", "type:community-submission AND status:submitted") == 2
    assert bench.count("requests", "type:community-inclusion AND status:submitted") == 1


def test_every_endpoint_gets_its_expected_answer_in_every_run(compared):
    answers = {
        (s["label"], s["status"]) for result in compared.results for samples in result["rounds"] for s in samples
    }

    assert answers == {(label, EXPECTED_STATUS.get(label, 200)) for label in BY_LABEL}
    assert "status codes identical across all runs and variants" in compared.stdout


def test_each_variant_runs_the_code_it_reports(compared):
    assert {result["variant"]: result["source"] for result in compared.results} == {
        "baseline": f"invenio-records-permissions {metadata.version('invenio-records-permissions')}",
        "patched": f"invenio-records-permissions {instance.git_describe(PATCHED)}",
    }


def test_allows_plugin_counts_permission_checks_in_every_round(compared):
    for result in compared.results:
        assert all(sum(s["allows_calls"] for s in samples) > 0 for samples in result["rounds"])


def test_compare_leaves_no_drafts_behind(bench, compared):
    assert bench.count("rdmrecords-drafts") == compared.drafts_before
    assert "deleting draft" not in compared.stderr


def copy_invenio_access(target, append=""):
    """Copy the installed invenio_access into ``target``, with ``append`` added to its permissions.py."""
    source = Path(importlib.util.find_spec("invenio_access").origin).parent
    shutil.copytree(source, target / "invenio_access", ignore=shutil.ignore_patterns("__pycache__"))
    with open(target / "invenio_access" / "permissions.py", "a") as permissions:
        permissions.write(append)


@pytest.mark.parametrize("slowed, sign", [("patched", 1), ("baseline", -1)])
def test_compare_reports_a_delay_added_to_either_variant(bench, tmp_path, slowed, sign):
    slow, fast = tmp_path / "slow", tmp_path / "fast"
    copy_invenio_access(slow, SLOWDOWN)
    copy_invenio_access(fast)
    # The baseline imports from PYTHONPATH, the patched variant from --patched-src ahead of it.
    patched, baseline = (slow, fast) if slowed == "patched" else (fast, slow)
    options = ["--patched-src", str(patched), "--plugin", "allows", "--runs", "1", "--rounds", "1", *REQUESTS]
    compare = bench.run("compare", *options, env={"PYTHONPATH": str(baseline)})
    assert compare.returncode == 0, compare.stdout + compare.stderr
    row = next(line.split() for line in compare.stdout.splitlines() if line.startswith("all "))
    time_b, time_p, time_change, allows_b, allows_p, allows_change, calls_b, calls_p, _ = (
        float(field.rstrip("%")) for field in row[2:]
    )

    # Every counted allows() call of the slowed variant sleeps once.
    added = sign * calls_b * DELAY
    assert time_p - time_b == pytest.approx(added, rel=0.2)
    assert allows_p - allows_b == pytest.approx(added, rel=0.2)
    assert time_change * sign > 0 and allows_change * sign > 0
    assert calls_p == calls_b
