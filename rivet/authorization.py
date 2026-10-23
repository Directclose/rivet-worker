from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class AuthorizationError(PermissionError):
    pass


def _public_addresses(host: str) -> bool:
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise AuthorizationError("source host cannot be resolved") from exc
    if not addresses:
        return False
    for value in addresses:
        address = ipaddress.ip_address(value)
        if address.is_private or address.is_loopback or address.is_link_local or address.is_multicast or address.is_reserved:
            return False
    return True


def authorize_source(source: dict, requested_url: str, *, resolve_dns: bool = True) -> None:
    if not source.get("ingest_authorized"):
        raise AuthorizationError("source is not authorized for ingestion")
    parsed = urlparse(requested_url)
    registered = urlparse(source["source_url"])
    if parsed.scheme != "https" or not parsed.hostname:
        raise AuthorizationError("only HTTPS source URLs are accepted")
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        raise AuthorizationError("credentials and custom ports are not accepted in source URLs")
    allowed_host = source["allowed_host"].strip().lower()
    if parsed.hostname.lower() != allowed_host or registered.hostname != parsed.hostname:
        raise AuthorizationError("requested URL does not match the registered source host")
    if requested_url != source["source_url"]:
        raise AuthorizationError("requested URL must exactly match the authorized source URL")
    if resolve_dns and not _public_addresses(parsed.hostname):
        raise AuthorizationError("source host must resolve only to public addresses")

