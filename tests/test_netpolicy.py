"""Tests for the SSRF netpolicy module: literal IPs, DNS resolution,
allowlist, name denylist, redirect validation, and the explicit bypass."""

from __future__ import annotations

import socket
import unittest
import urllib.parse
from unittest import mock

from agletras_mcp import netpolicy
from agletras_mcp.config import load_settings
from agletras_mcp.errors import ToolError

from tests.support import StrictHostTestsMixin, TempHomeCase

PRIVATE_LITERALS = (
    "127.0.0.1",      # loopback
    "127.0.0.2",      # loopback (whole /8)
    "10.1.2.3",       # private
    "172.16.0.9",     # private (172.16/12)
    "192.168.1.1",    # private
    "169.254.169.254",  # link-local (cloud metadata)
    "0.0.0.0",        # this-network
    "100.64.0.1",     # shared/CGNAT
    "192.0.2.77",     # TEST-NET-1
    "198.18.0.5",     # benchmarking
    "224.0.0.5",      # multicast
    "240.0.0.1",      # reserved
    "::1",            # IPv6 loopback
    "fe80::1",        # IPv6 link-local
    "fc00::1234",     # IPv6 unique-local
    "::",             # unspecified
    "ff02::1",        # IPv6 multicast
    "::ffff:10.0.0.9",  # IPv4-mapped private smuggled via IPv6
)

PUBLIC_LITERALS = (
    "93.184.216.34",  # example.com's historic address
    "8.8.8.8",
    "2606:2800:220:1:248:1893:25c8:1946",  # public IPv6
)


class LiteralIPTests(StrictHostTestsMixin, TempHomeCase):
    def _check(self, host: str) -> None:
        netpolicy.check_host(host, load_settings())

    def test_private_literal_ips_blocked(self) -> None:
        for host in PRIVATE_LITERALS:
            with self.assertRaises(ToolError) as ctx:
                self._check(host)
            self.assertEqual(ctx.exception.code, "blocked_host", msg=host)

    def test_public_literal_ips_allowed(self) -> None:
        for host in PUBLIC_LITERALS:
            self._check(host)  # no exception


class LocalhostNameTests(StrictHostTestsMixin, TempHomeCase):
    def test_localhost_names_blocked(self) -> None:
        for host in ("localhost", "localhost.", "sub.localhost", "myserver.local", "db.internal"):
            with self.assertRaises(ToolError) as ctx:
                netpolicy.check_host(host, load_settings())
            self.assertEqual(ctx.exception.code, "blocked_host", msg=host)

    def test_bypass_env_permits_private_hosts(self) -> None:
        import os
        os.environ["AGLETRAS_ALLOW_PRIVATE_HOSTS"] = "1"
        try:
            netpolicy.check_host("127.0.0.1", load_settings())
            netpolicy.check_host("localhost", load_settings())
        finally:
            os.environ["AGLETRAS_ALLOW_PRIVATE_HOSTS"] = "0"


class DnsResolutionTests(StrictHostTestsMixin, TempHomeCase):
    """Hostname checks resolve DNS and validate every returned address."""

    def test_dns_private_address_blocked(self) -> None:
        infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", 443))]
        with mock.patch.object(netpolicy.socket, "getaddrinfo", return_value=infos):
            with self.assertRaises(ToolError) as ctx:
                netpolicy.check_host("evil.example.com", load_settings())
        self.assertEqual(ctx.exception.code, "blocked_host")
        self.assertIn("10.0.0.7", ctx.exception.message)

    def test_dns_metadata_address_blocked(self) -> None:
        infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 80))]
        with mock.patch.object(netpolicy.socket, "getaddrinfo", return_value=infos):
            with self.assertRaises(ToolError) as ctx:
                netpolicy.check_host("rebind.example.com", load_settings())
        self.assertEqual(ctx.exception.code, "blocked_host")

    def test_dns_public_address_allowed(self) -> None:
        infos = [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:2800:220:1:248:1893:25c8:1946", 0, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        ]
        with mock.patch.object(netpolicy.socket, "getaddrinfo", return_value=infos):
            netpolicy.check_host("example.com", load_settings())

    def test_dns_failure_is_network_error(self) -> None:
        with mock.patch.object(
            netpolicy.socket, "getaddrinfo", side_effect=socket.gaierror(-2, "Name or service not known")
        ):
            with self.assertRaises(ToolError) as ctx:
                netpolicy.check_host("does-not-exist.invalid", load_settings())
        self.assertEqual(ctx.exception.code, "network_error")


class AllowedDomainTests(StrictHostTestsMixin, TempHomeCase):
    def setUp(self) -> None:
        super().setUp()
        import os
        os.environ["AGLETRAS_ALLOWED_DOMAINS"] = "example.com, arxiv.org"

    def test_allowlist_permits_exact_and_subdomains(self) -> None:
        import os
        os.environ["AGLETRAS_ALLOW_PRIVATE_HOSTS"] = "0"  # allowlist alone must not bypass DNS
        with mock.patch.object(
            netpolicy.socket, "getaddrinfo", side_effect=socket.gaierror("unused")
        ):
            # example.com is allowed but then DNS fails -> network_error (not blocked_host)
            with self.assertRaises(ToolError) as ctx:
                netpolicy.check_host("example.com", load_settings())
            self.assertEqual(ctx.exception.code, "network_error")
            with self.assertRaises(ToolError) as ctx:
                netpolicy.check_host("blog.example.com", load_settings())
            self.assertEqual(ctx.exception.code, "network_error")

    def test_allowlist_blocks_other_domains_before_dns(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            netpolicy.check_host("evil.net", load_settings())
        self.assertEqual(ctx.exception.code, "blocked_host")
        with self.assertRaises(ToolError) as ctx:
            netpolicy.check_host("notexample.com", load_settings())
        self.assertEqual(ctx.exception.code, "blocked_host")


class AssertSafeUrlTests(TempHomeCase):
    def test_non_http_schemes_rejected(self) -> None:
        for url in ("ftp://example.com/x", "file:///etc/passwd", "gopher://h"):
            with self.assertRaises(ToolError) as ctx:
                netpolicy.assert_safe_url(url, load_settings())
            self.assertEqual(ctx.exception.code, "invalid_argument", msg=url)

    def test_userinfo_rejected(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            netpolicy.assert_safe_url("https://user:pass@example.com/", load_settings())
        self.assertEqual(ctx.exception.code, "invalid_argument")

    def test_safe_url_passes_through(self) -> None:
        parsed = netpolicy.assert_safe_url("https://example.com/page", load_settings())
        self.assertEqual(parsed.hostname, "example.com")

    def test_empty_host_rejected(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            netpolicy.assert_safe_url("https:///path", load_settings())
        self.assertEqual(ctx.exception.code, "invalid_argument")


class SafeRedirectHandlerTests(StrictHostTestsMixin, TempHomeCase):
    def _handler(self) -> netpolicy.SafeRedirectHandler:
        return netpolicy.SafeRedirectHandler(load_settings())

    def _redirect(self, target: str, hops: int = 0) -> urllib.request.Request:
        handler = self._handler()
        req = urllib.request.Request("http://example.com/start")
        req._agletras_hops = hops  # type: ignore[attr-defined]
        return handler.redirect_request(req, None, 302, "Found", {}, target)

    def test_public_redirect_allowed(self) -> None:
        request = self._redirect("https://example.org/next")
        self.assertEqual(request.full_url, "https://example.org/next")

    def test_private_redirect_blocked(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            self._redirect("http://169.254.169.254/latest/meta-data/")
        self.assertEqual(ctx.exception.code, "blocked_host")

    def test_localhost_redirect_blocked(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            self._redirect("http://localhost:8080/admin")
        self.assertEqual(ctx.exception.code, "blocked_host")

    def test_redirect_scheme_downgrade_blocked(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            self._redirect("ftp://example.org/file")
        self.assertEqual(ctx.exception.code, "invalid_argument")

    def test_redirect_chain_cap(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            self._redirect("https://example.org/next", hops=load_settings().max_redirects)
        self.assertEqual(ctx.exception.code, "too_many_redirects")


if __name__ == "__main__":
    unittest.main()
