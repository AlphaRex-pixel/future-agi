"""``check_if_dataset_creation_is_allowed`` must reach the entitlements service.

It imported ``usage.services.entitlements``, a module that does not exist, so
every dataset creation logged an ImportError traceback and returned "allowed"
from the except branch without checking anything.
"""

from unittest.mock import patch

import pytest

from ee.usage.schemas.events import CheckResult
from ee.usage.utils.usage_entries import check_if_dataset_creation_is_allowed

ENTITLEMENTS = "ee.usage.services.entitlements.Entitlements"


@pytest.mark.django_db
def test_configured_dataset_limit_is_enforced(organization):
    denied = CheckResult(
        allowed=False, error_code="ENTITLEMENT_LIMIT", limit=3, current_usage=3
    )
    with (
        patch(f"{ENTITLEMENTS}.get_entitlement", return_value=3),
        patch(f"{ENTITLEMENTS}.can_create", return_value=denied) as can_create,
        patch("ee.usage.utils.usage_entries.logger") as logger,
    ):
        allowed, detail = check_if_dataset_creation_is_allowed(organization)

    assert allowed is False
    assert detail["limit"] == 3
    can_create.assert_called_once_with(str(organization.id), "datasets", 0)
    logger.exception.assert_not_called()


@pytest.mark.django_db
def test_dataset_within_limit_is_allowed(organization):
    with (
        patch(f"{ENTITLEMENTS}.get_entitlement", return_value=3),
        patch(
            f"{ENTITLEMENTS}.can_create", return_value=CheckResult(allowed=True)
        ) as can_create,
    ):
        assert check_if_dataset_creation_is_allowed(organization) == (True, {})

    can_create.assert_called_once()


@pytest.mark.django_db
def test_unconfigured_dataset_entitlement_stays_uncapped(organization):
    """No plan sets "datasets" yet; get_limit() would read that as 0 and deny."""
    with (
        patch(f"{ENTITLEMENTS}.get_entitlement", return_value=None),
        patch(f"{ENTITLEMENTS}.can_create") as can_create,
    ):
        assert check_if_dataset_creation_is_allowed(organization) == (True, {})

    can_create.assert_not_called()
