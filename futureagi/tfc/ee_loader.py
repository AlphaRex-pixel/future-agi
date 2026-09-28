import importlib.util
import os


def has_ee(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ModuleNotFoundError, ValueError):
        return False


def usage_event_consumer_available() -> bool:
    """Whether this code ships the consumer of the usage:events Redis stream:
    Future AGI Cloud's UsageConsumerWorkflow, which the Temporal worker loads
    from the same modules (tfc/temporal/common/registry.py)."""
    return has_ee("ee.cloud.temporal") or has_ee("ee.usage.temporal")


def _is_oss_mode() -> bool:
    """Env-var-based OSS detection for use during Django settings load.

    Mirrors ee.usage.deployment.DeploymentMode precedence but doesn't go
    through django.conf.settings (which isn't fully populated while
    settings.py is still executing). Runtime code should use
    DeploymentMode.is_oss() instead — this is for the app-registration gate.
    """
    if os.environ.get("CLOUD_DEPLOYMENT", "") in ("US", "EU", "DEV"):
        return False
    if os.environ.get("EE_LICENSE_KEY", ""):
        return False
    return True


def ee_feature_enabled(module: str) -> bool:
    """True iff an EE feature module should be wired up.

    Requires both:
      - module is importable (code present on disk), and
      - deployment is not OSS (env vars indicate EE or Cloud).

    `ee.usage` itself is exempt — it provides DeploymentMode, so it's gated
    on presence only via `has_ee("ee.usage")`.
    """
    return has_ee(module) and not _is_oss_mode()
