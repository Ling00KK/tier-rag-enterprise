import ipaddress
import os
import socket
from urllib.parse import urlsplit


def _allowed_hosts():
    return {value.strip().lower().rstrip(".") for value in os.getenv("ONLINE_SOURCE_ALLOWED_HOSTS", "").split(",") if value.strip()}


def validate_outbound_url(url):
    """Reject credential-bearing and local/private online-source URLs.

    Explicit hosts in ONLINE_SOURCE_ALLOWED_HOSTS may be used for approved private
    company services. Validation is repeated after redirects by the caller.
    """
    parsed = urlsplit(str(url))
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise RuntimeError("在线资料地址必须是无内嵌账号密码的 HTTPS URL")
    host = parsed.hostname.lower().rstrip(".")
    if host in _allowed_hosts():
        return url
    if host == "localhost" or host.endswith(".localhost"):
        raise RuntimeError("在线资料地址不能指向本机或内网")
    try:
        addresses = {item[4][0].split("%")[0] for item in socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)}
    except OSError as error:
        raise RuntimeError("在线资料地址无法解析") from error
    if not addresses:
        raise RuntimeError("在线资料地址无法解析")
    for value in addresses:
        address = ipaddress.ip_address(value)
        if not address.is_global:
            raise RuntimeError("在线资料地址不能指向本机、内网或保留地址")
    return url


def csv_safe(value):
    text = "" if value is None else str(value)
    if text.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + text
    return text
