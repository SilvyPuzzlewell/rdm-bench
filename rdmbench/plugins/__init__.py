"""Measurements of every request beyond its end-to-end time, chosen with ``--plugin NAME``.

A plugin is a module in this package that defines ``Plugin``, a subclass of :class:`Plugin`.
"""

import importlib
import pkgutil
from dataclasses import dataclass


@dataclass(frozen=True)
class Metric:
    """A value of every request sample; the report compares its medians per round."""

    key: str
    title: str
    fmt: str = ".2f"


REQUEST_TIME = Metric("seconds", "time")


class Plugin:
    metrics: tuple[Metric, ...] = ()

    def install(self):
        """Hook into the code under test.

        Runs once the code variant is on ``sys.path`` and before the app is created, so import
        what the plugin hooks into here, never at module level.
        """

    def totals(self):
        """Cumulative value of each metric by key; a request's sample gets the increase during it."""
        raise NotImplementedError


NAMES = sorted(m.name for m in pkgutil.iter_modules(__path__))


def load(name):
    return importlib.import_module(f"{__name__}.{name}").Plugin


def metrics(names):
    """The request time followed by the metrics of the plugins ``names``."""
    return [REQUEST_TIME, *(metric for name in names for metric in load(name).metrics)]
