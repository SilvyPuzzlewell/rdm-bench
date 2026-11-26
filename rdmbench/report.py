"""The baseline/patched comparison that ``compare`` writes."""

import statistics
from collections import Counter

from rdmbench.state import StateSpec
from rdmbench.workload import BY_LABEL, KINDS, describe_requests

VARIANTS = ("baseline", "patched")


def summarize(outputs):
    """Compare the results of both variants, given as ``{variant: [result of each run]}``."""
    return "\n\n".join([_header(outputs), _times(outputs), _status_codes(outputs)]) + "\n"


def _change(before, after):
    return f"{(after - before) / before * 100:+.1f}%" if before else "n/a"


def _header(outputs):
    first = outputs["baseline"][0]
    lines = [f"{v:9} {outputs[v][0]['source']}" for v in VARIANTS]
    lines += [
        f"database  {StateSpec(**first['state']).describe()}",
        f"workload  {describe_requests(first['requests'])}",
        f"{len(outputs['baseline'])} runs per variant x {len(first['rounds'])} rounds",
    ]
    return "\n".join(lines)


def _times(outputs):
    """Medians per round of request time, ``allows()`` time and calls, for all requests and per type."""
    rounds = {v: [samples for result in outputs[v] for samples in result["rounds"]] for v in VARIANTS}
    sizes = Counter(BY_LABEL[s["label"]].kind for s in rounds["baseline"][0])

    def median(variant, kind, key):
        return statistics.median(
            sum(s[key] for s in samples if kind == "all" or BY_LABEL[s["label"]].kind == kind)
            for samples in rounds[variant]
        )

    lines = [
        f"{'median/round (s)':16} {'requests':>8}   {'time b':>8} {'time p':>8} {'change':>7}"
        f"   {'allows b':>8} {'allows p':>8} {'change':>7}   {'calls b':>7} {'calls p':>7}"
    ]
    for kind in ["all", *(k for k in KINDS if sizes[k])]:
        (time_b, allows_b, calls_b), (time_p, allows_p, calls_p) = (
            [median(v, kind, key) for key in ("seconds", "allows_seconds", "allows_calls")] for v in VARIANTS
        )
        n = sizes.total() if kind == "all" else sizes[kind]
        lines.append(
            f"{kind:16} {n:8}   {time_b:8.2f} {time_p:8.2f} {_change(time_b, time_p):>7}"
            f"   {allows_b:8.2f} {allows_p:8.2f} {_change(allows_b, allows_p):>7}   {calls_b:7.0f} {calls_p:7.0f}"
        )
    return "\n".join(lines)


def _status_codes(outputs):
    """Check that every run of both variants answered each request like the first baseline run."""
    reference = outputs["baseline"][0]["rounds"]
    mismatches = Counter()
    for variant in VARIANTS:
        for result in outputs[variant]:
            for samples, expected in zip(result["rounds"], reference, strict=True):
                for sample, want in zip(samples, expected, strict=True):
                    if sample["status"] != want["status"]:
                        mismatches[(variant, sample["label"], want["status"], sample["status"])] += 1
    if mismatches:
        lines = ["status code mismatches against the first baseline run:"]
        for (variant, label, expected, got), count in sorted(mismatches.items()):
            lines.append(f"  {variant:9} {label:30} expected {expected} got {got}  x{count}")
        return "\n".join(lines)
    statuses = Counter(s["status"] for s in reference[0])
    codes = ", ".join(f"{code}: {n}" for code, n in sorted(statuses.items()))
    return f"status codes identical across all runs and variants ({codes} in round 1)"
