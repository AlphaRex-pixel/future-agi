"""Integer settings read from environment variables.

Stdlib and django.core.exceptions only, so settings.py and the Django-free
CLIs (the outbox CDC installer) can use it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from django.core.exceptions import ImproperlyConfigured


def env_int(
    name: str,
    default: int,
    *,
    env: Mapping[str, str] | None = None,
    minimum: int | None = None,
) -> int:
    """``name`` from ``env`` (default ``os.environ``) as an int.

    Unset or blank means ``default``. A value that is not an integer, or is
    below ``minimum``, raises ImproperlyConfigured naming the variable: never
    a bare ValueError, and never a silent fallback to the default.
    """
    raw = (os.environ if env is None else env).get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ImproperlyConfigured(f"{name} must be an integer, not {raw!r}") from None
    if minimum is not None and value < minimum:
        raise ImproperlyConfigured(f"{name} must be at least {minimum}, not {value}")
    return value
