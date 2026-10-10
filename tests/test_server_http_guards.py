"""The HTTP handler's request guards: Host check (DNS rebinding) and same-origin check (CSRF)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def _handler(srv, headers, port=6161):
    h = srv.Handler.__new__(srv.Handler)
    h.headers = headers
    h.server = SimpleNamespace(server_address=("127.0.0.1", port))
    return h


class TestLocalHost:
    """Only requests addressed to a loopback host name are served."""

    @pytest.mark.parametrize(
        "host, allowed",
        [
            ("localhost:6161", True),
            ("127.0.0.1:6161", True),
            ("[::1]:6161", True),
            ("localhost", True),
            ("evil.example:6161", False),
            ("localhost.evil.example:6161", False),
            ("0.0.0.0:6161", False),
            ("", False),
            (None, False),
        ],
    )
    def test_host_header(self, srv, host, allowed):
        headers = {} if host is None else {"Host": host}
        assert _handler(srv, headers)._local_host() is allowed


class TestSameOrigin:
    """Actions need a JSON body and, when the browser sends an Origin, this exact origin."""

    @pytest.mark.parametrize(
        "origin, content_type, port, allowed",
        [
            (None, "application/json", 6161, True),
            (None, "application/json; charset=utf-8", 6161, True),
            (None, "text/plain", 6161, False),
            (None, None, 6161, False),
            ("http://localhost:6161", "application/json", 6161, True),
            ("http://127.0.0.1:6161", "application/json", 6161, True),
            ("http://localhost:6161", "text/plain", 6161, False),
            ("http://localhost:6162", "application/json", 6161, False),
            ("http://localhost:7000", "application/json", 7000, True),
            ("http://localhost", "application/json", 6161, False),
            ("https://evil.example", "application/json", 6161, False),
            ("http://localhost.evil.example:6161", "application/json", 6161, False),
            ("null", "application/json", 6161, False),
        ],
    )
    def test_origin_and_content_type(self, srv, origin, content_type, port, allowed):
        headers = {k: v for k, v in (("Origin", origin), ("Content-Type", content_type)) if v is not None}
        assert _handler(srv, headers, port)._same_origin() is allowed
