import json
import subprocess
import sys
from pathlib import Path

import rdm_permissions_bench as cli
from rdmbench import instance
from rdmbench.workload import DEFAULT_WORKLOAD


def test_cli_and_plugins_import_no_invenio():
    # The patched variant only works if its directories are on sys.path before invenio is imported.
    code = (
        "import sys, rdm_permissions_bench\n"
        "from rdmbench import plugins\n"
        "for name in plugins.NAMES: plugins.load(name)\n"
        "print(sorted(m for m in sys.modules if m.startswith(('invenio', 'flask'))))"
    )
    imported = subprocess.run(
        [sys.executable, "-c", code], cwd=instance.BENCH_DIR, capture_output=True, text=True, check=True
    )

    assert imported.stdout.strip() == "[]"


def test_compare_alternates_the_variants_and_passes_its_options_to_every_run(tmp_path, monkeypatch):
    patched = tmp_path / "patched"
    patched.mkdir()
    (patched / "module.py").touch()
    runs, outputs = [], {}

    def run(command, check):
        options = dict(zip(command[3::2], command[4::2]))
        runs.append(options)
        Path(options["--out"]).write_text(json.dumps({"variant": options["--variant"]}))

    monkeypatch.setattr(cli, "RESULTS", tmp_path / "results")
    monkeypatch.setattr(cli.subprocess, "run", run)
    monkeypatch.setattr(cli, "summarize", lambda results: outputs.update(results) or "summary")
    flags = "--plugin allows --runs 3 --rounds 2 --read 5".split()
    monkeypatch.setattr(sys, "argv", ["rdm_permissions_bench.py", "compare", "--patched-src", str(patched), *flags])
    cli.main()

    assert [r["--variant"] for r in runs] == ["baseline", "patched", "patched", "baseline", "baseline", "patched"]
    for options in runs:
        assert {key: value for key, value in options.items() if key not in ("--variant", "--out")} == {
            "--rounds": "2",
            "--patched-src": str(patched),
            "--plugin": "allows",
            "--workload": str(DEFAULT_WORKLOAD),
            "--search": "115",
            "--read": "5",
            "--create": "8",
            "--update": "8",
            "--delete": "8",
        }
    assert {variant: len(results) for variant, results in outputs.items()} == {"baseline": 3, "patched": 3}
