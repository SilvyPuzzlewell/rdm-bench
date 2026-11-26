"""Text reports: the baseline/patched comparison of a ``compare`` and the cProfile report of a ``run``."""

import io
import pstats
import statistics
import sysconfig
from collections import Counter, defaultdict

from rdmbench.state import describe_state
from rdmbench.workload import describe_plan, kind_of

VARIANTS = ("baseline", "patched")
PERMISSION_CODE = r"invenio_records_permissions/|invenio_access/permissions|flask_principal|generators\.py"


def summarize(outputs):
    """Compare the results of both variants, given as ``{variant: [result of each run]}``."""
    rounds = {v: [r for result in outputs[v] for r in result["rounds"]] for v in VARIANTS}
    sections = [
        _header(outputs, rounds),
        _totals(rounds),
        _by_type(rounds),
        _by_endpoint(rounds),
        _by_policy(rounds),
        _status_codes(outputs),
    ]
    return "\n\n".join(sections) + "\n"


def _change(before, after):
    return f"{(after - before) / before * 100:+.1f}%" if before else "n/a"


def _table(title, counts, medians, digits):
    """A row per name in ``counts``, from ``medians[variant][name] = (time, allows() time)``."""
    lines = [f"{title:30} {'n':>5} {'baseline':>9} {'patched':>9} {'change':>8} {'allows b':>9} {'allows p':>9}"]
    for name, n in counts.items():
        if name not in medians["patched"]:
            continue
        (b, allows_b), (p, allows_p) = medians["baseline"][name], medians["patched"][name]
        lines.append(
            f"{name:30} {n:5} {b:9.{digits}f} {p:9.{digits}f} {_change(b, p):>8}"
            f" {allows_b:9.{digits}f} {allows_p:9.{digits}f}"
        )
    return "\n".join(lines)


def _header(outputs, rounds):
    first = outputs["baseline"][0]
    lines = [f"{v:9} {outputs[v][0]['source']}  ({outputs[v][0]['module']})" for v in VARIANTS]
    # Results from before the workload took parameters carry "size" instead of "plan".
    workload = describe_plan(first["plan"]) if "plan" in first else f"~{first['size']} sampled requests per round"
    runs = len(outputs["baseline"])
    lines += [
        f"database  {describe_state(first.get('state'))}",
        f"workload  {workload}",
        f"{runs} runs per variant x {len(rounds['baseline']) // runs} rounds, seed {first['seed']}, "
        f"Python {first['python']}",
    ]
    return "\n".join(lines)


def _totals(rounds):
    def median_total(variant, key):
        return statistics.median(sum(s[key] for s in r["samples"]) for r in rounds[variant])

    req_b, req_p = (median_total(v, "seconds") for v in VARIANTS)
    all_b, all_p = (median_total(v, "allows_seconds") for v in VARIANTS)
    calls_b, calls_p = (median_total(v, "allows_calls") for v in VARIANTS)
    return "\n".join(
        [
            f"{'median per round':34} {'baseline':>10} {'patched':>10} {'change':>8}",
            f"{'request time (s)':34} {req_b:10.2f} {req_p:10.2f} {_change(req_b, req_p):>8}",
            f"{'allows() time (s)':34} {all_b:10.2f} {all_p:10.2f} {_change(all_b, all_p):>8}",
            f"{'allows() calls':34} {calls_b:10.0f} {calls_p:10.0f}",
            f"{'allows() share of request time':34} {all_b / req_b:10.1%} {all_p / req_p:10.1%}",
        ]
    )


def _by_type(rounds):
    def median_total(variant, kind, key):
        return statistics.median(
            sum(s[key] for s in r["samples"] if kind_of(s["label"]) == kind) for r in rounds[variant]
        )

    counts = Counter(kind_of(s["label"]) for s in rounds["baseline"][0]["samples"])
    medians = {
        v: {kind: (median_total(v, kind, "seconds"), median_total(v, kind, "allows_seconds")) for kind in counts}
        for v in VARIANTS
    }
    order = sorted(counts, key=lambda kind: -medians["baseline"][kind][0])
    return _table("type (median s per round)", {kind: counts[kind] for kind in order}, medians, 2)


def _by_endpoint(rounds):
    samples = {v: defaultdict(list) for v in VARIANTS}
    for v in VARIANTS:
        for r in rounds[v]:
            for s in r["samples"]:
                samples[v][s["label"]].append(s)

    def median_ms(items, key):
        return statistics.median(s[key] for s in items) * 1000

    medians = {
        v: {label: (median_ms(items, "seconds"), median_ms(items, "allows_seconds")) for label, items in by_label.items()}
        for v, by_label in samples.items()
    }
    baseline = samples["baseline"]
    order = sorted(baseline, key=lambda label: -sum(s["seconds"] for s in baseline[label]))
    return _table("endpoint (median ms)", {label: len(baseline[label]) for label in order}, medians, 1)


def _by_policy(rounds):
    # Medians across rounds: a single GC-sized pause would otherwise dominate a policy's total.
    def median(variant, key, index):
        return statistics.median(r["policies"].get(key, [0, 0.0])[index] for r in rounds[variant])

    keys = {key for v in VARIANTS for r in rounds[v] for key in r["policies"]}
    lines = [f"{'policy:action (median allows() ms/round)':44} {'calls':>7} {'baseline':>9} {'patched':>9} {'change':>8}"]
    for key in sorted(keys, key=lambda k: -median("baseline", k, 1))[:15]:
        b, p = median("baseline", key, 1) * 1000, median("patched", key, 1) * 1000
        lines.append(f"{key:44} {median('baseline', key, 0):7.0f} {b:9.1f} {p:9.1f} {_change(b, p):>8}")
    return "\n".join(lines)


def _status_codes(outputs):
    """Check that every run of both variants answered each request like the first baseline run."""
    reference = outputs["baseline"][0]["rounds"]
    mismatches = Counter()
    for variant in VARIANTS:
        for result in outputs[variant]:
            for r, expected in zip(result["rounds"], reference):
                if len(r["samples"]) != len(expected["samples"]):
                    mismatches[(variant, "requests per round", len(expected["samples"]), len(r["samples"]))] += 1
                    continue
                for sample, want in zip(r["samples"], expected["samples"], strict=True):
                    if sample["status"] != want["status"]:
                        mismatches[(variant, sample["label"], want["status"], sample["status"])] += 1
    if mismatches:
        lines = ["status code mismatches against the first baseline run:"]
        for (variant, label, expected, got), count in sorted(mismatches.items()):
            lines.append(f"  {variant:9} {label:30} expected {expected} got {got}  x{count}")
        return "\n".join(lines)
    statuses = Counter(s["status"] for s in reference[0]["samples"])
    codes = ", ".join(f"{code}: {n}" for code, n in sorted(statuses.items()))
    return f"status codes identical across all runs and variants ({codes} in round 1)"


def profile_report(path, variant, rounds, seed, plan):
    """Readable pstats sections, with the site-packages prefix trimmed from paths."""
    out = io.StringIO()
    stats = pstats.Stats(str(path), stream=out)

    def section(title, explanation):
        out.write(f"\n{'=' * 110}\n{title}\n{explanation}\n{'=' * 110}\n")

    out.write(
        f"cProfile of the {variant} variant: {rounds} measured rounds of {describe_plan(plan)} (seed {seed});\n"
        "app start-up, the warm-up round and the drafts created and deleted around each round are not included.\n"
        "cProfile instruments every call, so code made of many small calls (like permission checks)\n"
        "looks more expensive than it is; compare shares rather than absolute times.\n"
        f"Open {path.name} in PyCharm (Tools > Open CProfile snapshot) or with `snakeviz {path.name}`.\n"
    )
    section("Top 70 functions by cumulative time", "cumtime = time in the function including everything it calls")
    stats.sort_stats("cumulative").print_stats(70)
    section("Top 70 functions by own time", "tottime = time in the function body itself, callees excluded")
    stats.sort_stats("tottime").print_stats(70)
    section(
        "Permission code by cumulative time",
        "invenio-records-permissions, invenio-access Permission, flask-principal and all generators.py modules",
    )
    stats.sort_stats("cumulative").print_stats(PERMISSION_CODE, 80)
    section("Who calls Permission.allows()", "callers of invenio_access Permission.allows, with call counts")
    stats.print_callers(r"invenio_access/permissions\.py:\d+\(allows\)")
    section("Who reads BasePermissionPolicy.needs", "the generator evaluation behind allows() and explicit reads")
    stats.print_callers(r"invenio_records_permissions/policies/base\.py:\d+\(needs\)")
    return out.getvalue().replace(sysconfig.get_paths()["purelib"] + "/", "")
