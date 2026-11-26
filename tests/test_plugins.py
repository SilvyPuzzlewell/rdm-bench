from rdmbench.plugins.allows import Plugin


def test_allows_counts_a_check_once_even_when_it_calls_another_allows():
    class Permission:
        def allows(self, identity):
            return identity == "ok"

    class Policy(Permission):
        def allows(self, identity):
            return super().allows(identity)

    plugin = Plugin()
    for cls in (Permission, Policy):
        cls.allows = plugin._wrap(cls.__dict__["allows"])

    assert Policy().allows("ok") is True
    assert Permission().allows("denied") is False
    assert plugin.totals()["allows_calls"] == 2
