"""``manage.py create_user``: the first account of an install, which
./bin/install runs and the Helm install notes tell the operator to run."""

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from accounts.models import User


@pytest.mark.parametrize(
    ("password", "reason"),
    # Sign-up trims the password before it checks it.
    [("password", "too common"), ("1234567 ", "too short")],
)
def test_create_user_refuses_a_password_the_validators_reject(db, password, reason):
    with pytest.raises(CommandError, match=reason):
        call_command(
            "create_user",
            "--email",
            "owner@example.com",
            "--name",
            "Owner",
            "--password",
            password,
        )

    assert not User.objects.filter(email="owner@example.com").exists()
