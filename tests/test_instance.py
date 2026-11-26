import importlib
import re
import sys

import pytest

from rdmbench.instance import code_under_test, read_variables


def test_variables_are_read_like_the_shell_reads_them(tmp_path):
    path = tmp_path / "variables"
    path.write_text('# services\nexport HOST=127.0.0.1\nUSER="guest"  # quoted\n\nEMPTY=\n')

    assert read_variables(path) == {"HOST": "127.0.0.1", "USER": "guest", "EMPTY": ""}


@pytest.fixture
def checkouts(tmp_path):
    """Directories ``installed`` and ``patched``, each with the package ``rdmbench_fake``."""
    for name in ("installed", "patched"):
        (tmp_path / name / "rdmbench_fake").mkdir(parents=True)
        (tmp_path / name / "rdmbench_fake" / "__init__.py").touch()
    yield tmp_path
    sys.modules.pop("rdmbench_fake", None)


@pytest.mark.parametrize(
    "variant, imported_from, code",
    [("patched", "patched", "patched unknown"), ("baseline", "installed", "rdmbench_fake")],
)
def test_variant_that_imported_its_own_code_is_accepted(checkouts, monkeypatch, variant, imported_from, code):
    monkeypatch.syspath_prepend(checkouts / imported_from)
    importlib.import_module("rdmbench_fake")

    assert code_under_test(variant, [checkouts / "patched"]) == code


@pytest.mark.parametrize("variant, imported_from", [("patched", "installed"), ("baseline", "patched")])
def test_variant_that_imported_the_other_variants_code_is_rejected(checkouts, monkeypatch, variant, imported_from):
    monkeypatch.syspath_prepend(checkouts / imported_from)
    importlib.import_module("rdmbench_fake")

    problem = f"the {variant} variant imported rdmbench_fake from {checkouts / imported_from}"
    with pytest.raises(SystemExit, match=re.escape(problem)):
        code_under_test(variant, [checkouts / "patched"])


def test_patched_code_the_app_never_imports_is_rejected(checkouts):
    with pytest.raises(SystemExit, match="the app imports none of the modules in"):
        code_under_test("patched", [checkouts / "patched"])
