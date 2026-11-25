# Copyright (C) 2026 Khalid El-Ouiali — MONARK AIOPS srl
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
What ApicClient does with an answer that is not data.

The APIC refuses a request with an `error` object in `imdata`, and it does not
always say so with a status a client would notice. Measured on 6.0(9c): an
`eq`/`ne` filter whose value is not valid for its attribute —
`eq(faultInst.code,"F0526")`, `eq(fvBD.unicastRoute,"False")` — is answered
`HTTP/1.1 301 Moved Permanently`, with no Location header, and the body

    {"totalCount": "1", "imdata": [{"error": {"attributes": {"code": "301",
     "text": "Incorrect filter format for faultInst.code, value 'F0526' is not valid"}}}]}

The 301 is the APIC's own error code reused as the HTTP status; it is not a
redirect. The client used to raise only on a status >= 400, so it parsed that
body as data: `count` returned `totalCount` ("1"), and `query` returned the
error object as a row with `_class: "error"` and `complete: true`.

These tests drive the real ApicClient over an httpx.MockTransport, because what
is under test is how the client reads a wire response — statuses, headers, body
shapes — which the FakeHTTPClient of test_client.py (no headers, no redirects)
cannot express. Every call the tools make is covered: a single page, children,
a scope, fetch_all, count, get_by_dn and controller_version all share
`_request_json`, and each must refuse the same answers.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

from niwashi_mcp.apic.client import _FETCH_ALL_PAGE_SIZE, _MAX_ERROR_TEXT, ApicClient
from niwashi_mcp.exceptions import (
    ApicAuthError,
    ApicError,
    ApicRequestError,
    ApicResponseError,
)
from tests.conftest import apic_response, make_imdata_objects

_APIC_ORIGIN = "https://apic.test"

# ── A scripted APIC over a mock transport ─────────────────────────────────────

Answer = Callable[[], httpx.Response]


class ScriptedApic:
    """Answers each request with the next scripted answer (the last one repeats).

    Records every request it receives — including any the client sent to a
    redirect target, which is how "the client did not follow it" is observed.
    """

    def __init__(self, *answers: Answer) -> None:
        self._answers = answers
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        index = min(len(self.requests), len(self._answers)) - 1
        return self._answers[index]()

    @property
    def hosts(self) -> set[str]:
        return {request.url.host for request in self.requests}


def build_client(apic: ScriptedApic, *, retry_attempts: int = 3) -> ApicClient:
    """A real ApicClient, constructed as in production, talking to `apic`.

    Only the transport is swapped. `follow_redirects` is read off the client the
    constructor built, so a change to what the constructor sets changes what
    these tests exercise.
    """
    client = ApicClient(
        "apic.test", "admin", "pw", retry_attempts=retry_attempts, retry_backoff_base=0.0
    )
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(apic),
        follow_redirects=client._client.follow_redirects,
    )
    return client


def reply(status: int, body: Any = None, *, headers: dict[str, str] | None = None) -> Answer:
    """An answer of `status`, JSON `body` (or none) and `headers`."""

    def make() -> httpx.Response:
        if body is None:
            return httpx.Response(status, headers=headers)
        return httpx.Response(status, json=body, headers=headers)

    return make


def error_body(code: str, text: str) -> dict[str, Any]:
    """The body the APIC sends for a request it refused: one error object."""
    return {
        "totalCount": "1",
        "imdata": [{"error": {"attributes": {"code": code, "text": text}}}],
    }


_BD_ROWS = make_imdata_objects(
    "fvBD",
    [
        {"dn": "uni/tn-OT/BD-a", "name": "a"},
        {"dn": "uni/tn-OT/BD-b", "name": "b"},
    ],
)
_ERROR_ITEM = {"error": {"attributes": {"code": "301", "text": "Incorrect filter format"}}}
_OK = apic_response(_BD_ROWS)

# ── Every call that reads the APIC ────────────────────────────────────────────

_Call = Callable[[ApicClient], Awaitable[Any]]

_CALLS: dict[str, _Call] = {
    "query-one-page": lambda c: c.query_class("fvBD", {}),
    "query-with-children": lambda c: c.query_class("fvBD", {}, include_children=["fvRsCtx"]),
    "query-scoped": lambda c: c.query_class("fvBD", {}, scope_dn="uni/tn-OT"),
    "query-filter-expr": lambda c: c.query_class(
        "faultInst", {}, filter_expr='eq(faultInst.code,"F0526")'
    ),
    "query-fetch-all": lambda c: c.query_class("fvBD", {}, fetch_all=True),
    "count": lambda c: c.count_class("fvBD", {}),
    "count-filtered": lambda c: c.count_class(
        "faultInst", {"severity": "critical"}, filter_expr='eq(faultInst.code,"F0526")'
    ),
    "get-by-dn": lambda c: c.get_by_dn("uni/tn-OT/BD-a"),
    "get-by-dn-with-children": lambda c: c.get_by_dn("uni/tn-OT/BD-a", include_children=["fvRsCtx"]),
    "controller-version": lambda c: c.controller_version(),
}

# ── Every answer that is not data ─────────────────────────────────────────────

_APIC_301 = error_body("301", "Incorrect filter format for faultInst.code, value 'F0526' is not valid")

# (answer, status, apic_code, apic_text) — what the raised error must carry.
_REFUSALS = [
    pytest.param(
        reply(301, _APIC_301), 301, "301", "faultInst.code", id="301-as-the-apic-sends-it"
    ),
    pytest.param(
        reply(302, error_body("302", "found elsewhere")), 302, "302", "found elsewhere",
        id="302-with-an-error-body",
    ),
    pytest.param(
        reply(307, _APIC_301), 307, "301", "faultInst.code", id="307-with-an-error-body"
    ),
    pytest.param(
        reply(301, headers={"Location": "https://apic.test/login"}),
        301, "", "", id="301-redirect-with-no-body",
    ),
    pytest.param(
        reply(302, {"redirect": "login"}, headers={"Location": "https://elsewhere.test/login"}),
        302, "", "", id="302-redirect-to-another-host",
    ),
    pytest.param(
        reply(307, headers={"Location": "https://elsewhere.test/api/class/fvBD.json"}),
        307, "", "", id="307-redirect-to-another-host",
    ),
    pytest.param(
        reply(200, _APIC_301), 200, "301", "faultInst.code", id="error-object-with-http-200"
    ),
    pytest.param(
        reply(400, error_body("121", "Prop 'x' not found in class 'fvBD' property table")),
        400, "121", "Prop 'x' not found", id="error-object-with-http-400",
    ),
    pytest.param(
        reply(200, {"totalCount": "1", "imdata": [{"error": {}}]}),
        200, "", "", id="error-object-without-attributes",
    ),
    pytest.param(
        reply(200, {"totalCount": "1", "imdata": [{"error": "refused"}]}),
        200, "", "", id="error-object-that-is-not-an-object",
    ),
    pytest.param(
        reply(200, {"totalCount": "3", "imdata": [*_BD_ROWS, _ERROR_ITEM]}),
        200, "301", "Incorrect filter format", id="mixed-rows-then-error",
    ),
    pytest.param(
        reply(200, {"totalCount": "3", "imdata": [_ERROR_ITEM, *_BD_ROWS]}),
        200, "301", "Incorrect filter format", id="mixed-error-then-rows",
    ),
    pytest.param(
        reply(200, {"totalCount": "2", "imdata": [_ERROR_ITEM, _ERROR_ITEM]}),
        200, "301", "Incorrect filter format", id="two-error-objects",
    ),
    pytest.param(
        reply(501, error_body("501", "not implemented")), 501, "501", "not implemented",
        id="a-5xx-that-is-not-transient",
    ),
]


@pytest.mark.parametrize("answer, status, code, text", _REFUSALS)
@pytest.mark.parametrize("call", list(_CALLS.values()), ids=list(_CALLS))
class TestEveryReadRefusesAnAnswerThatIsNotData:
    async def test_it_raises_a_request_error_carrying_what_the_apic_said(
        self, call, answer, status, code, text
    ):
        apic = ScriptedApic(answer)
        with pytest.raises(ApicRequestError) as caught:
            await call(build_client(apic))

        assert caught.value.status == status
        assert caught.value.apic_code == code
        assert text in caught.value.apic_text
        assert isinstance(caught.value, ApicError)

    async def test_it_is_not_retried_and_nothing_is_followed(
        self, call, answer, status, code, text
    ):
        """None of these is transient, and a Location is never visited."""
        apic = ScriptedApic(answer)
        with pytest.raises(ApicRequestError):
            await call(build_client(apic))

        assert len(apic.requests) == 1
        assert apic.hosts == {"apic.test"}


# ── The three failures as they were reported ──────────────────────────────────


class TestTheBugAsItWasObserved:
    async def test_count_is_not_the_error_bodys_total_count(self):
        """The APIC's 301 carries `totalCount: "1"`; count() reported 1."""
        client = build_client(ScriptedApic(reply(301, _APIC_301)))
        with pytest.raises(ApicRequestError, match="Incorrect filter format"):
            await client.count_class("faultInst", {}, filter_expr='eq(faultInst.code,"F0526")')

    async def test_query_does_not_return_the_error_object_as_a_result_row(self):
        """Parsed as data it became `[{"_class": "error"}]` with `complete: true`."""
        client = build_client(ScriptedApic(reply(301, _APIC_301)))
        with pytest.raises(ApicRequestError, match="Incorrect filter format"):
            await client.query_class("fvCtx", {}, filter_expr='eq(fvCtx.dn,"uni/tn-common/ctx-*")')

    async def test_a_refusal_in_a_200_is_not_a_result_either(self):
        client = build_client(ScriptedApic(reply(200, _APIC_301)))
        with pytest.raises(ApicRequestError):
            await client.query_class("fvBD", {})

    async def test_get_by_dn_does_not_report_a_refusal_as_not_found(self):
        """`None` means "no object at that DN"; a refusal says nothing about it."""
        client = build_client(ScriptedApic(reply(301, _APIC_301)))
        with pytest.raises(ApicRequestError):
            await client.get_by_dn("uni/tn-OT/BD-a")


# ── fetch_all: a refusal on any page ends the walk ────────────────────────────


def _page(index: int, total: int, size: int = _FETCH_ALL_PAGE_SIZE) -> dict[str, Any]:
    """Page `index` of a walk, holding `size` distinct objects."""
    rows = make_imdata_objects(
        "fvBD",
        [{"dn": f"uni/tn-OT/BD-{index}-{i}", "name": f"{index}-{i}"} for i in range(size)],
    )
    return {"totalCount": str(total), "imdata": rows}


_WALK_TOTAL = 3 * _FETCH_ALL_PAGE_SIZE

_LATE_REFUSALS = [
    pytest.param(reply(301, _APIC_301), id="301"),
    pytest.param(reply(302, headers={"Location": "https://apic.test/x"}), id="302"),
    pytest.param(reply(307, _APIC_301), id="307"),
    pytest.param(reply(200, _APIC_301), id="error-object-in-a-200"),
    pytest.param(reply(400, error_body("121", "Prop 'x' not found")), id="400"),
    pytest.param(
        reply(200, {"totalCount": str(_WALK_TOTAL), "imdata": [*_BD_ROWS, _ERROR_ITEM]}),
        id="mixed",
    ),
]


class TestFetchAllWalk:
    @pytest.mark.parametrize("bad_page", [0, 1, 2])
    @pytest.mark.parametrize("refusal", _LATE_REFUSALS)
    async def test_a_refusal_on_any_page_raises_rather_than_returning_a_partial_set(
        self, bad_page, refusal
    ):
        """The first page, a middle one, the last: none returns what was fetched before it."""
        answers = [reply(200, _page(i, _WALK_TOTAL)) for i in range(bad_page)] + [refusal]
        apic = ScriptedApic(*answers)
        with pytest.raises(ApicRequestError):
            await build_client(apic).query_class("fvBD", {}, fetch_all=True)

        assert len(apic.requests) == bad_page + 1, "the walk went on after the refusal"

    async def test_a_clean_walk_is_unchanged(self):
        total = _FETCH_ALL_PAGE_SIZE + 10
        apic = ScriptedApic(
            reply(200, _page(0, total)), reply(200, _page(1, total, size=10))
        )
        result = await build_client(apic).query_class("fvBD", {}, fetch_all=True)

        assert len(result.objects) == total
        assert result.complete is True


# ── What still reads as an answer ─────────────────────────────────────────────


class TestAnswersStayAnswers:
    """The guard must not turn an empty or ordinary answer into an error."""

    async def test_an_empty_set_is_an_empty_result(self):
        apic = ScriptedApic(reply(200, {"totalCount": "0", "imdata": []}))
        result = await build_client(apic).query_class("fvBD", {})
        assert result.objects == []
        assert result.total_available == 0

    async def test_a_count_of_zero_is_still_zero(self):
        apic = ScriptedApic(reply(200, {"totalCount": "0", "imdata": []}))
        assert await build_client(apic).count_class("fvBD", {}) == 0

    async def test_a_dn_with_no_object_is_still_not_found(self):
        apic = ScriptedApic(reply(200, {"totalCount": "0", "imdata": []}))
        assert await build_client(apic).get_by_dn("uni/tn-nope") is None

    async def test_ordinary_objects_come_back_as_before(self):
        apic = ScriptedApic(reply(200, _OK))
        result = await build_client(apic).query_class("fvBD", {})
        assert [o["name"] for o in result.objects] == ["a", "b"]
        assert {o["_class"] for o in result.objects} == {"fvBD"}

    async def test_a_2xx_other_than_200_with_a_body_is_still_read(self):
        apic = ScriptedApic(reply(202, _OK))
        result = await build_client(apic).query_class("fvBD", {})
        assert len(result.objects) == 2

    async def test_a_transient_status_is_still_retried_then_succeeds(self):
        apic = ScriptedApic(reply(503, error_body("503", "busy")), reply(200, _OK))
        result = await build_client(apic).query_class("fvBD", {})
        assert len(result.objects) == 2
        assert len(apic.requests) == 2

    async def test_a_transient_status_that_never_recovers_quotes_the_apic(self):
        apic = ScriptedApic(reply(503, error_body("503", "busy")))
        with pytest.raises(ApicRequestError, match="busy"):
            await build_client(apic).query_class("fvBD", {})
        assert len(apic.requests) == 3

    async def test_a_reauth_on_401_is_still_followed_by_the_retry(self):
        login = reply(200, {"imdata": [{"aaaLogin": {"attributes": {"token": "t"}}}]})
        apic = ScriptedApic(reply(401, error_body("401", "expired")), login, reply(200, _OK))
        result = await build_client(apic).query_class("fvBD", {})
        assert len(result.objects) == 2


# ── The 403 of a revoked session is still one re-login, then a retry ─────────

_REVOKED = error_body("403", "Token is valid but session for user 'admin' is invalid")
_LOGIN = reply(200, {"imdata": [{"aaaLogin": {"attributes": {"token": "fresh-token"}}}]})


class TestARevokedSessionStillReauthenticatesOnce:
    """The re-auth step sits before every check this change adds, and is untouched."""

    @pytest.mark.parametrize("status", [401, 403])
    async def test_one_login_then_the_retry_carries_the_new_token(self, status):
        apic = ScriptedApic(reply(status, _REVOKED), _LOGIN, reply(200, _OK))
        client = build_client(apic)
        client._client.cookies.set("APIC-cookie", "stale-token")

        result = await client.query_class("fvBD", {})

        assert len(result.objects) == 2
        methods = [request.method for request in apic.requests]
        assert methods == ["GET", "POST", "GET"]
        assert "fresh-token" in apic.requests[2].headers["cookie"]
        assert "stale-token" not in apic.requests[2].headers["cookie"]

    async def test_a_session_still_revoked_after_the_login_is_an_auth_error_not_a_request_error(self):
        apic = ScriptedApic(reply(403, _REVOKED), _LOGIN, reply(403, _REVOKED))
        with pytest.raises(ApicAuthError):
            await build_client(apic).count_class("fvBD", {})

        assert len(apic.requests) == 3, "more than one re-login was attempted"

    async def test_the_retry_after_the_login_is_checked_like_any_other_answer(self):
        """A re-login does not buy the retry an exemption from the checks."""
        apic = ScriptedApic(reply(403, _REVOKED), _LOGIN, reply(301, _APIC_301))
        with pytest.raises(ApicRequestError, match="Incorrect filter format"):
            await build_client(apic).count_class("fvBD", {})


# ── Malformed bodies ──────────────────────────────────────────────────────────


class TestEnvelopeShape:
    @pytest.mark.parametrize(
        "body",
        [
            pytest.param([], id="a-json-list"),
            pytest.param("imdata", id="a-json-string-that-contains-the-word"),
            pytest.param({"totalCount": "0"}, id="no-imdata"),
            pytest.param({"imdata": {"fvBD": {}}}, id="imdata-is-a-dict"),
            pytest.param({"imdata": "fvBD"}, id="imdata-is-a-string"),
            pytest.param(None, id="json-null"),
        ],
    )
    async def test_a_2xx_without_an_imdata_list_is_a_response_error(self, body):
        apic = ScriptedApic(lambda: httpx.Response(200, content=json.dumps(body).encode()))
        with pytest.raises(ApicResponseError):
            await build_client(apic).query_class("fvBD", {})

    async def test_a_2xx_that_is_not_json_is_a_response_error(self):
        apic = ScriptedApic(lambda: httpx.Response(200, content=b"<html>login</html>"))
        with pytest.raises(ApicResponseError):
            await build_client(apic).count_class("fvBD", {})

    async def test_an_error_status_that_is_not_json_still_raises_a_request_error(self):
        apic = ScriptedApic(lambda: httpx.Response(302, content=b"<html>moved</html>"))
        with pytest.raises(ApicRequestError) as caught:
            await build_client(apic).count_class("fvBD", {})
        assert caught.value.apic_text == ""

    async def test_an_error_status_with_a_non_dict_body_still_raises_a_request_error(self):
        apic = ScriptedApic(reply(301, ["not", "an", "envelope"]))
        with pytest.raises(ApicRequestError):
            await build_client(apic).count_class("fvBD", {})


# ── The text is bounded and one line ──────────────────────────────────────────


class TestTheApicTextIsSanitised:
    async def _refuse_with(self, code: Any, text: Any) -> ApicRequestError:
        body = {"totalCount": "1", "imdata": [{"error": {"attributes": {"code": code, "text": text}}}]}
        with pytest.raises(ApicRequestError) as caught:
            await build_client(ScriptedApic(reply(301, body))).count_class("fvBD", {})
        return caught.value

    async def test_newlines_and_control_characters_do_not_reach_the_message(self):
        # Built with chr() so the file carries no invisible characters of its own.
        line_separator, right_to_left_override = chr(0x2028), chr(0x202E)
        raw = f"line one\nline\ttwo\x00\x1b[31m red\r\n{line_separator}end{right_to_left_override}"
        error = await self._refuse_with("30\n1", raw)

        # Control characters are dropped, every run of whitespace becomes one space.
        assert error.apic_text == "line one line two[31m red end"
        assert error.apic_code == "30 1"
        assert not any(
            ord(ch) < 0x20 or ch in (line_separator, right_to_left_override)
            for ch in str(error)
        )

    async def test_a_runaway_text_is_cut_to_a_bound(self):
        error = await self._refuse_with("301", "x" * 50_000)

        assert len(error.apic_text) == _MAX_ERROR_TEXT
        assert error.apic_text.endswith("…")
        assert len(str(error)) < _MAX_ERROR_TEXT + 200

    async def test_an_integer_code_is_kept_and_a_structured_one_is_not_formatted(self):
        assert (await self._refuse_with(301, "t")).apic_code == "301"
        structured = await self._refuse_with({"a": 1}, ["b"])
        assert (structured.apic_code, structured.apic_text) == ("", "")

    async def test_a_code_that_repeats_the_status_is_not_said_twice(self):
        error = await self._refuse_with("301", "Incorrect filter format")
        assert "(APIC error code" not in str(error)
        assert "failed with HTTP 301: Incorrect filter format" in str(error)

    async def test_a_code_that_differs_from_the_status_is_said(self):
        apic = ScriptedApic(reply(200, error_body("301", "Incorrect filter format")))
        with pytest.raises(ApicRequestError) as caught:
            await build_client(apic).count_class("fvBD", {})
        assert "failed with HTTP 200 (APIC error code 301): Incorrect filter format" in str(
            caught.value
        )

    async def test_the_url_in_the_message_carries_no_query_string(self):
        """The filter is a query parameter; the message names the endpoint only."""
        apic = ScriptedApic(reply(301, _APIC_301))
        with pytest.raises(ApicRequestError) as caught:
            await build_client(apic).query_class(
                "faultInst", {}, filter_expr='eq(faultInst.code,"F0526")'
            )
        assert "?" not in str(caught.value).split(": ", 1)[0]
        assert "query-target-filter" not in caught.value.url


# ── Redirects are not followed, by construction ───────────────────────────────


def test_the_constructor_never_follows_redirects():
    """Set explicitly: a redirect would carry the session cookie to wherever it points."""
    client = ApicClient("apic.test", "admin", "pw")
    assert client._client.follow_redirects is False


async def test_a_mixed_answer_is_logged_as_discarded(caplog):
    apic = ScriptedApic(reply(200, {"totalCount": "3", "imdata": [*_BD_ROWS, _ERROR_ITEM]}))
    with caplog.at_level(logging.WARNING, logger="niwashi-mcp.apic"), pytest.raises(ApicRequestError):
        await build_client(apic).query_class("fvBD", {})

    assert any("discarding the response" in record.getMessage() for record in caplog.records)
