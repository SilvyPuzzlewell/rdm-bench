#!/usr/bin/env python
"""End-to-end benchmark of permission checks in a stock InvenioRDM instance.

Drives the oarepo/inveniordm-reference-repo instance through UI and REST requests of
anonymous visitors, record owners, community curators and an administrator, in
process through the Flask test client. It compares the installed packages
(``baseline``) with the code in the ``--patched-src`` directories, put first on
``sys.path`` (``patched``). Besides latency per endpoint it measures the time spent
in ``allows()`` and checks that both variants answer every request with the same
status code.

Both the database and the workload are parameters:

- ``setup`` generates the database; ``setup --help`` lists the counts (users,
  communities, members, records, drafts, requests, restricted and file shares).
- ``run`` and ``compare`` take the requests of one round as ``--requests NAME=COUNT``.
  NAME is a request type (search, read, create, update, delete), ``mix`` (every
  endpoint in production-like proportions) or a single endpoint; counts add up. A
  type or ``mix`` count is split over its endpoints by their weights, which the
  ``endpoints`` command lists. Updates and deletes work on drafts created before
  the round, and drafts the round creates are deleted after it, both unmeasured,
  so every round starts from the database ``setup`` generated.

``prepare.sh [setup options]`` installs the reference repo's venv, starts the services
and runs ``setup``; ``bench.sh [compare options]`` runs ``compare``. Or run it with
the reference repo's interpreter::

    PY=$(cd ../inveniordm-reference-repo && pwd)/.venv/bin/python
    $PY rdm_permissions_bench.py services-up
    $PY rdm_permissions_bench.py setup --records 1000 --community-records 400 --communities 20
    $PY rdm_permissions_bench.py endpoints
    $PY rdm_permissions_bench.py compare --patched-src ../invenio-records-permissions
    $PY rdm_permissions_bench.py compare --patched-src ../invenio-records-permissions --runs 4 --requests search=200 read=300

Service ports and credentials come from the reference repo's ``variables`` file
(assigned with ~/prace/maintenance/check_ports.py). The code lives in ``rdmbench``:
``instance`` (services, configuration, code variant), ``state`` and ``generate``
(the database), ``workload`` (the requests), ``measure`` and ``report``.
"""

import argparse
import cProfile
import json
import pkgutil
import random
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from rdmbench import instance
from rdmbench.measure import AllowsTimer, Driver, measure_round
from rdmbench.report import profile_report, summarize
from rdmbench.state import STATE, StateSpec, describe_state, load_state, save_state
from rdmbench.workload import (
    DEFAULT_REQUESTS,
    KINDS,
    Pool,
    build_round,
    describe_endpoints,
    describe_plan,
    parse_request,
    plan_round,
)

RESULTS = instance.BENCH_DIR / "results"
DEFAULT_REPO = instance.BENCH_DIR.parent / "inveniordm-reference-repo"


def cmd_services_up(args):
    instance.compose(args.repo, "up", "-d", "--wait", *instance.SERVICES)


def cmd_services_down(args):
    instance.compose(args.repo, "stop", *instance.SERVICES)


def cmd_setup(args):
    spec = StateSpec.from_args(args)
    spec.check()
    instance.prepare_environment(args.repo)
    instance.wipe_and_create()
    # Imported here: it imports invenio, which run must not do before choosing the code variant.
    from rdmbench.generate import generate

    save_state(generate(spec))
    print(f"wrote {STATE}: {spec.describe()}")


def cmd_endpoints(args):
    state = load_state() if STATE.exists() else None
    print(describe_endpoints(Pool(state) if state else None))
    if state:
        print(f"Database ({STATE.name}): {describe_state(state.get('spec'))}")


def cmd_run(args):
    instance.prepare_environment(args.repo)
    instance.use_variant(args.variant, args.patched_src)
    state = load_state()
    pool = Pool(state)
    plan = plan_round(args.requests or DEFAULT_REQUESTS, pool)

    timer = AllowsTimer()
    profiler = cProfile.Profile() if args.profile else None
    if not profiler:
        # Kept out of profiles, where it would only add a wrapper frame around allows().
        timer.install()
    driver = Driver(instance.create_app(), state, timer)

    warmup = build_round(pool, plan, random.Random(args.seed - 1))
    driver.check_logins(warmup)
    measure_round(driver, timer, warmup)
    module, source = instance.code_under_test(args.variant, args.patched_src)
    rounds = []
    for index in range(args.rounds):
        steps = build_round(pool, plan, random.Random(args.seed + index))
        rounds.append(measure_round(driver, timer, steps, profiler))
        samples = rounds[-1]["samples"]
        print(
            f"[{args.variant}] round {index + 1}/{args.rounds}: {len(samples)} requests, "
            f"{sum(s['seconds'] for s in samples):.1f}s, allows() {sum(s['allows_seconds'] for s in samples):.1f}s "
            f"in {sum(s['allows_calls'] for s in samples)} calls",
            flush=True,
        )

    result = {
        "variant": args.variant,
        "module": module,
        "source": source,
        "python": sys.version.split()[0],
        "state": state.get("spec"),
        "plan": plan,
        "seed": args.seed,
        "rounds": rounds,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result))
    if profiler:
        args.profile.parent.mkdir(parents=True, exist_ok=True)
        profiler.dump_stats(args.profile)
        report = args.profile.with_suffix(".txt")
        report.write_text(profile_report(args.profile, args.variant, args.rounds, args.seed, plan))
        print(f"profile: {args.profile}\nreport:  {report}")


def cmd_compare(args):
    """Run both variants in alternating order, each run in a fresh process."""
    state = load_state()
    plan = plan_round(args.requests or DEFAULT_REQUESTS, Pool(state))
    print(f"database: {describe_state(state.get('spec'))}\nworkload: {describe_plan(plan)}", flush=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    outputs = defaultdict(list)
    for run in range(args.runs):
        for variant in ["baseline", "patched"] if run % 2 == 0 else ["patched", "baseline"]:
            out = RESULTS / f"{stamp}-{run}-{variant}.json"
            command = [sys.executable, __file__, "--repo", str(args.repo), "run", "--variant", variant]
            command += [arg for src in args.patched_src for arg in ("--patched-src", str(src))]
            command += ["--rounds", str(args.rounds), "--seed", str(args.seed), "--out", str(out)]
            command += ["--requests", *(f"{label}={n}" for label, n in plan.items())]
            subprocess.run(command, check=True)
            outputs[variant].append(json.loads(out.read_text()))
    summary = summarize(outputs)
    (RESULTS / f"{stamp}-summary.txt").write_text(summary)
    print(summary)


def cmd_report(args):
    outputs = defaultdict(list)
    for path in sorted(RESULTS.glob(f"{args.stamp}-*-*.json")):
        result = json.loads(path.read_text())
        outputs[result["variant"]].append(result)
    if not outputs["baseline"] or not outputs["patched"]:
        raise SystemExit(f"no results for both variants under {RESULTS}/{args.stamp}-*")
    print(summarize(outputs))


def module_dir(text):
    """argparse type of ``--patched-src``: a directory with Python modules, made absolute."""
    path = Path(text).expanduser().resolve()
    if not any(pkgutil.iter_modules([str(path)])):
        raise argparse.ArgumentTypeError(f"{path} has no Python modules or packages")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO, help="reference repo checkout")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("services-up", help="start DB, search, cache and MQ containers")
    commands.add_parser("services-down", help="stop the containers (data volumes are kept)")
    setup = commands.add_parser("setup", help="wipe the instance and generate the benchmark database")
    StateSpec.add_arguments(setup)
    commands.add_parser("endpoints", help="list the endpoints, their types and weights")

    for name in ("run", "compare"):
        sub = commands.add_parser(name, help="measure one variant" if name == "run" else "measure both")
        sub.add_argument(
            "--patched-src",
            type=module_dir,
            action="append",
            required=True,
            help="directory put first on sys.path for the patched variant, e.g. a checkout of the "
            "package under test (repeatable)",
        )
        sub.add_argument(
            "--requests",
            nargs="+",
            action="extend",
            type=parse_request,
            metavar="NAME=COUNT",
            help=f"requests per round by type ({', '.join(KINDS)}), mix or endpoint; counts add up "
            "(default: mix=300)",
        )
        sub.add_argument("--rounds", type=int, default=3, help="measured rounds per process")
        sub.add_argument("--seed", type=int, default=100)
        if name == "run":
            sub.add_argument("--variant", choices=["baseline", "patched"], required=True)
            sub.add_argument("--out", type=Path, required=True)
            sub.add_argument("--profile", type=Path, help="cProfile the measured rounds into this .prof file")
        else:
            sub.add_argument("--runs", type=int, default=3, help="processes per variant")

    report = commands.add_parser("report", help="summarize the results of an earlier compare")
    report.add_argument("stamp", help="timestamp prefix of the result files, e.g. 20261125-151340")

    args = parser.parse_args()
    args.repo = args.repo.expanduser().resolve()
    handlers = {
        "services-up": cmd_services_up,
        "services-down": cmd_services_down,
        "setup": cmd_setup,
        "endpoints": cmd_endpoints,
        "run": cmd_run,
        "compare": cmd_compare,
        "report": cmd_report,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
