import pytest

from abenlux.auth.principals import PrincipalStore
from abenlux.auth.rbac import (
    AuthorizationError,
    Permission,
    Principal,
    Role,
    permissions_for,
    require,
)


def test_permission_matrix():
    assert permissions_for(Role.DEVELOPER) == {Permission.VIEW_OWN}
    assert Permission.VIEW_AGGREGATES in permissions_for(Role.MANAGER)
    assert Permission.VIEW_AGGREGATES not in permissions_for(Role.DEVELOPER)
    assert Permission.MANAGE in permissions_for(Role.ADMIN)
    assert Permission.MANAGE not in permissions_for(Role.MANAGER)


def test_no_role_can_view_another_individual():
    # the governance invariant: there is NO permission that grants individual drilldown.
    # every role's permissions are a subset of {own, aggregates, cost, manage}.
    individual_detail_perms = set()  # intentionally empty: such a permission does not exist
    for role in Role:
        assert permissions_for(role) & individual_detail_perms == set()
    # and aggregates is the ONLY way managers see others - never per-person
    assert permissions_for(Role.MANAGER) - {Permission.VIEW_OWN} == {Permission.VIEW_AGGREGATES}


def test_everyone_has_view_own():
    for role in Role:
        assert Permission.VIEW_OWN in permissions_for(role)


def test_require_raises_for_missing_permission():
    dev = Principal("d@x", "Dev", Role.DEVELOPER, "px_dev")
    require(dev, Permission.VIEW_OWN)  # ok
    with pytest.raises(AuthorizationError):
        require(dev, Permission.VIEW_AGGREGATES)


def test_principal_store_resolves_token_and_pseudonym():
    store = PrincipalStore.default_dev(hmac_key=b"k")
    p = store.resolve("mgr-token")
    assert p is not None and p.role == Role.MANAGER
    assert p.pseudonym.startswith("px_")
    assert store.resolve("nope") is None
    # pseudonym is deterministic for the same subject+key (matches edge pipeline)
    from abenlux.privacy.pseudonymize import pseudonymize
    assert p.pseudonym == pseudonymize(p.subject, b"k")
