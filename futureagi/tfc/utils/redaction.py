"""Keep credentials that ride inside connection URLs out of log lines."""

import re

# scheme://user:password@ -- RFC 3986 userinfo cannot hold an unencoded
# "/", "?", "#" or "@", so the match stops before the host, path or query.
_URL_PASSWORD = re.compile(
    r"(?P<head>[A-Za-z][A-Za-z0-9+.\-]*://[^:/?#@\s]*):[^/?#@\s]*@"
)


def redact_url_credentials(text: str) -> str:
    """Mask the password of every ``scheme://user:password@`` URL in ``text``.

    The user, host, port and path stay readable for debugging:
    ``redis://:s3cret@redis:6379/2`` becomes ``redis://:***@redis:6379/2``.
    """
    return _URL_PASSWORD.sub(r"\g<head>:***@", text)
