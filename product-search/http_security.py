"""Validate configured destinations before attaching service credentials."""
from ipaddress import ip_address

import httpx


def service_url(value: str, *, allow_insecure_http: bool = False) -> httpx.URL:
    try:
        if any(ord(c) < 33 or ord(c) == 127 for c in value):
            raise ValueError
        url = httpx.URL(value)
        if (url.scheme not in ('http', 'https') or not url.host
                or url.userinfo or url.query or url.fragment):
            raise ValueError
    except (ValueError, httpx.InvalidURL):
        raise ValueError('Service URL must be HTTP(S) without credentials, query or fragment') from None
    try:
        loopback = ip_address(url.host).is_loopback
    except ValueError:
        loopback = url.host == 'localhost'
    if url.scheme != 'https' and not loopback and not allow_insecure_http:
        raise ValueError('Service URL requires HTTPS; allow_insecure_http is only for a trusted private network')
    return url


def bearer_token(value: str) -> str:
    if not value or any(not 33 <= ord(c) <= 126 for c in value):
        raise ValueError('Token must be non-empty printable ASCII without whitespace')
    return value
