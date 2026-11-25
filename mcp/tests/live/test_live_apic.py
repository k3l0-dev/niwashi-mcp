# Copyright (C) 2026 Khalid El-Ouiali — MONARK AIOPS srl
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
tests/live/test_live_apic.py

End-to-end tests against a real Cisco APIC (or the APIC simulator lab
instance) via apic.client.ApicClient directly — no StubBackend, no
FakeHTTPClient standing in for the transport. These are the only tests in
the whole suite that see APIC's actual error-response shape, actual DN
formats, and actual attribute sets; everything else in tests/unit and
tests/integration either tests pure logic or a Python reimplementation of
the backend (StubBackend).

All tests here depend on the session-scoped `live_client` fixture (see
conftest.py), which auto-skips the whole session when the simulator is
unreachable, and are marked @pytest.mark.live so the default `uv run
pytest` (which excludes `live` via pyproject.toml's addopts) never runs
them. Run explicitly with:

    uv run pytest tests/live/ -m live
"""

from collections import Counter

import pytest

from niwashi_mcp.exceptions import ApicRequestError

# `live` keeps this suite out of the default run (see pyproject.toml
# addopts). `asyncio(loop_scope="session")` binds every test in this module
# to the same session-scoped event loop as the `live_client` fixture — an
# async resource created in one event loop (the httpx connection pool
# opened during authenticate()) cannot be reused from a different one.
pytestmark = [pytest.mark.live, pytest.mark.asyncio(loop_scope="session")]


# ── query_class() — real objects, real DN formats ─────────────────────────────


async def test_query_class_fvtenant_returns_real_objects(live_client):
    """fvTenant always has at least one instance on a live APIC (uni/tn-common
    at minimum) — assert real objects with the real 'uni/tn-<name>' DN format
    come back, not a StubBackend-shaped approximation."""
    tenants = (await live_client.query_class("fvTenant", {}, limit=20)).objects
    assert len(tenants) > 0
    for t in tenants:
        assert t["_class"] == "fvTenant"
        assert t["dn"].startswith("uni/tn-")
        assert t["name"]


async def test_query_class_fvbd_returns_real_objects(live_client):
    """fvBD objects on the lab fabric — real dn format is 'uni/tn-X/BD-Y'."""
    bds = (await live_client.query_class("fvBD", {}, limit=20)).objects
    assert len(bds) > 0
    for bd in bds:
        assert bd["_class"] == "fvBD"
        assert "/BD-" in bd["dn"]


async def test_query_class_config_only_returns_fewer_attributes(live_client):
    """rsp-prop-include=config-only must return a strict subset of the
    attribute keys the full response carries, for the *same* real object —
    filtered by name to guarantee both requests hit the identical instance
    rather than comparing two arbitrary limit=1 picks."""
    full = (await live_client.query_class("fvBD", {}, limit=1)).objects
    assert full, "no fvBD objects on the lab fabric to test against"
    name = full[0]["name"]

    config = (
        await live_client.query_class(
            "fvBD", {"name": name}, limit=1, config_only=True
        )
    ).objects
    assert config
    assert len(config[0]) < len(full[0])
    assert set(config[0]) <= set(full[0])


# ── get_by_dn() — found and not-found cases ───────────────────────────────────


async def test_get_by_dn_found_matches_query_result(live_client):
    """Fetch a real DN from a query_class() result, then re-fetch it
    directly via get_by_dn() — the found case."""
    bds = (await live_client.query_class("fvBD", {}, limit=1)).objects
    assert bds, "no fvBD objects on the lab fabric to test against"
    dn = bds[0]["dn"]

    obj = await live_client.get_by_dn(dn)
    assert obj is not None
    assert obj["dn"] == dn
    assert obj["_class"] == "fvBD"


async def test_get_by_dn_not_found_returns_none(live_client):
    """A deliberately-wrong DN — the not-found case returns None, exactly
    like the APIC's real empty-imdata response for a missing DN."""
    obj = await live_client.get_by_dn(
        "uni/tn-aci-mcp-live-test-does-not-exist/BD-nope"
    )
    assert obj is None


# ── count_class() ──────────────────────────────────────────────────────────────


async def test_count_class_agrees_with_query_total_available(live_client):
    """count_class() and query_class() must report the same size for the same
    result set.

    This is the invariant the previous version of this test missed. It only
    asserted `total >= 0`, which a broken count satisfies trivially — and the
    count did break: the APIC `rsp-subtree-include=count` mechanism this
    client relied on returned a `moCount` that disagreed with reality by
    nearly 2x fabric-wide (203 vs. 403 fvBD). Comparing the two tools against
    each other catches that on any fabric without hardcoding instance counts
    that differ per lab.
    """
    counted = await live_client.count_class("fvBD", {})
    queried = await live_client.query_class("fvBD", {}, limit=1)
    assert counted == queried.total_available
    assert counted > 0, "no fvBD objects on the lab fabric to test against"


async def test_count_class_scoped_to_a_tenant_is_not_silently_zero(live_client):
    """A count scoped to a subtree must report that subtree's real size.

    Pins the sharpest edge of the old bug: a scoped count came back as 0
    against tenants holding up to 192 bridge domains. Zero is the one wrong
    answer an agent will not question — it reads as "none configured in this
    tenant" rather than as a failed lookup.

    The tenant is the *busiest* one on the fabric, not an arbitrary first
    pick. That matters, because the old idiom failed on only 5 of the 28
    tenants holding bridge domains here: a test scoped to whichever tenant
    happened to come back first passed against the broken code on this very
    lab. Targeting the largest subtree maximises the chance of landing on a
    real discrepancy instead of relying on luck of the draw — it is still a
    sampling test, not a proof, which is why the unit-level guard above pins
    the request shape directly.
    """
    bds = (await live_client.query_class("fvBD", {}, limit=200)).objects
    assert bds, "no fvBD objects on the lab fabric to test against"

    per_tenant = Counter("/".join(bd["dn"].split("/")[:2]) for bd in bds)
    tenant_dn, expected_at_least = per_tenant.most_common(1)[0]

    counted = await live_client.count_class("fvBD", {}, scope_dn=tenant_dn)
    queried = await live_client.query_class("fvBD", {}, scope_dn=tenant_dn, limit=1)
    assert counted == queried.total_available
    assert counted >= expected_at_least, (
        f"scoped count returned {counted} for {tenant_dn}, but at least "
        f"{expected_at_least} fvBD were already seen under it"
    )


async def test_count_class_with_filter_agrees_with_query(live_client):
    """A filtered count must match the filtered query's totalCount.

    The filtered case was wrong too, and less obviously so than the scoped
    one — it returned a plausible-looking number that was simply not the
    right one (99 against a real 203).
    """
    bds = (await live_client.query_class("fvBD", {}, limit=1)).objects
    assert bds, "no fvBD objects on the lab fabric to test against"
    name = bds[0]["name"]

    counted = await live_client.count_class("fvBD", {"name": name})
    queried = await live_client.query_class("fvBD", {"name": name}, limit=1)
    assert counted == queried.total_available
    assert counted > 0


# ── error handling — real APIC error-response shape ──────────────────────────


async def test_query_class_bad_filter_expr_raises_apic_request_error(live_client):
    """A filter_expr referencing a nonexistent property on fvBD is rejected
    by the real APIC with an HTTP 400 and a non-empty, human-readable error
    body — exactly the response shape StubBackend can never produce, since
    it has no concept of APIC's own filter/property validation."""
    with pytest.raises(ApicRequestError) as exc_info:
        await live_client.query_class(
            "fvBD", {}, filter_expr='eq(fvBD.notARealPropertyXYZ,"x")'
        )
    assert exc_info.value.status == 400
    assert exc_info.value.apic_text


# ── fetch_all and the fabric version — what only a real APIC can confirm ─────
#
# Both of these were written against the documented shape of the APIC's
# answers, and neither had been run against a fabric when it was added: the unit
# tests prove the logic, these prove the assumptions (that an APIC accepts
# page-size=200 and keeps totalCount stable across pages; that a controller
# reports its release in a form niwashi_mcp.apic.version can read).


async def test_fetch_all_walks_a_multi_page_class_to_its_total(live_client):
    """fvBD on the lab fabric has more than one 200-object page; a fetch_all
    walk must return exactly totalCount distinct objects and say it is complete."""
    probe = await live_client.query_class("fvBD", {}, limit=1)
    if not 200 < probe.total_available <= 5000:
        pytest.skip(
            f"fvBD has {probe.total_available} objects here; this test needs "
            "more than one 200-object page and fewer than the 5,000 cap"
        )

    result = await live_client.query_class("fvBD", {}, fetch_all=True)

    assert result.complete is True
    assert result.total_available == probe.total_available
    assert len(result.objects) == result.total_available
    assert len({o["dn"] for o in result.objects}) == len(result.objects), (
        "a page boundary repeated or dropped an object"
    )


async def test_controller_version_is_a_release_string_we_can_read(live_client):
    """The fabric's own answer must parse, or the startup check degrades to
    "unknown" on every real fabric."""
    from niwashi_mcp.apic.version import normalise_apic_version

    reported = await live_client.controller_version()

    assert reported, "the APIC reported no controller firmware version"
    assert normalise_apic_version(reported) is not None, (
        f"firmwareCtrlrRunning.version {reported!r} is not in a format "
        "normalise_apic_version understands — teach it, or the version check is blind"
    )


# ── A value the APIC refuses is an error, not an answer ───────────────────────
#
# Measured on APIC 6.0(9c): an `eq`/`ne` filter whose value is not valid for its
# attribute is answered `HTTP/1.1 301 Moved Permanently` — no Location header,
# body `{"totalCount": "1", "imdata": [{"error": {"attributes": {"code": "301",
# "text": "Incorrect filter format for <class>.<attr>, value '<v>' is not
# valid"}}}]}`. The 301 is the APIC's own error code used as the status, not a
# redirect. The client used to parse that body as data: count() returned its
# totalCount ("1") for a code that does not exist, and query() returned the error
# object as a row. All read-only GETs; nothing is written.


async def test_an_invalid_enum_value_raises_with_the_apics_reason(live_client):
    """`unicastRoute` is yes|no; "False" is the value that was measured."""
    with pytest.raises(ApicRequestError) as caught:
        await live_client.query_class("fvBD", {"unicastRoute": "False"}, limit=1)

    assert caught.value.status == 301, "the APIC's answer to an invalid value has changed"
    assert caught.value.apic_code == "301"
    assert "fvBD.unicastRoute" in caught.value.apic_text


async def test_count_of_a_code_the_apic_refuses_raises_instead_of_counting_one(live_client):
    """The eval's 13 bogus counts: eq(faultInst.code, <not a defined code>)."""
    with pytest.raises(ApicRequestError, match="Incorrect filter format"):
        await live_client.count_class(
            "faultInst", {}, filter_expr='eq(faultInst.code,"F0526")'
        )


async def test_a_dn_filter_with_a_star_raises_instead_of_returning_an_error_row(live_client):
    """The eval's fvCtx query: the second operand ('x') is the invalid value."""
    with pytest.raises(ApicRequestError, match="Incorrect filter format"):
        await live_client.query_class(
            "fvCtx",
            {},
            filter_expr='or(eq(fvCtx.dn,"uni/tn-common/ctx-*"),eq(fvCtx.dn,"x"))',
            fetch_all=True,
        )


async def test_a_refused_value_inside_and_or_ne_raises_too(live_client):
    with pytest.raises(ApicRequestError, match="Incorrect filter format"):
        await live_client.count_class(
            "fvBD", {}, filter_expr='and(eq(fvBD.name,"x"),eq(fvBD.unicastRoute,"False"))'
        )
    with pytest.raises(ApicRequestError, match="Incorrect filter format"):
        await live_client.count_class(
            "faultInst", {}, filter_expr='ne(faultInst.code,"F9999")'
        )


async def test_a_value_the_apic_accepts_still_answers(live_client):
    """The guard must not refuse what the APIC answers: a valid enum value, and a
    wcard on the same attribute (a string match, which the APIC does not validate)."""
    assert await live_client.count_class("fvBD", {"unicastRoute": "yes"}) >= 0
    assert await live_client.count_class(
        "faultInst", {}, filter_expr='wcard(faultInst.code,"F0526")'
    ) >= 0
