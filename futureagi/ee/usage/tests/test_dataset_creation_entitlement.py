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
IS_CLOUD = "ee.usage.services.entitlements.DeploymentMode.is_cloud"


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
def test_unconfigured_dataset_limit_fails_open_on_cloud_and_logs_it(organization):
    """No plan sets "datasets" yet; can_create() reads that as "not on your plan"."""
    with (
        patch(IS_CLOUD, return_value=True),
        patch(f"{ENTITLEMENTS}.get_entitlement", return_value=None),
        # Reads billing.yaml, which only the private cloud overlay ships.
        patch("ee.usage.services.entitlements._find_upgrade_cta", return_value=None),
        patch("ee.usage.utils.usage_entries.logger") as logger,
    ):
        assert check_if_dataset_creation_is_allowed(organization) == (True, {})

    logger.warning.assert_called_once_with(
        "dataset_limit_unconfigured_allowing", organization_id=str(organization.id)
    )
    logger.exception.assert_not_called()


@pytest.mark.django_db
def test_self_hosted_dataset_creation_is_uncapped_without_a_warning(organization):
    with (
        patch(IS_CLOUD, return_value=False),
        patch("ee.usage.utils.usage_entries.logger") as logger,
    ):
        assert check_if_dataset_creation_is_allowed(organization) == (True, {})

    logger.warning.assert_not_called()
    logger.exception.assert_not_called()
