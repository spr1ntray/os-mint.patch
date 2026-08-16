from __future__ import annotations

from urllib.parse import quote, urlparse


def proxy_to_url(raw: str) -> str:
    """Convert Hub proxy formats to an httpx proxy URL.

    Accepts:
    - host:port:user:password
    - http://user:password@host:port
    - socks5://user:password@host:port
    """
    value = (raw or "").strip()
    if not value:
        raise ValueError("empty_proxy")

    if "://" in value:
        parsed = urlparse(value)
        if not parsed.hostname or not parsed.port:
            raise ValueError("invalid_proxy_url")
        scheme = parsed.scheme or "http"
        if parsed.username is not None:
            user = quote(parsed.username, safe="")
            password = quote(parsed.password or "", safe="")
            return f"{scheme}://{user}:{password}@{parsed.hostname}:{parsed.port}"
        return f"{scheme}://{parsed.hostname}:{parsed.port}"

    parts = value.split(":")
    if len(parts) == 2:
        host, port = parts
        if not host or not port.isdigit():
            raise ValueError("invalid_proxy")
        return f"http://{host}:{port}"
    if len(parts) == 4:
        host, port, user, password = parts
        if not host or not port.isdigit() or not user:
            raise ValueError("invalid_proxy")
        return (
            f"http://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}"
        )
    if len(parts) > 4:
        host, port, user = parts[0], parts[1], parts[2]
        password = ":".join(parts[3:])
        if not host or not port.isdigit() or not user:
            raise ValueError("invalid_proxy")
        return (
            f"http://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}"
        )
    raise ValueError("invalid_proxy")
