"""Which provider base URLs model discovery may call, with and without the
AGENTCC_ALLOW_PRIVATE_PROVIDER_URLS opt-in."""

import socket
from unittest.mock import patch

import pytest

from agentcc.services.url_safety import (
    ALLOW_PRIVATE_PROVIDER_URLS_ENV,
    PROVIDER_PRIVATE_URL_ERROR,
    PROVIDER_URL_ERROR,
    WEBHOOK_PRIVATE_URL_ERROR,
    ensure_provider_base_url_allowed,
    ensure_public_http_url,
    private_provider_urls_allowed,
)

_DNS = {
    "api.openai.com": ["104.18.6.192"],
    "mock-llm": ["172.20.0.5"],
    "host.docker.internal": ["192.168.65.254"],
    "tailnet-box": ["100.101.102.103"],
    "ula-box": ["fd12:3456::1"],
    "localhost": ["127.0.0.1", "::1"],
    "split-horizon": ["104.18.6.192", "10.0.0.12"],
    "rebind-to-metadata": ["169.254.169.254"],
}


def _fake_getaddrinfo(host, *args, **kwargs):
    try:
        addrs = [host] if host[0].isdigit() or ":" in host else _DNS[host]
    except KeyError:
        raise socket.gaierror(socket.EAI_NONAME, "no such host") from None
    return [
        (socket.AF_INET6 if ":" in a else socket.AF_INET, 0, 0, "", (a, 0))
        for a in addrs
    ]


@pytest.fixture(autouse=True)
def _stub_dns():
    with patch(
        "agentcc.services.url_safety.socket.getaddrinfo", side_effect=_fake_getaddrinfo
    ) as stub:
        yield stub


def _check(url, allow_private):
    """None if the URL passes, else the error message."""
    try:
        ensure_public_http_url(
            url,
            PROVIDER_URL_ERROR,
            allow_private=allow_private,
            private_message=PROVIDER_PRIVATE_URL_ERROR,
        )
    except ValueError as e:
        return str(e)
    return None


PUBLIC = [
    "https://api.openai.com/v1",
    "http://8.8.8.8:8080",
]
PRIVATE = [
    "http://mock-llm:8080",
    "http://host.docker.internal:11434",
    "http://10.1.2.3",
    "http://172.31.255.254",
    "http://192.168.1.10:8000",
    "http://100.64.0.1",
    "http://tailnet-box",
    "http://[fd12:3456::1]:8000",
    "http://ula-box",
    "http://split-horizon",
]
NEVER = [
    "http://127.0.0.1:8080",
    "http://localhost:11434",
    "http://[::1]:8080",
    "http://[::ffff:127.0.0.1]",
    "http://169.254.169.254/latest/meta-data",
    "http://[::ffff:169.254.169.254]",
    "http://[fe80::1]",
    "http://metadata.google.internal/computeMetadata/v1",
    "http://METADATA.google.internal.",
    "http://rebind-to-metadata",
    "http://100.100.100.200",
    "http://[fd00:ec2::254]",
    "http://168.63.129.16",
    "http://0.0.0.0:8080",
    "http://224.0.0.1",
    "http://255.255.255.255",
    "http://no-such-host.invalid",
    "ftp://api.openai.com",
    "http://mock-llm:6379",  # blocked port
]


@pytest.mark.parametrize("url", PUBLIC)
def test_public_urls_pass(url):
    assert _check(url, allow_private=False) is None
    assert _check(url, allow_private=True) is None


@pytest.mark.parametrize("url", PRIVATE)
def test_private_urls_need_the_opt_in(url):
    assert _check(url, allow_private=False) == PROVIDER_PRIVATE_URL_ERROR
    assert _check(url, allow_private=True) is None


@pytest.mark.parametrize("url", NEVER)
def test_loopback_metadata_and_unusable_urls_never_pass(url):
    assert _check(url, allow_private=False) == PROVIDER_URL_ERROR
    assert _check(url, allow_private=True) == PROVIDER_URL_ERROR


def test_private_url_error_names_the_opt_in():
    assert f"{ALLOW_PRIVATE_PROVIDER_URLS_ENV}=true" in PROVIDER_PRIVATE_URL_ERROR


def test_save_error_does_not_ask_for_a_resolvable_host(monkeypatch):
    # Saving accepts a host that does not resolve here, so its error must not
    # say the host has to resolve.
    monkeypatch.delenv(ALLOW_PRIVATE_PROVIDER_URLS_ENV, raising=False)
    ensure_provider_base_url_allowed("http://no-such-host.invalid")
    with pytest.raises(ValueError) as refused:
        ensure_provider_base_url_allowed("http://127.0.0.1:8080")
    assert "never allowed" in str(refused.value)
    assert "resolve" not in str(refused.value)


def test_metadata_host_is_refused_before_any_lookup(_stub_dns):
    assert _check("http://metadata.google.internal", allow_private=True)
    _stub_dns.assert_not_called()


def test_webhook_urls_stay_strict():
    # Webhooks never take the opt-in: private and CGNAT hosts stay refused.
    for url in ("http://mock-llm:8080", "http://tailnet-box"):
        with pytest.raises(ValueError, match=WEBHOOK_PRIVATE_URL_ERROR):
            ensure_public_http_url(url, WEBHOOK_PRIVATE_URL_ERROR)


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, False),
        ("", False),
        ("true", True),
        ("TRUE", True),
        ("1", True),
        ("false", False),
        ("0", False),
        ("yes-please", False),
    ],
)
def test_opt_in_env_parsing(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv(ALLOW_PRIVATE_PROVIDER_URLS_ENV, raising=False)
    else:
        monkeypatch.setenv(ALLOW_PRIVATE_PROVIDER_URLS_ENV, value)
    assert private_provider_urls_allowed() is expected
