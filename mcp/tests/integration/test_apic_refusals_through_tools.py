# Copyright (C) 2026 Khalid El-Ouiali — MONARK AIOPS srl
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
The tools, over the real client, when the APIC answers with something that is not data.

tests/integration/test_tool_error_surface.py proves the tools let a *raised*
backend failure through. This file closes the gap before it: that the real
ApicClient raises for the wire answers that used to be read as data — a 3xx (the
APIC answers an invalid filter value with a `301`, no Location, an error body),
and an `error` object in `imdata` whatever the status, alone or beside data rows
(tests/unit/test_client_non_2xx.py has the client side and the measurements).

Observed, before the fix, through the tools:

    count(faultInst, filter_expr='eq(faultInst.code,"F0526")')  ->  count: 1
    query(fvCtx, filter_expr='eq(fvCtx.dn,"uni/tn-common/ctx-*")')
                                -> results: [{"_class": "error"}], complete: true

`totalCount` of an error body is "1"; the error object parsed as an object has
a class, "error", and no attributes. Each is a wrong answer in the register of a
right one, which is the one outcome the server is built to avoid: a refusal must
be a tool error, never a number and never a row.

Every tool path is here: a single page, a scope, children, every page of a
fetch_all walk, count, count with group_by (both of its requests), and get_by_dn.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from niwashi_mcp.exceptions import ApicRequestError, SecretAttributeError, SecretDnError
from niwashi_mcp.main import count, get_by_dn, query
from tests.conftest import (
    MINIMAL_DESCRIPTIONS,
    apic_response,
    make_ctx,
    make_imdata_objects,
)
from tests.unit.test_client_non_2xx import ScriptedApic, build_client, error_body, reply

_FETCH_PAGE = 200

_APIC_301 = error_body(
    "301", "Incorrect filter format for faultInst.code, value 'F0526' is not valid"
)
_ERROR_ITEM = {"error": {"attributes": {"code": "301", "text": "Incorrect filter format"}}}
_ROWS = make_imdata_objects(
    "fvBD", [{"dn": "uni/tn-OT/BD-a", "name": "a", "arpFlood": "no"}]
)

# What the APIC can answer instead of data. The same set the client tests use.
_REFUSALS = [
    pytest.param(reply(301, _APIC_301), id="301-as-the-apic-sends-it"),
    pytest.param(reply(302, headers={"Location": "https://elsewhere.test/login"}), id="302"),
    pytest.param(reply(307, headers={"Location": "https://apic.test/api/x"}), id="307"),
    pytest.param(reply(200, _APIC_301), id="error-object-in-a-200"),
    pytest.param(reply(400, error_body("121", "Prop 'x' not found")), id="error-object-in-a-400"),
    pytest.param(
        reply(200, {"totalCount": "2", "imdata": [*_ROWS, _ERROR_ITEM]}), id="mixed-rows-and-error"
    ),
]


def _ctx(apic: ScriptedApic):
    return make_ctx(
        {"descriptions": dict(MINIMAL_DESCRIPTIONS), "backend": build_client(apic)}
    )


def _page(index: int, total: int) -> dict[str, Any]:
    rows = make_imdata_objects(
        "fvBD",
        [
            {"dn": f"uni/tn-OT/BD-{index}-{i}", "name": f"{index}-{i}", "arpFlood": "no"}
            for i in range(_FETCH_PAGE)
        ],
    )
    return {"totalCount": str(total), "imdata": rows}


def _first_count(total: int = 3) -> Callable[[], Any]:
    """The answer to count_class's one-object request, ahead of a group_by fetch."""
    return reply(200, {"totalCount": str(total), "imdata": _ROWS})


# ── query ─────────────────────────────────────────────────────────────────────

_QUERIES: dict[str, Callable[[Any], Awaitable[Any]]] = {
    "single-page": lambda ctx: query("fvBD", ctx),
    "explicit-page": lambda ctx: query("fvBD", ctx, page=2, limit=50),
    "filter_expr": lambda ctx: query("fvCtx", ctx, filter_expr='eq(fvCtx.dn,"uni/tn-common/ctx-*")'),
    "scoped": lambda ctx: query("fvBD", ctx, scope_dn="uni/tn-OT"),
    "include_children": lambda ctx: query("fvBD", ctx, include_children=["fvRsCtx"]),
    "rsp_subtree_include": lambda ctx: query("fvBD", ctx, rsp_subtree_include="faults"),
    "fetch_all": lambda ctx: query("fvBD", ctx, fetch_all=True),
}


@pytest.mark.parametrize("refusal", _REFUSALS)
class TestQuery:
    @pytest.mark.parametrize("call", list(_QUERIES.values()), ids=list(_QUERIES))
    async def test_a_refusal_is_a_tool_error_not_a_result_row(self, call, refusal):
        apic = ScriptedApic(refusal)

        with pytest.raises(ApicRequestError):
            await call(_ctx(apic))

        assert len(apic.requests) == 1

    @pytest.mark.parametrize("bad_page", [1, 2])
    async def test_a_refusal_on_a_later_fetch_all_page_is_a_tool_error_too(self, refusal, bad_page):
        total = 3 * _FETCH_PAGE
        apic = ScriptedApic(*[reply(200, _page(i, total)) for i in range(bad_page)], refusal)

        with pytest.raises(ApicRequestError):
            await query("fvBD", _ctx(apic), fetch_all=True)

        assert len(apic.requests) == bad_page + 1


# ── count ─────────────────────────────────────────────────────────────────────

_COUNTS: dict[str, Callable[[Any], Awaitable[Any]]] = {
    "plain": lambda ctx: count("fvBD", ctx),
    "filtered": lambda ctx: count("fvBD", ctx, filters={"arpFlood": "no"}),
    "filter_expr": lambda ctx: count(
        "faultInst", ctx, filter_expr='eq(faultInst.code,"F0526")'
    ),
    "scoped": lambda ctx: count("fvBD", ctx, scope_dn="uni/tn-OT"),
}


@pytest.mark.parametrize("refusal", _REFUSALS)
class TestCount:
    @pytest.mark.parametrize("call", list(_COUNTS.values()), ids=list(_COUNTS))
    async def test_a_refusal_is_a_tool_error_not_a_count(self, call, refusal):
        """The error body's `totalCount` is "1"; count() used to return it."""
        apic = ScriptedApic(refusal)

        with pytest.raises(ApicRequestError):
            await call(_ctx(apic))

        assert len(apic.requests) == 1

    async def test_group_by_fails_when_the_count_request_is_refused(self, refusal):
        apic = ScriptedApic(refusal)

        with pytest.raises(ApicRequestError):
            await count("fvBD", _ctx(apic), group_by="arpFlood")

        assert len(apic.requests) == 1

    async def test_group_by_fails_when_the_fetch_behind_it_is_refused(self, refusal):
        """The count is clean, the breakdown's fetch is not: no total without groups."""
        apic = ScriptedApic(_first_count(), refusal)

        with pytest.raises(ApicRequestError):
            await count("fvBD", _ctx(apic), group_by="arpFlood")

        assert len(apic.requests) == 2

    async def test_group_by_fails_when_a_later_page_of_the_fetch_is_refused(self, refusal):
        total = 2 * _FETCH_PAGE
        apic = ScriptedApic(_first_count(total), reply(200, _page(0, total)), refusal)

        with pytest.raises(ApicRequestError):
            await count("fvBD", _ctx(apic), group_by="arpFlood")

        assert len(apic.requests) == 3


# ── get_by_dn ─────────────────────────────────────────────────────────────────

_DN = "uni/tn-OT/BD-a"


@pytest.mark.parametrize("refusal", _REFUSALS)
class TestGetByDn:
    async def test_a_refusal_is_neither_found_nor_not_found(self, refusal):
        """`found: False` says "no object at that DN"; a refusal says nothing about it."""
        apic = ScriptedApic(refusal)

        with pytest.raises(ApicRequestError):
            await get_by_dn(_DN, _ctx(apic))

        assert len(apic.requests) == 1

    async def test_with_children_a_refusal_is_still_an_error(self, refusal):
        apic = ScriptedApic(refusal)

        with pytest.raises(ApicRequestError):
            await get_by_dn(_DN, _ctx(apic), include_children=["fvRsCtx"], config_only=True)


class TestTheSameCallsStillAnswerWhenTheApicDoes:
    """The guard refuses refusals, nothing else: found, not found, zero and a page stay answers."""

    async def test_get_by_dn_found(self):
        result = await get_by_dn(_DN, _ctx(ScriptedApic(reply(200, apic_response(_ROWS)))))
        assert result["_class"] == "fvBD"
        assert result["dn"] == _DN
        assert "found" not in result

    async def test_get_by_dn_not_found(self):
        empty = reply(200, {"totalCount": "0", "imdata": []})
        result = await get_by_dn(_DN, _ctx(ScriptedApic(empty)))
        assert result["found"] is False

    async def test_count_zero_is_zero(self):
        empty = reply(200, {"totalCount": "0", "imdata": []})
        result = await count("fvBD", _ctx(ScriptedApic(empty)))
        assert result["count"] == 0

    async def test_count_group_by_over_a_clean_set(self):
        apic = ScriptedApic(_first_count(1), reply(200, apic_response(_ROWS)))
        result = await count("fvBD", _ctx(apic), group_by="arpFlood")
        assert result["count"] == 1
        assert result["groups"] == {"no": 1}
        assert result["complete"] is True

    async def test_query_a_clean_page(self):
        result = await query("fvBD", _ctx(ScriptedApic(reply(200, apic_response(_ROWS)))))
        assert [row["name"] for row in result["results"]] == ["a"]
        assert result["complete"] is True


# ── What the refusal says, and what could be in it ────────────────────────────


class TestTheErrorTheAgentReceives:
    async def test_it_quotes_the_apic_so_the_agent_can_correct_the_value(self):
        apic = ScriptedApic(reply(301, _APIC_301))

        with pytest.raises(ApicRequestError) as caught:
            await count("faultInst", _ctx(apic), filter_expr='eq(faultInst.code,"F0526")')

        assert caught.value.status == 301
        assert caught.value.apic_code == "301"
        assert "Incorrect filter format for faultInst.code" in str(caught.value)

    async def test_a_secret_cannot_be_echoed_because_it_is_never_sent(self):
        """The APIC quotes the value it refuses; for a secret, no request is ever made.

        `redact.check_request` runs before the client is called, so a predicate on a
        secret attribute, or a value that spells a secret DN, never reaches the APIC to
        be echoed back in an error text. What can be echoed is a value on an ordinary
        attribute — the caller's own, already in the log line the tool writes.
        """
        guess = "zzguess"
        echo = reply(
            301,
            error_body("301", f"Incorrect filter format for vnsCCred.value, value '{guess}' is not valid"),
        )
        apic = ScriptedApic(echo)
        ctx = _ctx(apic)

        with pytest.raises(SecretAttributeError):
            await query("vnsCCred", ctx, filters={"value": guess})
        with pytest.raises(SecretAttributeError):
            await count("vnsCCred", ctx, filter_expr=f'eq(vnsCCred.value,"{guess}")')
        with pytest.raises(SecretDnError):
            await get_by_dn(f"uni/fabric/snmpp-default/community-{guess}", ctx)
        with pytest.raises(SecretDnError):
            await query("faultInst", ctx, scope_dn=f"uni/fabric/snmpp-default/community-{guess}")

        assert apic.requests == [], "a request that names a secret reached the APIC"
