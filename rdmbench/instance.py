"""The InvenioRDM instance under test: its services, configuration, app and code variant."""

import os
import pkgutil
import shlex
import subprocess
import sys
from importlib import metadata
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parents[1]
INSTANCE = BENCH_DIR / "instance"
REPO = BENCH_DIR.parent / "inveniordm-reference-repo"
BASE_URL = "https://127.0.0.1:5000"
SERVICES = ["cache", "db", "mq", "search"]
ROLES = [
    ("admin", "superuser-access"),
    ("administration", "administration-access"),
    ("administration-moderation", "administration-moderation"),
]


def compose(*args):
    """Run ``docker compose`` on the reference repo's service containers."""
    command = ["docker", "compose", "--env-file", "variables", "-f", "docker-compose.yml"]
    command += ["-f", "docker-compose.ports.yml", *args]
    subprocess.run(command, cwd=REPO, check=True)


def read_variables(path):
    """Parse the shell-style ``variables`` file of the reference repo."""
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        parts = shlex.split(value, comments=True)
        values[key.removeprefix("export ").strip()] = parts[0] if parts else ""
    return values


def service_env(v):
    """Invenio configuration pointing at the services described by ``variables``."""
    redis = f"redis://{v['INVENIO_REDIS_HOST']}:{v['INVENIO_REDIS_PORT']}"
    amqp = (
        f"amqp://{v['INVENIO_RABBIT_USER']}:{v['INVENIO_RABBIT_PASSWORD']}"
        f"@{v['INVENIO_RABBIT_HOST']}:{v['INVENIO_RABBIT_PORT']}/"
    )
    database = (
        f"postgresql+psycopg2://{v['INVENIO_DATABASE_USER']}:{v['INVENIO_DATABASE_PASSWORD']}"
        f"@{v['INVENIO_DATABASE_HOST']}:{v['INVENIO_DATABASE_PORT']}/{v['INVENIO_DATABASE_DBNAME']}"
    )
    search = f"{v['INVENIO_OPENSEARCH_HOST']}:{v['INVENIO_OPENSEARCH_PORT']}"
    return {
        "INVENIO_INSTANCE_PATH": str(INSTANCE),
        "INVENIO_SQLALCHEMY_DATABASE_URI": database,
        "INVENIO_SEARCH_HOSTS": repr([search]),
        "INVENIO_SEARCH_INDEX_PREFIX": v["INVENIO_SEARCH_INDEX_PREFIX"],
        "INVENIO_CACHE_TYPE": "RedisCache",
        "INVENIO_CACHE_REDIS_URL": f"{redis}/0",
        "INVENIO_ACCOUNTS_SESSION_REDIS_URL": f"{redis}/1",
        "INVENIO_CELERY_RESULT_BACKEND": f"{redis}/2",
        "INVENIO_COMMUNITIES_IDENTITIES_CACHE_REDIS_URL": f"{redis}/4",
        "INVENIO_BROKER_URL": amqp,
        "INVENIO_CELERY_BROKER_URL": amqp,
        # No worker runs during the benchmark; tasks execute inline instead.
        "INVENIO_CELERY_TASK_ALWAYS_EAGER": "True",
        "INVENIO_CELERY_TASK_EAGER_PROPAGATES": "True",
        # invenio.cfg configures S3, which is not started; keep files on local disk.
        "INVENIO_FILES_REST_STORAGE_FACTORY": "invenio_files_rest.storage:pyfs_storage_factory",
        "INVENIO_MAIL_SUPPRESS_SEND": "True",
        "INVENIO_RATELIMIT_ENABLED": "False",
        "INVENIO_SECRET_KEY": "benchmark-not-a-secret",
        "INVENIO_SITE_UI_URL": BASE_URL,
        "INVENIO_SITE_API_URL": f"{BASE_URL}/api",
        "INVENIO_LOGGING_CONSOLE_LEVEL": "WARNING",
    }


def prepare_environment():
    """Export the service configuration and link the repo's files into the instance."""
    INSTANCE.mkdir(exist_ok=True)
    (INSTANCE / "data").mkdir(exist_ok=True)
    for name in ("invenio.cfg", "app_data", "templates", "static"):
        link = INSTANCE / name
        if not link.exists():
            link.symlink_to(REPO / name)
    os.environ.update(service_env(read_variables(REPO / "variables")))


class _NoAsset(str):
    def __html__(self):
        return ""

    def render(self):
        return ""


class NoAssetsManifestLoader:
    """Webpack manifest stand-in so that UI pages render without built assets."""

    def load(self, filepath):
        return self

    def __getitem__(self, key):
        return _NoAsset()

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _NoAsset()


def create_app():
    from invenio_app.factory import create_app as invenio_create_app

    return invenio_create_app(WEBPACKEXT_MANIFEST_LOADER=NoAssetsManifestLoader)


def invenio_cli(*args, check=True):
    invenio = Path(sys.executable).parent / "invenio"
    print(f"$ invenio {' '.join(args)}", flush=True)
    subprocess.run([str(invenio), *args], check=check, stdout=subprocess.DEVNULL)


def wipe_and_create():
    """Same steps as the reference repo's wipe_recreate.sh, without demo data."""
    import redis

    redis.StrictRedis.from_url(os.environ["INVENIO_CACHE_REDIS_URL"]).flushall()
    invenio_cli("db", "drop", "--yes-i-know", check=False)
    invenio_cli("index", "destroy", "--force", "--yes-i-know", check=False)
    invenio_cli("index", "queue", "init", "purge", check=False)
    invenio_cli("db", "create")
    invenio_cli("files", "location", "create", "--default", "default-location", str(INSTANCE / "data"))
    for role, action in ROLES:
        invenio_cli("roles", "create", role)
        invenio_cli("access", "allow", action, "role", role)
    invenio_cli("index", "init", "--force")
    invenio_cli("rdm-records", "custom-fields", "init")
    invenio_cli("communities", "custom-fields", "init")
    invenio_cli("rdm-records", "fixtures")


def git_describe(path):
    try:
        return subprocess.run(
            ["git", "-C", str(path), "describe", "--always", "--dirty", "--all"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def use_variant(variant, patched_src):
    """Put the patched directories first on sys.path; must run before anything imports what they patch."""
    if variant == "patched":
        sys.path[:0] = [str(src) for src in patched_src]


def code_under_test(variant, patched_src):
    """Check where the app imported the modules of ``patched_src`` from; return what code it ran.

    Call it once the app has imported what it uses, e.g. after a round of requests.
    """
    versions = []
    for src in patched_src:
        names = [m.name for m in pkgutil.iter_modules([str(src)]) if m.name in sys.modules]
        if not names:
            raise SystemExit(f"the app imports none of the modules in {src}")
        for name in names:
            file = Path(sys.modules[name].__file__)
            if file.is_relative_to(src) != (variant == "patched"):
                raise SystemExit(f"the {variant} variant imported {name} from {file}")
        if variant == "patched":
            versions.append(f"{src.name} {git_describe(src)}")
        else:
            versions += [installed_version(name) for name in names]
    return ", ".join(versions)


def installed_version(module):
    distributions = metadata.packages_distributions().get(module)
    return ", ".join(f"{d} {metadata.version(d)}" for d in distributions) if distributions else module
