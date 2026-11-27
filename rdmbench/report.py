"""The baseline/patched comparison that ``compare`` writes."""

import statistics
from collections import Counter
from pathlib import Path

from rdmbench import plugins
from rdmbench.state import StateSpec
from rdmbench.workload import KINDS, describe_requests

VARIANTS = ("baseline", "patched")


def summarize(outputs):
    """Compare the results of both variants, given as ``{variant: [result of each run]}``."""
    return "\n\n".join([_header(outputs), _medians(outputs), _status_codes(outputs)]) + "\n"


def _change(before, after):
    return f"{(after - before) / before * 100:+.1f}%" if before else "n/a"


def _header(outputs):
    first = outputs["baseline"][0]
    lines = [f"{v:9} {outputs[v][0]['source']}" for v in VARIANTS]
    lines += [
        f"database  {StateSpec(**first['state']).describe()}",
        f"workload  {Path(first['workload']).name}, {describe_requests(first['requests'])}",
        f"plugins   {', '.join(first['plugins']) or 'none'}",
        f"{len(outputs['baseline'])} runs per variant x {len(first['rounds'])} rounds",
    ]
    return "\n".join(lines)


def _medians(outputs):
    """Medians per round of every metric, for all requests and per type."""
    metrics = plugins.metrics(outputs["baseline"][0]["plugins"])
    rounds = {v: [samples for result in outputs[v] for samples in result["rounds"]] for v in VARIANTS}
    kinds = {e["label"]: e["kind"] for e in outputs["baseline"][0]["endpoints"]}
    sizes = Counter(kinds[s["label"]] for s in rounds["baseline"][0])

    def median(variant, kind, key):
        return statistics.median(
            sum(s[key] for s in samples if kind == "all" or kinds[s["label"]] == kind) for samples in rounds[variant]
        )

    header = f"{'median/round':16} {'requests':>8}"
    header += "".join(f"   {m.title + ' b':>8} {m.title + ' p':>8} {'change':>7}" for m in metrics)
    lines = [header]
    for kind in ["all", *(k for k in KINDS if sizes[k])]:
        line = f"{kind:16} {sizes.total() if kind == 'all' else sizes[kind]:8}"
        for m in metrics:
            before, after = (median(v, kind, m.key) for v in VARIANTS)
            line += f"   {before:8{m.fmt}} {after:8{m.fmt}} {_change(before, after):>7}"
        lines.append(line)
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
