# Copyright (C) 2026 Khalid El-Ouiali — MONARK AIOPS srl
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
Unit tests for apic/client.py — ApicClient with a simulated httpx transport.

All tests use FakeHTTPClient to avoid any network calls.  Each test controls
exactly which responses (or exceptions) the fake transport returns, so we can
exercise every branch: happy path, re-auth, timeouts, malformed JSON, etc.
"""

from unittest.mock import MagicMock

import httpx
import pytest

from niwashi_mcp.apic.client import (
    _FETCH_ALL_PAGE_SIZE,
    _MAX_OBJECTS,
    _MAX_PAGES,
    ApicClient,
)
from niwashi_mcp.exceptions import (
    ApicAuthError,
    ApicConnectionError,
    ApicError,
    ApicRequestError,
    ApicResponseError,
)
from tests.conftest import apic_login_response, apic_response, make_imdata_objects

# ── Fake HTTP transport ───────────────────────────────────────────────────────


class _MockResponse:
    """Minimal httpx.Response stand-in."""

    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300
        self._body = body

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        if not self.is_success:
            raise httpx.HTTPStatusError(
                message=f"HTTP {self.status_code}",
                request=httpx.Request("GET", "https://test/"),
                response=MagicMock(status_code=self.status_code),
            )


class FakeHTTPClient:
    """Queue-based httpx.AsyncClient replacement.

    Each call to post() or get() pops the next item from `_queue`.
    Items can be _MockResponse instances (success) or Exception subclasses
    (raised directly to simulate network errors).

    Exposes `requests` for asserting what URLs / params were called.
    """

    def __init__(self, *responses):
        self._queue = list(responses)
        self.requests: list[dict] = []
        self.cookies = httpx.Cookies()
        self.timeout = 30.0

    def _next(self, method: str, url: str, **kwargs):
        self.requests.append({"method": method, "url": url, **kwargs})
        item = self._queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def post(self, url: str, **kwargs):
        return self._next("POST", url, **kwargs)

    async def get(self, url: str, **kwargs):
        return self._next("GET", url, **kwargs)

    async def aclose(self) -> None:
        pass


def _make_client(*responses, retry_attempts: int = 3) -> ApicClient:
    """Build an ApicClient wired to a FakeHTTPClient.

    retry_backoff_base=0 so retry tests never sleep for real; retry_attempts
    defaults to the production default (3) but is overridable per test.
    """
    client = ApicClient(
        "10.0.0.1", "admin", "secret",
        retry_attempts=retry_attempts, retry_backoff_base=0.0,
    )
    client._client = FakeHTTPClient(*responses)
    return client


# ── authenticate() ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_authenticate_success_stores_cookie():
    client = _make_client(_MockResponse(200, apic_login_response("tok-xyz")))
    await client.authenticate()
    assert client._client.cookies.get("APIC-cookie") == "tok-xyz"


@pytest.mark.asyncio
async def test_authenticate_401_raises_apic_auth_error():
    client = _make_client(_MockResponse(401, {}))
    with pytest.raises(ApicAuthError) as exc_info:
        await client.authenticate()
    assert exc_info.value.host == "10.0.0.1"
    assert exc_info.value.status == 401


@pytest.mark.asyncio
async def test_authenticate_403_raises_apic_auth_error():
    client = _make_client(_MockResponse(403, {}))
    with pytest.raises(ApicAuthError) as exc_info:
        await client.authenticate()
    assert exc_info.value.status == 403


@pytest.mark.asyncio
async def test_authenticate_timeout_raises_apic_connection_error():
    client = _make_client(httpx.TimeoutException("timed out"))
    with pytest.raises(ApicConnectionError) as exc_info:
        await client.authenticate()
    assert exc_info.value.host == "10.0.0.1"
    assert "timed out" in str(exc_info.value)


@pytest.mark.asyncio
async def test_authenticate_connect_error_raises_apic_connection_error():
    client = _make_client(httpx.ConnectError("connection refused"))
    with pytest.raises(ApicConnectionError):
        await client.authenticate()


@pytest.mark.asyncio
async def test_authenticate_malformed_json_raises_apic_response_error():
    """APIC returns 200 but imdata token path is wrong."""
    client = _make_client(_MockResponse(200, {"imdata": [{"unexpected": {}}]}))
    with pytest.raises(ApicResponseError):
        await client.authenticate()


@pytest.mark.asyncio
async def test_authenticate_empty_imdata_raises_apic_response_error():
    client = _make_client(_MockResponse(200, {"imdata": []}))
    with pytest.raises(ApicResponseError):
        await client.authenticate()


# ── query_class() — happy path ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_query_class_returns_parsed_objects():
    objects = make_imdata_objects(
        "fvBD",
        [
            {"dn": "uni/tn-OT/BD-servers", "name": "servers"},
            {"dn": "uni/tn-OT/BD-clients", "name": "clients"},
        ],
    )
    client = _make_client(_MockResponse(200, apic_response(objects)))
    result = await client.query_class("fvBD", {})
    assert len(result.objects) == 2
    assert all(r["_class"] == "fvBD" for r in result.objects)
    assert {r["name"] for r in result.objects} == {"servers", "clients"}
    assert result.total_available == 2
    assert result.complete is True


@pytest.mark.asyncio
async def test_query_class_empty_imdata_returns_empty_list():
    client = _make_client(_MockResponse(200, apic_response([])))
    result = await client.query_class("fvBD", {})
    assert result.objects == []
    assert result.total_available == 0


@pytest.mark.asyncio
async def test_query_class_embeds_children_when_requested():
    objects = make_imdata_objects(
        "fvBD",
        [{"dn": "uni/tn-OT/BD-mgmt", "name": "mgmt"}],
        children_map={
            "uni/tn-OT/BD-mgmt": [
                {
                    "fvSubnet": {
                        "attributes": {
                            "ip": "10.0.0.1/24",
                            "dn": "uni/tn-OT/BD-mgmt/subnet-[10.0.0.1/24]",
                        }
                    }
                }
            ]
        },
    )
    client = _make_client(_MockResponse(200, apic_response(objects)))
    result = await client.query_class("fvBD", {}, include_children=["fvSubnet"])
    assert len(result.objects) == 1
    children = result.objects[0].get("_children", [])
    assert len(children) == 1
    assert children[0]["_class"] == "fvSubnet"
    assert children[0]["ip"] == "10.0.0.1/24"


# ── query_class() — QueryResult / totalCount parsing ──────────────────────────


@pytest.mark.asyncio
async def test_query_class_total_available_parses_total_count():
    """total_available reflects the APIC-reported totalCount, which can be
    larger than the number of objects actually returned on this page."""
    objects = make_imdata_objects(
        "fvBD", [{"dn": "uni/tn-OT/BD-servers", "name": "servers"}]
    )
    body = {"totalCount": "250", "imdata": objects}
    client = _make_client(_MockResponse(200, body))
    result = await client.query_class("fvBD", {})
    assert len(result.objects) == 1
    assert result.total_available == 250
    assert result.complete is True


@pytest.mark.asyncio
async def test_query_class_total_available_falls_back_to_object_count():
    """A missing/non-numeric totalCount falls back to the number of objects
    actually parsed, rather than raising."""
    objects = make_imdata_objects(
        "fvBD", [{"dn": "uni/tn-OT/BD-servers", "name": "servers"}]
    )
    body = {"imdata": objects}
    client = _make_client(_MockResponse(200, body))
    result = await client.query_class("fvBD", {})
    assert result.total_available == 1


# ── query_class() — re-authentication ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_query_class_re_auths_on_401_and_retries():
    """First call returns 401 → re-auth → second call returns data."""
    objects = make_imdata_objects(
        "fvBD", [{"dn": "uni/tn-OT/BD-servers", "name": "servers"}]
    )
    client = _make_client(
        _MockResponse(401, {}),  # first query → 401
        _MockResponse(200, apic_login_response()),  # re-authenticate
        _MockResponse(200, apic_response(objects)),  # retry query → success
    )
    result = await client.query_class("fvBD", {})
    assert len(result.objects) == 1
    assert result.objects[0]["name"] == "servers"


@pytest.mark.asyncio
async def test_query_class_persistent_401_after_reauth_raises_apic_auth_error():
    """First call 401 → re-auth succeeds → second call still 401 → error.

    Raised immediately, not absorbed into the transient-status retry budget —
    a bad credential won't fix itself on a second outer attempt."""
    client = _make_client(
        _MockResponse(401, {}),  # first query → 401
        _MockResponse(200, apic_login_response()),  # re-authenticate
        _MockResponse(401, {}),  # retry query → still 401
    )
    with pytest.raises(ApicAuthError) as exc_info:
        await client.query_class("fvBD", {})
    assert "re-authentication" in str(exc_info.value)
    assert len(client._client.requests) == 3


# ── query_class() — network errors ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_query_class_timeout_raises_apic_connection_error():
    """A timeout on every attempt exhausts the retry budget (3 by default)."""
    client = _make_client(*[httpx.TimeoutException("timed out")] * 3)
    with pytest.raises(ApicConnectionError) as exc_info:
        await client.query_class("fvBD", {})
    assert "timed out" in str(exc_info.value)
    assert len(client._client.requests) == 3


@pytest.mark.asyncio
async def test_query_class_connect_error_raises_apic_connection_error():
    """A connect error on every attempt exhausts the retry budget."""
    client = _make_client(*[httpx.ConnectError("no route to host")] * 3)
    with pytest.raises(ApicConnectionError):
        await client.query_class("fvBD", {})
    assert len(client._client.requests) == 3


# ── query_class() — transient-status retry ────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 500, 502, 503, 504])
async def test_query_class_retries_transient_status_then_succeeds(status):
    """A single transient failure is retried and the second attempt succeeds."""
    client = _make_client(
        _MockResponse(status, {}),
        _MockResponse(200, apic_response([])),
    )
    result = await client.query_class("fvBD", {})
    assert result.objects == []
    assert len(client._client.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 500, 502, 503, 504])
async def test_query_class_exhausts_retries_on_persistent_transient_status(status):
    """A transient status that never recovers is raised after the full budget."""
    client = _make_client(*[_MockResponse(status, {})] * 3)
    with pytest.raises(ApicRequestError) as exc_info:
        await client.query_class("fvBD", {})
    assert exc_info.value.status == status
    assert len(client._client.requests) == 3


@pytest.mark.asyncio
async def test_query_class_retries_on_connect_error_then_succeeds():
    client = _make_client(
        httpx.ConnectError("connection refused"),
        _MockResponse(200, apic_response([])),
    )
    result = await client.query_class("fvBD", {})
    assert result.objects == []
    assert len(client._client.requests) == 2


@pytest.mark.asyncio
async def test_query_class_retries_on_timeout_then_succeeds():
    client = _make_client(
        httpx.TimeoutException("timed out"),
        _MockResponse(200, apic_response([])),
    )
    result = await client.query_class("fvBD", {})
    assert result.objects == []
    assert len(client._client.requests) == 2


@pytest.mark.asyncio
async def test_query_class_400_is_never_retried():
    """A permanent error (400) must not consume the retry budget — proven by
    queuing exactly one response: if the implementation retried, the fake
    transport's queue would be empty on the second GET and raise IndexError."""
    client = _make_client(_MockResponse(400, {}))
    with pytest.raises(ApicRequestError):
        await client.query_class("fvBD", {})
    assert len(client._client.requests) == 1


@pytest.mark.asyncio
async def test_query_class_recovers_after_transient_connection_error_post_reauth():
    """401 -> re-auth -> connection error on the retry GET -> next outer
    attempt succeeds without re-authenticating again."""
    objects = make_imdata_objects(
        "fvBD", [{"dn": "uni/tn-OT/BD-servers", "name": "servers"}]
    )
    client = _make_client(
        _MockResponse(401, {}),
        _MockResponse(200, apic_login_response()),
        httpx.TimeoutException("timeout on retry"),
        _MockResponse(200, apic_response(objects)),
    )
    result = await client.query_class("fvBD", {})
    assert len(result.objects) == 1
    assert len(client._client.requests) == 4


@pytest.mark.asyncio
async def test_query_class_exhausts_retry_budget_after_reauth_connection_errors():
    """401 -> re-auth -> connection error, then two more failed outer
    attempts — exhausts the retry budget and raises ApicConnectionError."""
    client = _make_client(
        _MockResponse(401, {}),
        _MockResponse(200, apic_login_response()),
        httpx.TimeoutException("timeout 1"),
        httpx.TimeoutException("timeout 2"),
        httpx.TimeoutException("timeout 3"),
    )
    with pytest.raises(ApicConnectionError):
        await client.query_class("fvBD", {})
    assert len(client._client.requests) == 5


@pytest.mark.asyncio
async def test_query_class_401_then_transient_status_then_recovers():
    """A transient failure after a successful re-auth still gets its own
    retry, rather than being conflated with the auth path."""
    client = _make_client(
        _MockResponse(401, {}),
        _MockResponse(200, apic_login_response()),
        _MockResponse(500, {}),
        _MockResponse(200, apic_response([])),
    )
    result = await client.query_class("fvBD", {})
    assert result.objects == []
    assert len(client._client.requests) == 4


@pytest.mark.asyncio
async def test_backoff_delay_is_bounded_and_increasing():
    client = ApicClient("10.0.0.1", "admin", "secret", retry_backoff_base=0.2)
    d1 = client._backoff_delay(1)
    d2 = client._backoff_delay(2)
    d3 = client._backoff_delay(3)
    assert d1 == 0.2
    assert d2 == 0.4
    assert d1 < d2 < d3
    assert client._backoff_delay(20) == 2.0  # capped


# ── query_class() — malformed APIC responses ─────────────────────────────────


@pytest.mark.asyncio
async def test_query_class_missing_imdata_key_raises_apic_response_error():
    """APIC returns 200 but response body has no 'imdata' key."""
    client = _make_client(_MockResponse(200, {"totalCount": "0"}))
    with pytest.raises(ApicResponseError) as exc_info:
        await client.query_class("fvBD", {})
    assert "imdata" in str(exc_info.value)


# ── query_class() — non-auth HTTP errors ──────────────────────────────────────


@pytest.mark.asyncio
async def test_query_class_400_with_apic_error_body_raises_apic_request_error():
    """A 400 (e.g. malformed filter_expr) with an APIC error body is wrapped,
    surfacing the APIC-supplied error text rather than a raw httpx error."""
    body = {
        "totalCount": "1",
        "imdata": [
            {
                "error": {
                    "attributes": {
                        "code": "400",
                        "text": "unable to process the query, class not found",
                    }
                }
            }
        ],
    }
    client = _make_client(_MockResponse(400, body))
    with pytest.raises(ApicRequestError) as exc_info:
        await client.query_class("fvBD", {})
    assert exc_info.value.status == 400
    assert "class not found" in exc_info.value.apic_text
    assert "class not found" in str(exc_info.value)
    assert len(client._client.requests) == 1  # 400 is permanent — never retried


@pytest.mark.asyncio
async def test_query_class_400_without_body_raises_apic_request_error():
    """A 400 with no usable APIC error body still raises ApicRequestError,
    just without APIC-supplied detail text."""
    client = _make_client(_MockResponse(400, {}))
    with pytest.raises(ApicRequestError) as exc_info:
        await client.query_class("fvBD", {})
    assert exc_info.value.status == 400
    assert exc_info.value.apic_text == ""
    assert len(client._client.requests) == 1


@pytest.mark.asyncio
async def test_query_class_400_is_catchable_as_apic_error():
    """ApicRequestError is a subclass of the base ApicError taxonomy."""
    client = _make_client(_MockResponse(400, {}))
    with pytest.raises(ApicError):
        await client.query_class("fvBD", {})


# ── query_class() — URL and parameter construction ────────────────────────────


@pytest.mark.asyncio
async def test_query_class_uses_class_endpoint_when_no_scope_dn():
    client = _make_client(_MockResponse(200, apic_response([])))
    await client.query_class("fvBD", {})
    url = client._client.requests[0]["url"]
    assert "/api/class/fvBD.json" in url


@pytest.mark.asyncio
async def test_query_class_uses_mo_subtree_endpoint_with_scope_dn():
    client = _make_client(_MockResponse(200, apic_response([])))
    await client.query_class("fvBD", {}, scope_dn="uni/tn-OT")
    url = client._client.requests[0]["url"]
    assert "/api/mo/uni/tn-OT.json" in url


@pytest.mark.asyncio
async def test_query_class_passes_limit_as_page_size():
    client = _make_client(_MockResponse(200, apic_response([])))
    await client.query_class("fvBD", {}, limit=42)
    params = client._client.requests[0].get("params", {})
    assert params.get("page-size") == "42"


@pytest.mark.asyncio
async def test_query_class_sets_filter_param_from_filters():
    client = _make_client(_MockResponse(200, apic_response([])))
    await client.query_class("fvBD", {"name": "servers"})
    params = client._client.requests[0].get("params", {})
    assert "query-target-filter" in params
    assert 'eq(fvBD.name,"servers")' in params["query-target-filter"]


@pytest.mark.asyncio
async def test_query_class_combines_filters_and_filter_expr():
    client = _make_client(_MockResponse(200, apic_response([])))
    await client.query_class(
        "fvBD", {"name": "srv"}, filter_expr='wcard(fvBD.dn,"uni/tn-OT")'
    )
    params = client._client.requests[0].get("params", {})
    filt = params.get("query-target-filter", "")
    assert filt.startswith("and(")
    assert 'wcard(fvBD.dn,"uni/tn-OT")' in filt
    assert 'eq(fvBD.name,"srv")' in filt


@pytest.mark.asyncio
async def test_query_class_sets_rsp_subtree_params_for_children():
    client = _make_client(_MockResponse(200, apic_response([])))
    await client.query_class("fvBD", {}, include_children=["fvSubnet", "fvRsCtx"])
    params = client._client.requests[0].get("params", {})
    assert params.get("rsp-subtree") == "children"
    assert "fvSubnet" in params.get("rsp-subtree-class", "")
    assert "fvRsCtx" in params.get("rsp-subtree-class", "")


@pytest.mark.asyncio
async def test_query_class_uses_only_filter_expr_when_no_filters():
    """filter_expr alone (no equality filters) sets query-target-filter directly."""
    client = _make_client(_MockResponse(200, apic_response([])))
    await client.query_class("fvBD", {}, filter_expr='wcard(fvBD.dn,"uni/tn-OT")')
    params = client._client.requests[0].get("params", {})
    assert params.get("query-target-filter") == 'wcard(fvBD.dn,"uni/tn-OT")'


@pytest.mark.asyncio
async def test_query_class_sets_order_by_param():
    """order_by kwarg maps to 'order-by' query parameter."""
    client = _make_client(_MockResponse(200, apic_response([])))
    await client.query_class("faultInst", {}, order_by="faultInst.severity|desc")
    params = client._client.requests[0].get("params", {})
    assert params.get("order-by") == "faultInst.severity|desc"


@pytest.mark.asyncio
async def test_query_class_sets_rsp_subtree_include_param():
    """rsp_subtree_include kwarg maps to 'rsp-subtree-include' query parameter."""
    client = _make_client(_MockResponse(200, apic_response([])))
    await client.query_class("fvBD", {}, rsp_subtree_include="faults,required")
    params = client._client.requests[0].get("params", {})
    assert params.get("rsp-subtree-include") == "faults,required"


@pytest.mark.asyncio
async def test_query_class_sets_time_range_param():
    """time_range kwarg maps to 'time-range' query parameter."""
    client = _make_client(_MockResponse(200, apic_response([])))
    await client.query_class("faultRecord", {}, time_range="24h")
    params = client._client.requests[0].get("params", {})
    assert params.get("time-range") == "24h"


@pytest.mark.asyncio
async def test_query_class_sets_page_param():
    """page kwarg maps to 'page' query parameter as a string."""
    client = _make_client(_MockResponse(200, apic_response([])))
    await client.query_class("fvBD", {}, page=2)
    params = client._client.requests[0].get("params", {})
    assert params.get("page") == "2"


# ── query_class() — fetch_all pagination ──────────────────────────────────────
#
# fetch_all pages at a fixed _FETCH_ALL_PAGE_SIZE and ignores the caller's
# `limit`. It used to page by `limit` and stop after 25 pages, so the default
# limit=20 capped a "fetch everything" call at 500 objects, and a set of exactly
# 25 x limit objects was reported complete=False. The pages below are therefore
# built at the real page size, from a generator, so a test states the *size of
# the fabric* it models rather than a hand-rolled page layout.


def _bd_page(names: list[str], total_count: int | str | None) -> _MockResponse:
    """Build one fvBD page response carrying `total_count` as totalCount.

    `None` omits the field entirely, as a response that never reported one.
    """
    objects = make_imdata_objects(
        "fvBD", [{"dn": f"uni/tn-OT/BD-{n}", "name": n} for n in names]
    )
    body: dict = {"imdata": objects}
    if total_count is not None:
        body["totalCount"] = str(total_count)
    return _MockResponse(200, body)


_TRUE_TOTAL = object()  # sentinel: "report the real number of objects"


def _bd_set(
    n_objects: int,
    *,
    reported: object = _TRUE_TOTAL,
    page_size: int = _FETCH_ALL_PAGE_SIZE,
) -> list[_MockResponse]:
    """The pages an APIC would serve for `n_objects` matching fvBDs.

    Every page carries `reported` as its totalCount — by default the true
    `n_objects`; pass an int/str to model a count that disagrees, or None to
    omit it. A set that is an exact multiple of `page_size` gets NO trailing
    empty page, exactly as a real APIC would not serve one: a client that asks
    for it has made a request the APIC never needed, and the fake transport
    raises when its queue runs dry.
    """
    total = n_objects if reported is _TRUE_TOTAL else reported
    pages = [
        _bd_page([f"n{i}" for i in range(lo, min(lo + page_size, n_objects))], total)
        for lo in range(0, n_objects, page_size)
    ]
    return pages or [_bd_page([], total)]


def _sent(client: ApicClient) -> list[dict]:
    """The `params` of every GET the client issued, in order."""
    return [r.get("params", {}) for r in client._client.requests]


@pytest.mark.asyncio
async def test_query_class_fetch_all_walks_pages_and_concatenates():
    """450 objects = two full pages and a short one; fetch_all joins them and
    stops at the short page instead of requesting a fourth."""
    client = _make_client(*_bd_set(450))
    result = await client.query_class("fvBD", {}, fetch_all=True)
    assert [o["name"] for o in result.objects] == [f"n{i}" for i in range(450)]
    assert result.total_available == 450
    assert result.complete is True
    assert len(client._client.requests) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 20, 200])
async def test_query_class_fetch_all_pages_at_a_fixed_size_whatever_the_limit(limit):
    """The wire carries page-size=200 on every page of a walk, with an
    incrementing 0-based page number — `limit` never reaches it.

    This replaces `..._sets_page_and_page_size_per_request`, which asserted
    page-size == limit and so pinned the coupling that capped the default call
    at 500 objects.
    """
    client = _make_client(*_bd_set(450))
    await client.query_class("fvBD", {}, limit=limit, fetch_all=True)
    sent = _sent(client)
    assert [p.get("page") for p in sent] == ["0", "1", "2"]
    assert {p.get("page-size") for p in sent} == {"200"}


@pytest.mark.asyncio
async def test_query_class_without_fetch_all_still_pages_by_limit():
    """The decoupling is for fetch_all only: a plain page keeps `limit`."""
    client = _make_client(*_bd_set(5, page_size=3))
    await client.query_class("fvBD", {}, limit=3)
    assert _sent(client)[0]["page-size"] == "3"


@pytest.mark.asyncio
async def test_query_class_fetch_all_ignores_the_page_argument():
    """`page` is documented as ignored under fetch_all: the walk starts at 0."""
    client = _make_client(*_bd_set(250))
    result = await client.query_class("fvBD", {}, page=7, fetch_all=True)
    assert [p["page"] for p in _sent(client)] == ["0", "1"]
    assert len(result.objects) == 250


@pytest.mark.asyncio
async def test_query_class_fetch_all_with_the_default_limit_gets_600_objects():
    """The reported regression: default limit (20) x 25 pages = 500, so 600
    objects came back as 500 with complete=False. 600 is far under the cap and
    must arrive whole."""
    client = _make_client(*_bd_set(600))
    result = await client.query_class("fvBD", {}, fetch_all=True)  # limit stays 20
    assert len(result.objects) == 600
    assert result.total_available == 600
    assert result.complete is True


@pytest.mark.asyncio
async def test_query_class_fetch_all_exact_multiple_of_the_page_size_is_complete():
    """400 = exactly two full pages. The next page would be empty, so the loop
    must conclude from totalCount, not from a short page — it never gets one —
    and must not spend a request on a page past the end (the fake transport
    raises if it does)."""
    client = _make_client(*_bd_set(2 * _FETCH_ALL_PAGE_SIZE))
    result = await client.query_class("fvBD", {}, fetch_all=True)
    assert len(result.objects) == 400
    assert result.complete is True
    assert len(client._client.requests) == 2


@pytest.mark.asyncio
async def test_query_class_fetch_all_single_short_page_no_truncation():
    """A single page shorter than the page size is the whole matching set — no
    truncation or cap involved."""
    client = _make_client(*_bd_set(1))
    result = await client.query_class("fvBD", {}, fetch_all=True)
    assert len(result.objects) == 1
    assert result.total_available == 1
    assert result.complete is True
    assert len(client._client.requests) == 1


@pytest.mark.asyncio
async def test_query_class_fetch_all_empty_set_is_complete():
    """Nothing matches: one empty page, totalCount 0 — complete, and not an
    error. (An empty page is only suspicious when totalCount says more is due.)"""
    client = _make_client(*_bd_set(0))
    result = await client.query_class("fvBD", {}, fetch_all=True)
    assert result.objects == []
    assert result.total_available == 0
    assert result.complete is True
    assert len(client._client.requests) == 1


@pytest.mark.asyncio
async def test_query_class_fetch_all_exactly_at_the_cap_is_complete():
    """A set of exactly _MAX_OBJECTS is all fetched, so it is complete.

    The old loop ended its `for` without a break on the 25th full page and
    reported complete=False — correctly capped, wrongly incomplete: nothing was
    left behind.
    """
    client = _make_client(*_bd_set(_MAX_OBJECTS))
    result = await client.query_class("fvBD", {}, fetch_all=True)
    assert len(result.objects) == _MAX_OBJECTS
    assert result.total_available == _MAX_OBJECTS
    assert result.complete is True
    assert len(client._client.requests) == _MAX_PAGES


@pytest.mark.asyncio
async def test_query_class_fetch_all_one_past_the_cap_is_incomplete():
    """_MAX_OBJECTS + 1: the first _MAX_OBJECTS come back, complete is False,
    total_available still names the true size, and no 26th request is made.
    Nothing beyond the cap is fetched, so nothing needs trimming."""
    client = _make_client(*_bd_set(_MAX_OBJECTS + 1))
    result = await client.query_class("fvBD", {}, fetch_all=True)
    assert len(result.objects) == _MAX_OBJECTS
    assert result.total_available == _MAX_OBJECTS + 1
    assert result.complete is False
    assert len(client._client.requests) == _MAX_PAGES


@pytest.mark.asyncio
async def test_query_class_fetch_all_far_over_the_cap_stops_at_it():
    """A fabric-wide scan of 999,999 stops after _MAX_PAGES requests with
    _MAX_OBJECTS in hand — the cap is a bound on the walk, not a suggestion.
    (Replaces the page-cap and object-cap tests: the two caps are now one,
    since page size x _MAX_PAGES == _MAX_OBJECTS.)"""
    pages = [
        _bd_page([f"p{p}-{i}" for i in range(_FETCH_ALL_PAGE_SIZE)], 999999)
        for p in range(_MAX_PAGES + 2)  # more supply than the cap will consume
    ]
    client = _make_client(*pages)
    result = await client.query_class("fvBD", {}, fetch_all=True)
    assert len(result.objects) == _MAX_OBJECTS
    assert result.total_available == 999999
    assert result.complete is False
    assert len(client._client.requests) == _MAX_PAGES


@pytest.mark.asyncio
async def test_query_class_fetch_all_empty_page_before_the_total_is_incomplete(caplog):
    """The APIC says 1000 and then serves an empty page after 200.

    The loop must stop (not request page after empty page), must not claim the
    result complete, and must say why in the log. An empty page that counted as
    "the natural end" would hand back 200 of 1000 as a finished answer.
    """
    client = _make_client(
        *_bd_set(200, reported=1000),
        _bd_page([], 1000),
        _bd_page(["never-asked-for"], 1000),
    )
    with caplog.at_level("WARNING", logger="niwashi-mcp.apic"):
        result = await client.query_class("fvBD", {}, fetch_all=True)

    assert len(result.objects) == 200
    assert result.total_available == 1000
    assert result.complete is False
    assert len(client._client.requests) == 2, "kept paging past an empty page"
    assert "incomplete" in caplog.text


@pytest.mark.asyncio
async def test_query_class_fetch_all_short_page_before_the_total_is_incomplete():
    """A short page is the natural end only when nothing says otherwise. Here
    totalCount promised 450 and the walk ran dry at 350."""
    client = _make_client(*_bd_set(350, reported=450))
    result = await client.query_class("fvBD", {}, fetch_all=True)
    assert len(result.objects) == 350
    assert result.total_available == 450
    assert result.complete is False


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [None, "n/a", ""], ids=["absent", "text", "blank"])
async def test_query_class_fetch_all_without_a_usable_total_trusts_the_short_page(missing):
    """No totalCount (absent, or not a number) means no figure to check the walk
    against, so the short page is all the evidence there is — what this loop
    always trusted. It must keep walking past FULL pages (a fallback of "what I
    have so far" would stop after page one) and report the fetched count."""
    client = _make_client(*_bd_set(450, reported=missing))
    result = await client.query_class("fvBD", {}, fetch_all=True)
    assert len(result.objects) == 450
    assert result.total_available == 450  # the fetched count, the usual fallback
    assert result.complete is True
    assert len(client._client.requests) == 3


@pytest.mark.asyncio
async def test_query_class_fetch_all_without_a_total_never_claims_a_capped_walk_complete():
    """No totalCount, and the set never ends: after _MAX_PAGES full pages there
    is no evidence the set is exhausted, so complete must be False."""
    pages = [
        _bd_page([f"p{p}-{i}" for i in range(_FETCH_ALL_PAGE_SIZE)], None)
        for p in range(_MAX_PAGES + 2)
    ]
    client = _make_client(*pages)
    result = await client.query_class("fvBD", {}, fetch_all=True)
    assert len(result.objects) == _MAX_OBJECTS
    assert result.complete is False
    assert len(client._client.requests) == _MAX_PAGES


@pytest.mark.asyncio
async def test_query_class_fetch_all_remembers_the_total_from_an_earlier_page():
    """totalCount reported on page 0 and omitted from a later page still governs.

    Page 0 promises 450; the short last page, which omits the field, delivers
    only 300 in all. Forgetting the figure would read that short page as a
    natural end and call the walk complete, with 150 objects missing and
    `total_available` quietly shrinking to what was fetched.
    """
    first = _bd_set(450)[0]
    last = _bd_page([f"n{i}" for i in range(200, 300)], None)
    client = _make_client(first, last)
    result = await client.query_class("fvBD", {}, fetch_all=True)
    assert len(result.objects) == 300
    assert result.total_available == 450
    assert result.complete is False


# ── query_class() — fetch_all: totalCount that moves, and objects seen twice ──
#
# The set is not frozen while a walk reads it. totalCount can change between
# pages, and an object created or deleted early in the set shifts every later
# page by one — so the object on a page boundary can arrive twice while another
# is never sent.


@pytest.mark.asyncio
async def test_query_class_fetch_all_latest_total_count_wins_when_the_set_shrinks():
    """Page 0 says 450, page 1 says 400 and 400 objects are in hand: complete.

    Keeping the FIRST figure would see 400 < 450 behind a full page and ask for
    a page 2 the set no longer has (the fake transport raises on it), and would
    report a total_available the APIC had already retracted.
    """
    client = _make_client(
        _bd_page([f"n{i}" for i in range(0, 200)], 450),
        _bd_page([f"n{i}" for i in range(200, 400)], 400),
    )
    result = await client.query_class("fvBD", {}, fetch_all=True)

    assert len(result.objects) == 400
    assert result.total_available == 400
    assert result.complete is True
    assert len(client._client.requests) == 2


@pytest.mark.asyncio
async def test_query_class_fetch_all_latest_total_count_wins_when_the_set_grows():
    """Page 0 says 250, the short page 1 says 300 and only 250 are in hand.

    The figure the walk ends on is the one the APIC last gave. Keeping the first
    would call 250 of 300 complete, the set having grown under the walk.
    """
    client = _make_client(
        _bd_page([f"n{i}" for i in range(0, 200)], 250),
        _bd_page([f"n{i}" for i in range(200, 250)], 300),
    )
    result = await client.query_class("fvBD", {}, fetch_all=True)

    assert len(result.objects) == 250
    assert result.total_available == 300
    assert result.complete is False


@pytest.mark.asyncio
async def test_query_class_fetch_all_an_object_seen_twice_is_not_a_second_object(caplog):
    """250 matches; page 1 repeats the last object of page 0 and so is one short.

    249 distinct objects against a totalCount of 250: the walk is incomplete.
    Counting the repeat made it 250 — complete=True, a duplicate in the answer,
    and the object the shift skipped never missed. The first copy is the one
    kept, and the drop is logged.
    """
    first = _bd_page([f"n{i}" for i in range(0, 200)], 250)
    second = _bd_page(["changed-copy"] + [f"n{i}" for i in range(200, 249)], 250)
    # Same dn as the last object of page 0, different content.
    second.json()["imdata"][0]["fvBD"]["attributes"]["dn"] = "uni/tn-OT/BD-n199"
    client = _make_client(first, second)
    with caplog.at_level("WARNING", logger="niwashi-mcp.apic"):
        result = await client.query_class("fvBD", {}, fetch_all=True)

    dns = [o["dn"] for o in result.objects]
    assert len(dns) == len(set(dns)) == 249
    assert next(o for o in result.objects if o["dn"].endswith("BD-n199"))["name"] == "n199"
    assert result.total_available == 250
    assert result.complete is False
    assert "dropped 1 duplicate" in caplog.text


@pytest.mark.asyncio
async def test_query_class_fetch_all_a_repeat_on_a_full_page_does_not_end_the_walk():
    """450 matches, and page 1 repeats n199 yet still carries 200 objects.

    A full page is full whatever it repeats, so the walk goes on to page 2 —
    judging "short" on the objects KEPT would stop at 199 and report a walk that
    had 51 objects to go as incomplete. Page 2 then covers the remaining
    distinct objects: all 450 are in hand, once each, so it is complete. Without
    the dedupe the count reaches 450 a page early with 451 objects in the
    answer.
    """
    client = _make_client(
        _bd_page([f"n{i}" for i in range(0, 200)], 450),
        _bd_page([f"n{i}" for i in range(199, 399)], 450),
        _bd_page([f"n{i}" for i in range(399, 450)], 450),
    )
    result = await client.query_class("fvBD", {}, fetch_all=True)

    names = [o["name"] for o in result.objects]
    assert names == [f"n{i}" for i in range(450)]
    assert result.total_available == 450
    assert result.complete is True
    assert len(client._client.requests) == 3


@pytest.mark.asyncio
async def test_query_class_fetch_all_objects_without_a_dn_are_kept_and_counted():
    """Nothing to tell two of them apart by, so none is ever called a repeat."""
    objects = make_imdata_objects("fvBD", [{"name": "a"}, {"name": "a"}, {"name": "b"}])
    client = _make_client(_MockResponse(200, {"totalCount": "3", "imdata": objects}))
    result = await client.query_class("fvBD", {}, fetch_all=True)

    assert [o["name"] for o in result.objects] == ["a", "a", "b"]
    assert result.complete is True


@pytest.mark.asyncio
async def test_query_class_timeout_after_reauth_recovers_on_next_attempt():
    """A timeout on the retry GET (right after a successful re-auth) is now
    caught by the outer retry budget instead of failing immediately — the
    next outer attempt succeeds without re-authenticating again."""
    objects = make_imdata_objects(
        "fvBD", [{"dn": "uni/tn-OT/BD-servers", "name": "servers"}]
    )
    client = _make_client(
        _MockResponse(401, apic_response([])),        # first GET → 401
        _MockResponse(200, apic_login_response()),    # POST (re-auth) → OK
        httpx.TimeoutException("timeout on retry"),   # retry GET → timeout
        _MockResponse(200, apic_response(objects)),   # next outer attempt → success
    )
    result = await client.query_class("fvBD", {})
    assert len(result.objects) == 1
    assert len(client._client.requests) == 4


@pytest.mark.asyncio
async def test_query_class_connect_error_after_reauth_exhausts_retry_budget():
    """A ConnectError on the retry GET after re-auth, persisting across the
    full retry budget, raises ApicConnectionError."""
    client = _make_client(
        _MockResponse(401, apic_response([])),
        _MockResponse(200, apic_login_response()),
        httpx.ConnectError("connection refused on retry"),
        httpx.ConnectError("connection refused 2"),
        httpx.ConnectError("connection refused 3"),
    )
    with pytest.raises(ApicConnectionError):
        await client.query_class("fvBD", {})
    assert len(client._client.requests) == 5


@pytest.mark.asyncio
async def test_query_class_invalid_json_raises_apic_response_error():
    """A non-JSON response body raises ApicResponseError."""

    class _BadJsonResponse:
        status_code = 200
        is_success = True

        def json(self):
            raise ValueError("not valid JSON")

        def raise_for_status(self):
            pass

    client = _make_client(_BadJsonResponse())
    with pytest.raises(ApicResponseError, match="not valid JSON"):
        await client.query_class("fvBD", {})


@pytest.mark.asyncio
async def test_close_releases_http_client():
    """close() delegates to the underlying httpx client aclose()."""
    client = _make_client(_MockResponse(200, apic_response([])))
    closed: list[bool] = []
    original_aclose = client._client.aclose

    async def _track_close():
        closed.append(True)
        await original_aclose()

    client._client.aclose = _track_close
    await client.close()
    assert closed == [True]


# ── get_by_dn() / count_class() — _request_json() error handling ────────────
#
# get_by_dn() and count_class() share _request_json() rather than
# query_class()'s code path, so its error handling (401/403 re-auth,
# non-auth HTTP errors, malformed JSON) is exercised independently here.


@pytest.mark.asyncio
async def test_get_by_dn_found_returns_attributes():
    objects = make_imdata_objects("fvBD", [{"name": "servers", "dn": "uni/tn-OT/BD-servers"}])
    client = _make_client(_MockResponse(200, apic_response(objects)))
    obj = await client.get_by_dn("uni/tn-OT/BD-servers")
    assert obj["_class"] == "fvBD"
    assert obj["name"] == "servers"


@pytest.mark.asyncio
async def test_get_by_dn_missing_returns_none():
    """A real 'not found' is HTTP 200 with an empty imdata list — never a
    404 — so it must not be retried or raised, just returned as None."""
    client = _make_client(_MockResponse(200, apic_response([])))
    assert await client.get_by_dn("uni/tn-OT/BD-doesNotExist") is None
    assert len(client._client.requests) == 1


@pytest.mark.asyncio
async def test_get_by_dn_retries_on_404_then_succeeds():
    objects = make_imdata_objects("fvBD", [{"name": "servers", "dn": "uni/tn-OT/BD-servers"}])
    client = _make_client(
        _MockResponse(404, {}),
        _MockResponse(200, apic_response(objects)),
    )
    obj = await client.get_by_dn("uni/tn-OT/BD-servers")
    assert obj["name"] == "servers"
    assert len(client._client.requests) == 2


@pytest.mark.asyncio
async def test_get_by_dn_retries_exhausted_raises_apic_request_error():
    client = _make_client(*[_MockResponse(404, {})] * 3)
    with pytest.raises(ApicRequestError) as exc_info:
        await client.get_by_dn("uni/tn-OT/BD-servers")
    assert exc_info.value.status == 404
    assert len(client._client.requests) == 3


@pytest.mark.asyncio
async def test_get_by_dn_400_raises_apic_request_error():
    body = {
        "imdata": [
            {"error": {"attributes": {"code": "400", "text": "malformed DN"}}}
        ]
    }
    client = _make_client(_MockResponse(400, body))
    with pytest.raises(ApicRequestError) as exc_info:
        await client.get_by_dn("not-a-real-dn")
    assert exc_info.value.status == 400
    assert "malformed DN" in exc_info.value.apic_text
    assert len(client._client.requests) == 1


@pytest.mark.asyncio
async def test_get_by_dn_re_authenticates_on_401():
    objects = make_imdata_objects("fvBD", [{"name": "servers", "dn": "uni/tn-OT/BD-servers"}])
    client = _make_client(
        _MockResponse(401, {}),
        _MockResponse(200, apic_login_response()),
        _MockResponse(200, apic_response(objects)),
    )
    obj = await client.get_by_dn("uni/tn-OT/BD-servers")
    assert obj["name"] == "servers"


@pytest.mark.asyncio
async def test_count_class_reads_total_count():
    objects = make_imdata_objects("fvBD", [{"dn": "uni/tn-OT/BD-servers"}])
    body = {"imdata": objects, "totalCount": "42"}
    client = _make_client(_MockResponse(200, body))
    assert await client.count_class("fvBD", {}) == 42


@pytest.mark.asyncio
async def test_count_class_asks_for_a_one_object_page_not_the_count_subtree():
    """Regression guard for the count idiom itself.

    count_class() must request a 1-object page and read `totalCount`. It must
    NOT send `rsp-subtree-include=count`: that mechanism's `moCount` tally was
    measured wrong on APIC 6.0(9c), reporting 0 for scoped counts against real
    subtrees holding up to 192 objects — which reads as a legitimate answer
    ("none in this tenant") rather than as a failure. See the count_class()
    docstring for the full measurements.
    """
    objects = make_imdata_objects("fvBD", [{"dn": "uni/tn-OT/BD-servers"}])
    body = {"imdata": objects, "totalCount": "192"}
    client = _make_client(_MockResponse(200, body))

    assert await client.count_class("fvBD", {}, scope_dn="uni/tn-OT") == 192

    params = client._client.requests[0]["params"]
    assert params["page-size"] == "1"
    assert "rsp-subtree-include" not in params


@pytest.mark.asyncio
async def test_count_class_prefers_total_count_over_a_mo_count_body():
    """`totalCount` wins even if a `moCount` object is present in the body.

    The two disagreed by nearly 2x on a live fabric (moCount 203 vs. the real
    403 fvBD); totalCount was the exact one. This pins which field is
    authoritative, so restoring the old parser can never pass silently.
    """
    body = {
        "imdata": [{"moCount": {"attributes": {"count": "203"}}}],
        "totalCount": "403",
    }
    client = _make_client(_MockResponse(200, body))
    assert await client.count_class("fvBD", {}) == 403


@pytest.mark.asyncio
async def test_count_class_500_raises_apic_request_error():
    client = _make_client(*[_MockResponse(500, {})] * 3)
    with pytest.raises(ApicRequestError) as exc_info:
        await client.count_class("fvBD", {})
    assert exc_info.value.status == 500
    assert len(client._client.requests) == 3


@pytest.mark.asyncio
async def test_count_class_retries_on_500_then_succeeds():
    objects = make_imdata_objects("fvBD", [{"dn": "uni/tn-OT/BD-servers"}])
    body = {"imdata": objects, "totalCount": "7"}
    client = _make_client(_MockResponse(500, {}), _MockResponse(200, body))
    assert await client.count_class("fvBD", {}) == 7
    assert len(client._client.requests) == 2


# ── 403 on the data path, and the caps' actual values ─────────────────────────
#
# Two gaps a targeted mutation pass found. Neither is a defect in the client
# today; both are places where a plausible edit would go unnoticed.
#
# The re-auth branch triggers on `resp.status_code in (401, 403)`, but until now
# only the *login* endpoint was ever tested with a 403 — so narrowing that tuple
# to `(401,)` left the whole suite green. An APIC answers 403 on the data path
# when a token is valid but no longer carries the role for that class, which is
# recoverable by re-authenticating and is exactly what the branch is for.
#
# The cap tests import `_MAX_PAGES` and `_MAX_OBJECTS` and assert against them,
# which proves the loop *honours* whatever the constants say and can never
# notice their values changing. Measured: 25 → 250 and 5000 → 50000 both pass.
# These bounds are what stops a fabric-wide scan from being unbounded, and the
# tool docstrings tell an agent what they are.


@pytest.mark.asyncio
async def test_query_class_403_on_the_data_path_reauthenticates_and_retries():
    """A 403 on a data request is recoverable, exactly as a 401 is.

    Without this, narrowing the re-auth branch to 401 alone is invisible: the
    only 403 the suite exercised was on /aaaLogin, which never reaches it.
    """
    client = _make_client(
        _MockResponse(403, {}),
        _MockResponse(200, apic_login_response()),
        _MockResponse(200, apic_response([{"fvBD": {"attributes": {"dn": "uni/tn-T/BD-b"}}}])),
    )
    result = await client.query_class("fvBD", {})

    assert len(result.objects) == 1
    methods = [r["method"] for r in client._client.requests]
    assert methods == ["GET", "POST", "GET"], "no re-authentication happened between the two GETs"


@pytest.mark.asyncio
async def test_get_by_dn_403_on_the_data_path_reauthenticates_and_retries():
    """The same branch is shared by get_by_dn; assert it there too."""
    client = _make_client(
        _MockResponse(403, {}),
        _MockResponse(200, apic_login_response()),
        _MockResponse(200, apic_response([{"fvBD": {"attributes": {"dn": "uni/tn-T/BD-b"}}}])),
    )
    obj = await client.get_by_dn("uni/tn-T/BD-b")

    assert obj is not None
    assert [r["method"] for r in client._client.requests] == ["GET", "POST", "GET"]


@pytest.mark.asyncio
async def test_persistent_403_after_reauth_is_an_auth_error_not_a_request_error():
    """Still 403 after re-authenticating is an authorisation problem, and must
    say so — an ApicRequestError would send the caller looking at their filter."""
    client = _make_client(
        _MockResponse(403, {}),
        _MockResponse(200, apic_login_response()),
        _MockResponse(403, {}),
    )
    with pytest.raises(ApicAuthError) as exc_info:
        await client.query_class("fvBD", {})
    assert exc_info.value.status == 403


def test_the_fetch_all_caps_hold_the_values_the_tools_promise():
    """Pin the values, not just the loop's obedience to them.

    Every other cap test imports these constants and asserts against them, so
    they measure that the loop honours the cap — never what the cap is. Raising
    `_MAX_OBJECTS` tenfold passes all of them. These numbers are the only thing
    bounding a fabric-wide scan with no `scope_dn`, and `query`'s docstring
    tells an agent exactly what they are: a change here is a change to what the
    server promises, so it should be deliberate and arrive with its own diff.
    """
    assert _FETCH_ALL_PAGE_SIZE == 200
    assert _MAX_OBJECTS == 5000
    assert _MAX_PAGES == 25
    assert 1_000 <= _MAX_OBJECTS < 100_000, "no longer 'thousands', as the tool docstring says"


def test_the_fetch_all_cap_lands_on_a_page_boundary():
    """The three constants must agree, or the cap stops being exact.

    `_MAX_PAGES` is derived from the other two, so the loop's bound and the
    object cap are one limit stated twice. If the object cap were not a
    multiple of the page size, the last page would overshoot it and the cap
    would be a number the server does not keep — or objects would have to be
    fetched and then discarded.
    """
    assert _MAX_OBJECTS % _FETCH_ALL_PAGE_SIZE == 0
    assert _MAX_PAGES * _FETCH_ALL_PAGE_SIZE == _MAX_OBJECTS
    assert _FETCH_ALL_PAGE_SIZE <= 200, "above the page-size `query` clamps its own limit to"


def test_the_fetch_all_numbers_quoted_to_the_agent_match_the_constants():
    """The prose an agent reads must state the cap and page size the code keeps.

    The docstrings cannot be f-strings, so "5,000" and "200" are typed twice.
    This is the thing that fails when only one side moves.
    """
    import inspect

    from niwashi_mcp import main as main_mod

    cap = f"{_MAX_OBJECTS:,}"
    page = f"{_FETCH_ALL_PAGE_SIZE} objects per page"
    query_doc = inspect.getdoc(main_mod.query) or ""
    for name, text in (
        ("instructions", main_mod.mcp.instructions),
        ("query docstring", query_doc),
    ):
        assert cap in text, f"{name} no longer states the {cap}-object fetch_all cap"
    assert page in query_doc, (
        f"query's docstring no longer states the fetch_all page size ({page})"
    )


def test_non_numeric_total_count_falls_back_rather_than_reporting_zero():
    """The half of the docstring's promise that was never tested.

    `test_query_class_missing_total_count_falls_back` covers a *missing*
    totalCount; the docstring also promises a non-numeric one falls back. It
    does — but nothing asserted it, so making the except clause return 0 instead
    passed. That would tell an agent a populated result set is empty.
    """
    assert ApicClient._parse_total_available({"totalCount": "n/a"}, 2) == 2
    assert ApicClient._parse_total_available({"totalCount": None}, 7) == 7
    assert ApicClient._parse_total_available({"totalCount": "12"}, 0) == 12
