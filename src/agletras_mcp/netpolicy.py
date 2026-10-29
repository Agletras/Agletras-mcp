"""URL/host safety policy for outbound fetches (SSRF guard).

Every outbound http(s) request made by this server — ``fetch_url``, PDF URL
downloads, and the search providers — passes through this module:

- only ``http``/``https`` URLs without embedded userinfo are accepted;
- hostnames are DNS-resolved and *every* resolved address is checked against
  a blocklist covering loopback, private, link-local, multicast, reserved,
  and shared (CGNAT) ranges — which includes the cloud-metadata endpoint
  ``169.254.169.254``;
- each redirect destination is validated the same way *before* it is
  followed (:class:`SafeRedirectHandler`), with a hard chain-length cap;
- an optional allowlist (``AGLETRAS_ALLOWED_DOMAINS``) restricts fetches to
  a comma-separated set of domains (suffix match, subdomains included);
- ``AGLETRAS_ALLOW_PRIVATE_HOSTS=1`` opts out of the private-address block
  for trusted local testing against development servers. Never enable it
  for untrusted input.

Known limitation, accepted for a local research tool and documented in the
README: urllib re-resolves DNS at connect time, so a rebinding DNS server
could still connect to a private address *after* validation. Eliminating
that racing window requires pinning the connection to the validated IP,
which the standard library does not support. Validation still blocks every
ordinary SSRF target (localhost, LAN, metadata services, literal IPs).
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import urllib.parse
import urllib.request
from ipaddress import IPv4Address, IPv6Address
from typing import Any

from .config import Settings
from .errors import ToolError

LOGGER = logging.getLogger(__name__)

_BLOCKED_HOSTNAMES = frozenset({"localhost"})
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal")

# Belt-and-braces blocklist. Python's ``is_global``/``is_private`` flags cover
# most of these on 3.13+, but special-use ranges have shifted between versions,
# so the interesting ones are pinned explicitly too.
_EXTRA_BLOCKED_NETS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "0.0.0.0/8",            # "this network" (RFC 791)
        "100.64.0.0/10",        # shared address space / CGNAT (RFC 6598)
        "192.0.0.0/24",         # IETF protocol assignments (RFC 6890)
        "192.0.2.0/24",         # TEST-NET-1 (RFC 5737)
        "198.18.0.0/15",        # benchmarking (RFC 2544)
        "198.51.100.0/24",      # TEST-NET-2
        "203.0.113.0/24",       # TEST-NET-3
        "2001:db8::/32",        # IPv6 documentation
    )
)


def ip_is_blocked(ip: IPv4Address | IPv6Address) -> bool:
    """True for loopback/private/link-local/reserved/multicast/CGNAT addresses.

    IPv4-mapped IPv6 addresses (``::ffff:10.0.0.1``) are normalized to their
    IPv4 form first so they cannot smuggle a private address past the check.
    """
    if isinstance(ip, IPv6Address):
        mapped = ip.ipv4_mapped
        if mapped is not None:
            ip = mapped
    if ip.is_multicast or not ip.is_global:
        return True
    return any(ip in net for net in _EXTRA_BLOCKED_NETS)


def hostname_is_blocked(hostname: str) -> bool:
    """True for ``localhost`` and other names that must never be fetched."""
    host = hostname.strip().rstrip(".").lower()
    if host in _BLOCKED_HOSTNAMES or host.endswith(_BLOCKED_SUFFIXES):
        return True
    return False


def domain_is_allowed(hostname: str, allowed_domains: tuple[str, ...]) -> bool:
    """Suffix match against an allowlist (``example.com`` allows subdomains)."""
    host = hostname.strip().rstrip(".").lower()
    return any(host == domain or host.endswith("." + domain) for domain in allowed_domains)


def _resolved_addresses(hostname: str) -> list[str]:
    """All addresses DNS returns for *hostname* (any family)."""
    infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


def check_host(hostname: str, settings: Settings) -> None:
    """Validate *hostname*: allowlist, name denylist, and resolved addresses.

    Raises :class:`ToolError` (``invalid_argument``, ``blocked_host``, or
    ``network_error``) when the host must not be contacted.
    """
    host = (hostname or "").strip().rstrip(".").lower()
    if not host:
        raise ToolError("invalid_argument", "URL has no hostname")
    if settings.allowed_domains and not domain_is_allowed(host, settings.allowed_domains):
        raise ToolError(
            "blocked_host",
            f"Host {host!r} is not in the allowed domains: "
            + ", ".join(settings.allowed_domains),
        )
    if settings.allow_private_hosts:
        LOGGER.debug("private-host fetch permitted by AGLETRAS_ALLOW_PRIVATE_HOSTS: %r", host)
        return
    if hostname_is_blocked(host):
        raise ToolError("blocked_host", f"Refusing to fetch blocked hostname {host!r}")

    # Literal IP host: validate directly (no DNS round-trip needed).
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if ip_is_blocked(literal):
            raise ToolError("blocked_host", f"Refusing to fetch private/reserved address {host!r}")
        return

    try:
        addresses = _resolved_addresses(host)
    except socket.gaierror as exc:
        raise ToolError("network_error", f"DNS lookup failed for {host!r}: {exc}") from exc
    except OSError as exc:
        raise ToolError("network_error", f"Address resolution failed for {host!r}: {exc}") from exc
    if not addresses:
        raise ToolError("network_error", f"DNS returned no addresses for {host!r}")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise ToolError(
                "network_error", f"DNS returned an unparseable address for {host!r}: {address!r}"
            ) from exc
        if ip_is_blocked(ip):
            raise ToolError(
                "blocked_host",
                f"{host!r} resolves to a private/loopback/link-local or otherwise "
                f"blocked address ({address}); refusing to fetch it",
            )


def assert_safe_url(url: str, settings: Settings) -> urllib.parse.ParseResult:
    """Validate *url* against the SSRF policy. Returns the parsed URL.

    Raises :class:`ToolError` for unsupported schemes, embedded credentials,
    or disallowed hosts. Call this for the initial URL; redirects are
    re-validated per hop by :class:`SafeRedirectHandler`.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ToolError("invalid_argument", f"Only http/https URLs are supported: {url!r}")
    if parsed.username or parsed.password:
        raise ToolError("invalid_argument", f"URLs with embedded credentials are not allowed: {url!r}")
    check_host(parsed.hostname or "", settings)
    return parsed


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirect handler that validates every destination before following it.

    Also enforces a hard redirect-chain cap because stdlib urllib does not.
    """

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._settings = settings

    def redirect_request(  # type: ignore[override] - signature matches stdlib
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        hops = int(getattr(req, "_agletras_hops", 0))
        if hops >= self._settings.max_redirects:
            raise ToolError(
                "too_many_redirects",
                f"Exceeded {self._settings.max_redirects} redirects while fetching "
                f"{req.full_url!r}",
            )
        # Validate scheme, credentials, and the new host's addresses *before*
        # any connection is made to the redirect target.
        assert_safe_url(newurl, self._settings)
        request = super().redirect_request(req, fp, code, msg, headers, newurl)
        if request is not None:
            request._agletras_hops = hops + 1  # type: ignore[attr-defined]
            LOGGER.info("redirect %d -> %s", code, newurl)
        return request
