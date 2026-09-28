"""USAGE_EVENTS_MAX_LEN, the cap on the usage:events billing stream.

Read by settings.py the way fi-collector reads it (cmd/fi-collector/main.go):
blank is the default, anything but a positive integer refuses to start. 0
would trim events the consumer has not read yet, and a negative cap makes
every XADD fail, which the emitter only logs.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

BACKEND = Path(__file__).resolve().parents[2]
READ_SETTING = (
    "import importlib, os; "
    "s = importlib.import_module(os.environ['DJANGO_SETTINGS_MODULE']); "
    "print(s.USAGE_EVENTS_MAX_LEN)"
)


def _load_settings(value):
    return subprocess.run(
        [sys.executable, "-c", READ_SETTING],
        env={**os.environ, "USAGE_EVENTS_MAX_LEN": value},
        cwd=BACKEND,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.parametrize("value", ["", " "])
def test_unset_cap_is_one_million(value):
    loaded = _load_settings(value)
    assert loaded.returncode == 0, loaded.stderr
    assert loaded.stdout.splitlines()[-1] == "1000000"


def test_a_positive_cap_is_used():
    loaded = _load_settings("250000")
    assert loaded.returncode == 0, loaded.stderr
    assert loaded.stdout.splitlines()[-1] == "250000"


@pytest.mark.parametrize("value", ["0", "-1", "lots"])
def test_anything_else_refuses_to_load(value):
    loaded = _load_settings(value)
    assert loaded.returncode != 0
    assert "ImproperlyConfigured: USAGE_EVENTS_MAX_LEN must be" in loaded.stderr
