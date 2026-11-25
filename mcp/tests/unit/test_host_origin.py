# Copyright (C) 2026 Khalid El-Ouiali — MONARK AIOPS srl
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
The Host and Origin guard in front of the MCP endpoint.

Why this file exists
--------------------
A server on loopback with authentication off — which loopback allows, and which a
SIGHUP reload can bring about later — is reachable by DNS rebinding: a web page
makes the victim's browser resolve its own name to 127.0.0.1, then talks to the
server as if it were same-origin. The browser cannot hide what it believes the
server is called, so the Host header still carries the attacker's domain, and
checking it closes the hole. FastMCP's `host_origin_protection="auto"` does that
check; `_serve` turns it on.

It also changes who can reach the server, so the *deployments* are pinned here,
not just the attack:

  - a client on the machine, by any loopback name, is unaffected;
  - a reverse proxy on the machine forwards its public name, so it has to be
    listed in `MCP_ALLOWED_HOSTS` — and without that it gets 421, which is the
    price of the default and is asserted rather than discovered;
  - a server bound to a routable interface and reached over it is left alone
    unless an allowlist is given, so a LAN deployment with API keys keeps working.

The decision FastMCP takes depends on the address the request *arrived on*
(`scope["server"]`), not on the address the process bound, so the requests here
are raw ASGI scopes: a test client reports a made-up server address and would
exercise none of it.
"""

from __future__ import annotations

import pytest
from starlette.middleware import Middleware

from niwashi_mcp import main
from niwashi_mcp.middleware.health import HealthMiddleware

_PORT = 8000


async def _status(
    app, *, host: str, arrived_on: str = "127.0.0.1", origin: str | None = None
) -> int:
    """Send GET /health through ``app`` and return the status code.

    `/health` is answered by `HealthMiddleware`, which sits inside the guard, so
    a 200 means the guard let the request in and anything else is the guard.
    """
    headers = [(b"host", host.encode())]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": "/health", "raw_path": b"/health",
        "query_string": b"", "headers": headers,
        "client": ("127.0.0.1", 50000), "server": (arrived_on, _PORT),
    }
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


async def _served_app(monkeypatch, **env):
    """The app `_serve` would run, built from the arguments it passes.

    `run_http_async` is intercepted rather than run, and its middleware, guard
    mode and allowlist are handed to `http_app` the way FastMCP does it — so what
    is tested is what `_serve` configured, not a copy of it. For a loopback bind
    FastMCP also appends the bind address to the allowlist; the guard accepts
    every loopback name regardless, so that is not reproduced.
    """
    for key in ("MCP_HOST", "MCP_API_KEYS", "MCP_ALLOW_NO_AUTH", "MCP_PORT", "MCP_ALLOWED_HOSTS"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(main, "load_dotenv", lambda *a, **k: None)

    captured: dict = {}

    async def fake_run(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(main.mcp, "run_http_async", fake_run)
    await main._serve()
    app = main.mcp.http_app(
        stateless_http=True,
        json_response=True,
        middleware=[Middleware(HealthMiddleware)],
        host_origin_protection=captured["host_origin_protection"],
        allowed_hosts=captured["allowed_hosts"],
    )
    return app, captured


class TestWhatServeConfigures:
    @pytest.mark.asyncio
    async def test_protection_is_auto_and_there_is_no_allowlist_by_default(self, monkeypatch):
        _, passed = await _served_app(monkeypatch)

        assert passed["host_origin_protection"] == "auto"
        assert passed["allowed_hosts"] is None

    @pytest.mark.asyncio
    async def test_the_allowlist_is_read_comma_separated_and_trimmed(self, monkeypatch):
        _, passed = await _served_app(
            monkeypatch, MCP_ALLOWED_HOSTS=" mcp.example.com , *.corp.example ,, "
        )

        assert passed["allowed_hosts"] == ["mcp.example.com", "*.corp.example"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("blank", ["", "  ", " , ,"])
    async def test_a_blank_allowlist_is_no_allowlist_not_an_empty_one(self, monkeypatch, blank):
        """FastMCP reads any list as explicit and then validates the Host on every
        bind; a blank variable must not switch that on by accident."""
        _, passed = await _served_app(monkeypatch, MCP_ALLOWED_HOSTS=blank)

        assert passed["allowed_hosts"] is None


class TestOnLoopback:
    """The default install: loopback bind, no proxy."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "host", ["127.0.0.1:8000", "localhost:8000", "localhost", "[::1]:8000"]
    )
    async def test_every_name_the_machine_has_for_itself_is_accepted(self, monkeypatch, host):
        app, _ = await _served_app(monkeypatch)

        assert await _status(app, host=host) == 200

    @pytest.mark.asyncio
    @pytest.mark.parametrize("host", ["attacker.example", "attacker.example:8000", "127.0.0.1.attacker.example"])
    async def test_a_rebound_name_is_refused(self, monkeypatch, host):
        """The attack: the browser connects to 127.0.0.1 but the Host is still
        the attacker's."""
        app, _ = await _served_app(monkeypatch)

        assert await _status(app, host=host) == 421

    @pytest.mark.asyncio
    async def test_a_web_page_on_another_origin_is_refused(self, monkeypatch):
        app, _ = await _served_app(monkeypatch)

        assert await _status(app, host="127.0.0.1:8000", origin="https://attacker.example") == 403

    @pytest.mark.asyncio
    async def test_a_local_page_is_not(self, monkeypatch):
        """A page served from another local port is loopback to loopback."""
        app, _ = await _served_app(monkeypatch)

        assert await _status(app, host="127.0.0.1:8000", origin="http://localhost:3000") == 200


class TestBehindAReverseProxy:
    """Caddy, nginx or similar on the same machine, upstream 127.0.0.1:8000.

    Caddy forwards the public name as the Host header, unchanged.
    """

    @pytest.mark.asyncio
    async def test_the_public_name_is_refused_until_it_is_listed(self, monkeypatch):
        """The cost of the default. Stated here so it is a decision, and so the
        startup hint and the docs have something to be true about."""
        app, _ = await _served_app(monkeypatch, MCP_API_KEYS="key")

        assert await _status(app, host="mcp.example.com") == 421

    @pytest.mark.asyncio
    @pytest.mark.parametrize("host", ["mcp.example.com", "MCP.Example.com", "mcp.example.com:443"])
    async def test_the_public_name_is_accepted_once_listed(self, monkeypatch, host):
        app, _ = await _served_app(
            monkeypatch, MCP_API_KEYS="key", MCP_ALLOWED_HOSTS="mcp.example.com"
        )

        assert await _status(app, host=host) == 200

    @pytest.mark.asyncio
    async def test_listing_the_public_name_does_not_open_the_rest(self, monkeypatch):
        app, _ = await _served_app(
            monkeypatch, MCP_API_KEYS="key", MCP_ALLOWED_HOSTS="mcp.example.com"
        )

        assert await _status(app, host="attacker.example") == 421
        assert await _status(app, host="127.0.0.1:8000") == 200

    @pytest.mark.asyncio
    async def test_the_proxy_on_a_wildcard_bind_is_in_the_same_position(self, monkeypatch):
        """Bound to 0.0.0.0, a proxy on the machine still arrives on 127.0.0.1."""
        app, _ = await _served_app(monkeypatch, MCP_HOST="0.0.0.0", MCP_API_KEYS="key")

        assert await _status(app, host="mcp.example.com", arrived_on="127.0.0.1") == 421

    @pytest.mark.asyncio
    async def test_a_browser_behind_the_proxy_is_same_origin_with_its_own_name(self, monkeypatch):
        """The Origin check compares against the Host as seen on the wire; a page
        on the public name talking to the public name is not cross-origin."""
        app, _ = await _served_app(
            monkeypatch, MCP_API_KEYS="key", MCP_ALLOWED_HOSTS="mcp.example.com"
        )

        assert await _status(app, host="mcp.example.com", origin="http://mcp.example.com") == 200
        assert await _status(app, host="mcp.example.com", origin="https://attacker.example") == 403


class TestOnARoutableBind:
    """`MCP_HOST=0.0.0.0` with API keys, reached over the network."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("host", ["10.0.0.5:8000", "mcp.lan:8000", "niwashi"])
    async def test_a_client_over_the_network_is_not_asked_for_a_known_name(
        self, monkeypatch, host
    ):
        """The address it arrived on is not loopback, so `auto` stays out of the
        way: this is a LAN deployment authenticated by its keys, and the
        hostnames its clients use are not this server's to know."""
        app, _ = await _served_app(monkeypatch, MCP_HOST="0.0.0.0", MCP_API_KEYS="key")

        assert await _status(app, host=host, arrived_on="10.0.0.5") == 200

    @pytest.mark.asyncio
    async def test_an_allowlist_applies_on_every_address(self, monkeypatch):
        """Once the operator names the hosts, the list is a list."""
        app, _ = await _served_app(
            monkeypatch, MCP_HOST="0.0.0.0", MCP_API_KEYS="key", MCP_ALLOWED_HOSTS="mcp.lan"
        )

        assert await _status(app, host="mcp.lan:8000", arrived_on="10.0.0.5") == 200
        assert await _status(app, host="attacker.example", arrived_on="10.0.0.5") == 421
