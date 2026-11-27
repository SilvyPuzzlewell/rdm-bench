#!/usr/bin/env python
"""End-to-end benchmark of permission checks in a stock InvenioRDM instance.

Sends UI and REST requests of anonymous visitors, record owners, community curators and
an administrator to the oarepo/inveniordm-reference-repo instance, in process through
the Flask test client. It compares the installed packages (``baseline``) with the code
in the ``--patched-src`` directories, put first on ``sys.path`` (``patched``): the
end-to-end time of the requests and that both answer every request with the same status
code. Plugins in ``rdmbench/plugins``, enabled with ``--plugin``, measure more of every
request, e.g. ``allows`` the time spent in permission checks.

``prepare.sh [setup options]`` installs the reference repo's venv, starts the services
and generates the database; ``bench.sh [compare options]`` runs the comparison. A round
sends requests to the endpoints of the workload file, ``workload.toml`` unless
``--workload`` names another: its ``requests_per_round`` split over them by weight, but
for a type given with e.g. ``--read N``, N requests split over that type's endpoints.
Updates and deletes work on drafts created before the
round, and the drafts a round creates are deleted after it, both unmeasured.

Service ports and credentials come from the reference repo's ``variables`` file.
"""

import argparse
import json
import pkgutil
import random
import subprocess
import sys
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from rdmbench import instance, plugins
from rdmbench.measure import Driver
from rdmbench.report import summarize
from rdmbench.state import STATE, StateSpec, load_state, save_state
from rdmbench.workload import DEFAULT_WORKLOAD, KINDS, Pool, Workload

RESULTS = instance.BENCH_DIR / "results"
SEED = 100


def cmd_services_up(args):
    instance.compose("up", "-d", "--wait", *instance.SERVICES)


def cmd_setup(args):
    spec = StateSpec.from_args(args)
    spec.check()
    instance.prepare_environment()
    instance.wipe_and_create()
    # Imported here: it imports invenio, which run must not do before choosing the code variant.
    from rdmbench.generate import generate

    save_state(generate(spec))
    print(f"wrote {STATE}: {spec.describe()}")


def requests_per_type(args, workload):
    """The ``--search``, ``--read``, ... requests per round, the workload's defaults for those not given."""
    return workload.default_requests() | {k: getattr(args, k) for k in KINDS if getattr(args, k) is not None}


def cmd_run(args):
    workload = Workload.load(args.workload)
    requests = requests_per_type(args, workload)
    instance.prepare_environment()
    instance.use_variant(args.variant, args.patched_src)
    state = load_state()
    pool = Pool(state)
    plan = workload.plan_round(requests, pool)
    active = [plugins.load(name)() for name in args.plugin]
    for plugin in active:
        plugin.install()
    driver = Driver(instance.create_app(), state, active)
    metrics = plugins.metrics(args.plugin)

    warmup = workload.build_round(pool, plan, random.Random(SEED - 1))
    driver.check_logins(warmup) # sanity check
    driver.run_round(warmup)
    source = instance.code_under_test(args.variant, args.patched_src)
    rounds = []
    for index in range(args.rounds):
        samples = driver.run_round(workload.build_round(pool, plan, random.Random(SEED + index)))
        rounds.append(samples)
        totals = ", ".join(f"{m.title} {sum(s[m.key] for s in samples):{m.fmt}}" for m in metrics)
        print(f"[{args.variant}] round {index + 1}/{args.rounds}: {totals}", flush=True)
    result = {
        "variant": args.variant,
        "source": source,
        "state": state["spec"],
        "workload": str(args.workload),
        "endpoints": [asdict(e) for e in workload.endpoints],
        "requests": requests,
        "plugins": args.plugin,
        "rounds": rounds,
    }
    args.out.write_text(json.dumps(result))


def cmd_compare(args):
    """Run both variants in alternating order, each run in a fresh process."""
    requests = requests_per_type(args, Workload.load(args.workload))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    RESULTS.mkdir(exist_ok=True)
    outputs = defaultdict(list)
    for run in range(args.runs):
        for variant in ["baseline", "patched"] if run % 2 == 0 else ["patched", "baseline"]:
            out = RESULTS / f"{stamp}-{run}-{variant}.json"
            command = [sys.executable, __file__, "run", "--variant", variant, "--out", str(out)]
            command += ["--rounds", str(args.rounds)]
            command += [arg for src in args.patched_src for arg in ("--patched-src", str(src))]
            command += [arg for name in args.plugin for arg in ("--plugin", name)]
            command += ["--workload", str(args.workload)]
            command += [arg for kind, count in requests.items() for arg in (f"--{kind}", str(count))]
            subprocess.run(command, check=True)
            outputs[variant].append(json.loads(out.read_text()))
    summary = summarize(outputs)
    (RESULTS / f"{stamp}-summary.txt").write_text(summary)
    print(summary)


def module_dir(text):
    """argparse type of ``--patched-src``: a directory with Python modules, made absolute."""
    path = Path(text).expanduser().resolve()
    if not any(pkgutil.iter_modules([str(path)])):
        raise argparse.ArgumentTypeError(f"{path} has no Python modules or packages")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(required=True)
    services = commands.add_parser("services-up", help="start DB, search, cache and MQ containers")
    services.set_defaults(handler=cmd_services_up)
    setup = commands.add_parser("setup", help="wipe the instance and generate the benchmark database")
    setup.set_defaults(handler=cmd_setup)
    StateSpec.add_arguments(setup)

    for name, handler in (("run", cmd_run), ("compare", cmd_compare)):
        sub = commands.add_parser(name, help="measure one variant" if name == "run" else "measure both")
        sub.set_defaults(handler=handler)
        sub.add_argument(
            "--patched-src",
            type=module_dir,
            action="append",
            required=True,
            help="directory put first on sys.path for the patched variant, e.g. a checkout of the "
            "package under test (repeatable)",
        )
        sub.add_argument(
            "--plugin",
            choices=plugins.NAMES,
            action="append",
            default=[],
            help="also record what this plugin of rdmbench/plugins measures (repeatable)",
        )
        sub.add_argument(
            "--workload",
            type=Path,
            default=DEFAULT_WORKLOAD,
            help=f"TOML file with the endpoints to send requests to (default: {DEFAULT_WORKLOAD.name})",
        )
        for kind in KINDS:
            sub.add_argument(
                f"--{kind}",
                type=int,
                help=f"{kind} requests per round (default: the share of the workload's requests_per_round "
                f"its {kind} endpoints weigh)",
            )
        sub.add_argument("--rounds", type=int, default=3, help="measured rounds per process")
        if name == "run":
            sub.add_argument("--variant", choices=["baseline", "patched"], required=True)
            sub.add_argument("--out", type=Path, required=True)
        else:
            sub.add_argument("--runs", type=int, default=3, help="processes per variant")

    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
