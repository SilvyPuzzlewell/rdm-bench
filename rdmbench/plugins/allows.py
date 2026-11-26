"""Time spent in permission checks."""

import functools
import time

from rdmbench import plugins


class Plugin(plugins.Plugin):
    """Counts and times the outermost ``allows()`` calls of ``Permission`` and permission policies."""

    metrics = (plugins.Metric("allows_seconds", "allows"), plugins.Metric("allows_calls", "calls", ".0f"))

    def __init__(self):
        self.calls = 0
        self.seconds = 0.0
        self._active = False

    def install(self):
        from invenio_access.permissions import Permission
        from invenio_records_permissions.policies.base import BasePermissionPolicy

        for cls in (Permission, BasePermissionPolicy):
            if "allows" in cls.__dict__:
                cls.allows = self._wrap(cls.__dict__["allows"])

    def totals(self):
        return {"allows_seconds": self.seconds, "allows_calls": self.calls}

    def _wrap(self, allows):
        @functools.wraps(allows)
        def timed_allows(permission, identity):
            # An allows() calling another one, e.g. through super(), is part of the outer call.
            if self._active:
                return allows(permission, identity)
            self._active = True
            start = time.perf_counter()
            try:
                return allows(permission, identity)
            finally:
                self.seconds += time.perf_counter() - start
                self.calls += 1
                self._active = False

        return timed_allows
