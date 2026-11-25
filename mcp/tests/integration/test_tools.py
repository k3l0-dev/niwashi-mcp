# Copyright (C) 2026 Khalid El-Ouiali — MONARK AIOPS srl
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
Integration tests for the five MCP tools: search_classes, get_schema, query,
get_by_dn, count.

Uses StubBackend and MINIMAL_DESCRIPTIONS from conftest so tests always run
without a live APIC or the full data/ schema collection.
"""


import pytest

from niwashi_mcp.exceptions import UnknownClassError
from tests.conftest import MINIMAL_DESCRIPTIONS, StubBackend, make_ctx

# ── Tool context helpers ──────────────────────────────────────────────────────


def _stub_ctx(sample_imdata, descriptions=None, *, cap_at=None):
    """Build a tool context with optional custom descriptions.

    No `schemas_dir`: 2.0 reads the object model from the catalogue inside the
    niwaki dependency, so there is no directory for a test to point at.
    """
    desc = descriptions if descriptions is not None else dict(MINIMAL_DESCRIPTIONS)
    return make_ctx(
        {
            "descriptions": desc,
            "backend": StubBackend(sample_imdata, cap_at=cap_at),
        }
    )


# ── search_classes ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_classes_returns_results(tool_ctx):
    from niwashi_mcp.main import search_classes

    results = await search_classes("bridge", tool_ctx)
    assert isinstance(results, list)
    assert len(results) > 0


@pytest.mark.asyncio
async def test_search_classes_result_shape(tool_ctx):
    from niwashi_mcp.main import search_classes

    results = await search_classes("tenant", tool_ctx)
    # Non-empty first: a `for` over an empty list asserts nothing, and an empty
    # result is exactly what a broken search returns.
    assert results, "search_classes returned nothing for a term in the fixture"
    for r in results:
        assert "class_name" in r
        assert "label" in r
        assert "comment" in r


@pytest.mark.asyncio
async def test_search_classes_limit_capped_at_50(tool_ctx):
    from niwashi_mcp.main import search_classes

    # Requesting 999 — must be capped at 50
    results = await search_classes("a", tool_ctx, limit=999)
    assert len(results) == 50  # equality: `<= 50` passed for any ceiling below it


@pytest.mark.asyncio
async def test_search_classes_limit_respected(tool_ctx):
    from niwashi_mcp.main import search_classes

    results = await search_classes("a", tool_ctx, limit=2)
    assert len(results) == 2


# ── search_classes — limit boundary values (0, -1, 1, cap, cap+1) ────────────


@pytest.mark.asyncio
async def test_search_classes_limit_zero_clamped_to_one(tool_ctx):
    """A limit of 0 is clamped to 1, not passed through to a [:0] slice."""
    from niwashi_mcp.main import search_classes

    results = await search_classes("a", tool_ctx, limit=0)
    assert len(results) == 1


@pytest.mark.asyncio
async def test_search_classes_limit_negative_clamped_to_one(tool_ctx):
    """A negative limit is clamped to 1 rather than mis-slicing results."""
    from niwashi_mcp.main import search_classes

    results = await search_classes("a", tool_ctx, limit=-1)
    assert len(results) == 1


@pytest.mark.asyncio
async def test_search_classes_limit_one_returns_exactly_one(tool_ctx):
    from niwashi_mcp.main import search_classes

    results = await search_classes("a", tool_ctx, limit=1)
    assert len(results) == 1


@pytest.mark.asyncio
async def test_search_classes_limit_at_cap_50_respected(tool_ctx):
    from niwashi_mcp.main import search_classes

    results = await search_classes("a", tool_ctx, limit=50)
    assert len(results) == 50  # equality: `<= 50` passed for any ceiling below it


@pytest.mark.asyncio
async def test_search_classes_limit_cap_plus_one_still_capped_at_50(tool_ctx):
    from niwashi_mcp.main import search_classes

    results = await search_classes("a", tool_ctx, limit=51)
    assert len(results) == 50  # equality: `<= 50` passed for any ceiling below it


@pytest.mark.asyncio
async def test_search_classes_no_match_returns_empty(tool_ctx):
    from niwashi_mcp.main import search_classes

    results = await search_classes("zzz_nonexistent_xyz_abc", tool_ctx)
    assert results == []


@pytest.mark.asyncio
async def test_search_classes_logs_result_count(tool_ctx):
    from niwashi_mcp.main import search_classes

    await search_classes("bridge", tool_ctx)
    tool_ctx.info.assert_called_once()
    call_args = tool_ctx.info.call_args[0][0]
    assert "search_classes" in call_args


# ── get_schema ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_schema_unknown_class_returns_empty(tool_ctx):
    from niwashi_mcp.main import get_schema

    schema = await get_schema("nonExistentClassXYZ", tool_ctx)
    assert schema == {}


@pytest.mark.asyncio
async def test_get_schema_unknown_class_logs_warning(tool_ctx):
    from niwashi_mcp.main import get_schema

    await get_schema("nonExistentClassXYZ", tool_ctx)
    tool_ctx.warning.assert_called_once()
    # ctx.warning reaches the MCP client as a log notification, so it is agent-
    # visible text, not a debug aid. Assert what it says: "a mock was called" is
    # true of any message, including an empty one.
    msg = tool_ctx.warning.call_args.args[0]
    assert "nonExistentClassXYZ" in msg and "not found" in msg


@pytest.mark.asyncio
async def test_get_schema_known_class_returns_required_fields(tool_ctx):
    # The skipif here guarded a `data/schemas` directory that 2.0 deleted and
    # .gitignore excludes, so it skipped in CI and in any clean checkout —
    # measured on `git archive HEAD`: 1 skipped, this one. The body needs no
    # such directory; the catalogue ships inside the niwaki dependency.
    from niwashi_mcp.main import get_schema

    schema = await get_schema("fvBD", tool_ctx)
    assert schema != {}
    for field in ("identifiedBy", "rnFormat", "containedBy"):
        assert field in schema, f"Missing field: {field}"


# ── query ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_query_returns_envelope(tool_ctx):
    from niwashi_mcp.main import query

    envelope = await query("fvTenant", tool_ctx)
    assert isinstance(envelope, dict)
    assert isinstance(envelope["results"], list)
    for key in (
        "results", "returned", "total_available", "truncated",
        "next_page", "complete", "note",
    ):
        assert key in envelope


@pytest.mark.asyncio
async def test_query_result_has_class_key(tool_ctx):
    from niwashi_mcp.main import query

    envelope = await query("fvBD", tool_ctx)
    results = envelope["results"]
    assert all("_class" in r for r in results)
    assert results, "query returned nothing — `all(...)` below is vacuous on []"
    assert all(r["_class"] == "fvBD" for r in results)


@pytest.mark.asyncio
async def test_query_with_equality_filter(tool_ctx):
    from niwashi_mcp.main import query

    envelope = await query("fvBD", tool_ctx, filters={"name": "servers"})
    results = envelope["results"]
    assert len(results) == 1
    assert results[0]["name"] == "servers"


@pytest.mark.asyncio
async def test_query_with_scope_dn_restricts_results(tool_ctx):
    from niwashi_mcp.main import query

    envelope = await query("fvBD", tool_ctx, scope_dn="uni/tn-OT")
    results = envelope["results"]
    assert len(results) >= 2
    assert all(r["dn"].startswith("uni/tn-OT/") for r in results)


@pytest.mark.asyncio
async def test_query_limit_capped_at_200(tool_ctx):
    from niwashi_mcp.main import query

    envelope = await query("fvBD", tool_ctx, limit=9999)
    # The clamp, asserted where it acts. `len(results) <= 200` held on a
    # three-object fixture whatever the ceiling was — including none at all.
    assert tool_ctx.lifespan_context["backend"].calls[-1]["limit"] == 200
    # …and the envelope still describes the fixture it actually got.
    assert envelope["returned"] == len(envelope["results"])
    assert envelope["returned"] <= envelope["total_available"]


@pytest.mark.asyncio
async def test_query_limit_applied(tool_ctx):
    from niwashi_mcp.main import query

    envelope = await query("fvBD", tool_ctx, limit=1)
    assert len(envelope["results"]) == 1


# ── query — limit boundary values (0, -1, 1, cap, cap+1) ─────────────────────


@pytest.mark.asyncio
async def test_query_limit_zero_clamped_to_one(tool_ctx):
    """A limit of 0 is clamped to 1, not forwarded to APIC as page-size=0."""
    from niwashi_mcp.main import query

    envelope = await query("fvBD", tool_ctx, limit=0)
    assert len(envelope["results"]) == 1


@pytest.mark.asyncio
async def test_query_limit_negative_clamped_to_one(tool_ctx):
    """A negative limit is clamped to 1, not forwarded to APIC as page-size=-1."""
    from niwashi_mcp.main import query

    envelope = await query("fvBD", tool_ctx, limit=-1)
    assert len(envelope["results"]) == 1


@pytest.mark.asyncio
async def test_query_limit_negative_reaches_backend_as_clamped_value(
    sample_imdata
):
    """The clamped value — not the raw negative input — is what the backend
    actually receives, so a negative limit never reaches the real APIC."""
    from niwashi_mcp.main import query

    ctx = _stub_ctx(sample_imdata)
    await query("fvBD", ctx, limit=-5)
    backend = ctx.lifespan_context["backend"]
    assert backend.calls[-1]["limit"] == 1


@pytest.mark.asyncio
async def test_query_limit_one_returns_exactly_one(tool_ctx):
    from niwashi_mcp.main import query

    envelope = await query("fvBD", tool_ctx, limit=1)
    assert len(envelope["results"]) == 1


@pytest.mark.asyncio
async def test_query_limit_at_cap_200_respected(tool_ctx):
    from niwashi_mcp.main import query

    envelope = await query("fvBD", tool_ctx, limit=200)
    # The clamp, asserted where it acts. `len(results) <= 200` held on a
    # three-object fixture whatever the ceiling was — including none at all.
    assert tool_ctx.lifespan_context["backend"].calls[-1]["limit"] == 200
    # …and the envelope still describes the fixture it actually got.
    assert envelope["returned"] == len(envelope["results"])
    assert envelope["returned"] <= envelope["total_available"]


@pytest.mark.asyncio
async def test_query_limit_cap_plus_one_still_capped_at_200(tool_ctx):
    from niwashi_mcp.main import query

    envelope = await query("fvBD", tool_ctx, limit=201)
    # The clamp, asserted where it acts. `len(results) <= 200` held on a
    # three-object fixture whatever the ceiling was — including none at all.
    assert tool_ctx.lifespan_context["backend"].calls[-1]["limit"] == 200
    # …and the envelope still describes the fixture it actually got.
    assert envelope["returned"] == len(envelope["results"])
    assert envelope["returned"] <= envelope["total_available"]


@pytest.mark.asyncio
async def test_query_order_by_asc(tool_ctx):
    from niwashi_mcp.main import query

    envelope = await query("fvBD", tool_ctx, order_by="fvBD.name|asc")
    names = [r["name"] for r in envelope["results"]]
    assert names == sorted(names)


@pytest.mark.asyncio
async def test_query_order_by_desc(tool_ctx):
    from niwashi_mcp.main import query

    envelope = await query("fvBD", tool_ctx, order_by="fvBD.name|desc")
    names = [r["name"] for r in envelope["results"]]
    assert names == sorted(names, reverse=True)


@pytest.mark.asyncio
async def test_query_none_filters_equivalent_to_empty(tool_ctx):
    from niwashi_mcp.main import query

    envelope_none = await query("fvTenant", tool_ctx, filters=None)
    envelope_empty = await query("fvTenant", tool_ctx, filters={})
    assert len(envelope_none["results"]) == len(envelope_empty["results"])


@pytest.mark.asyncio
async def test_query_include_children_populates_children_key(tool_ctx):
    from niwashi_mcp.main import query

    # fvBD "mgmt" in sample_imdata has a fvSubnet child
    envelope = await query("fvBD", tool_ctx, include_children=["fvSubnet"])
    mgmt = next((r for r in envelope["results"] if r["name"] == "mgmt"), None)
    assert mgmt is not None
    assert "_children" in mgmt
    assert mgmt["_children"][0]["_class"] == "fvSubnet"


# ── query — truncation envelope ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_query_truncated_false_when_returned_less_than_limit(tool_ctx):
    """Only 2 fvTenant objects exist in sample_imdata — well under the
    default limit=20 — so this is a natural end, not a partial page."""
    from niwashi_mcp.main import query

    envelope = await query("fvTenant", tool_ctx)
    assert envelope["returned"] < 20
    assert envelope["truncated"] is False
    assert envelope["complete"] is True
    assert envelope["note"] is None
    assert envelope["next_page"] is None


@pytest.mark.asyncio
async def test_query_truncated_true_reports_total_and_note(tool_ctx):
    """3 fvBD objects exist; limit=2 returns only a partial page."""
    from niwashi_mcp.main import query

    envelope = await query("fvBD", tool_ctx, limit=2)
    assert envelope["returned"] == 2
    assert envelope["total_available"] == 3
    assert envelope["truncated"] is True
    # Assert what the note SAYS, not that a string exists. Replacing the whole
    # note with "ok" passed. It is the only text telling an agent not to read a
    # partial page as a maximum, a total, or a complete list.
    note = envelope["note"]
    assert "Do not conclude" in note
    assert "fetch_all=True" in note
    assert f"{envelope['returned']} of {envelope['total_available']}" in note
    assert envelope["next_page"] == 1
    assert envelope["complete"] is True


@pytest.mark.asyncio
async def test_query_fetch_all_returns_everything(tool_ctx):
    from niwashi_mcp.main import query

    envelope = await query("fvBD", tool_ctx, limit=2, fetch_all=True)
    assert envelope["returned"] == 3
    assert envelope["total_available"] == 3
    assert envelope["truncated"] is False
    assert envelope["complete"] is True
    assert envelope["note"] is None
    assert envelope["next_page"] is None
    backend = tool_ctx.lifespan_context["backend"]
    assert backend.calls[-1]["fetch_all"] is True


# ── query — regression: argmax over a truncated page is wrong (Task 6) ───────
#
# The original bug this fix addresses: an agent asked "which bridge domain
# has the most subnets" called query(fvBD, include_children=[fvSubnet],
# limit=200), got exactly `limit` results, and computed argmax over that
# partial page — silently picking the wrong bridge domain because the real
# maximum was beyond page 1. This test encodes that scenario end to end.

_LARGE_BD_COUNT = 250
_ARGMAX_BD_INDEX = 100  # well beyond a default (or even limit=20) first page
_ARGMAX_SUBNET_COUNT = 12  # strictly greater than every other BD's count


def _make_bd_item(index: int, subnet_count: int) -> dict:
    """Build one fvBD imdata item, optionally carrying `subnet_count` fvSubnet
    children — mirrors make_imdata_objects()'s shape but inlines the children
    map per-object since counts vary per index here."""
    dn = f"uni/tn-OT/BD-bd{index:04d}"
    obj: dict = {"attributes": {"name": f"bd{index:04d}", "dn": dn}}
    if subnet_count:
        obj["children"] = [
            {
                "fvSubnet": {
                    "attributes": {
                        "ip": f"10.{index % 250}.{i}.1/24",
                        "dn": f"{dn}/subnet-[10.{index % 250}.{i}.1/24]",
                    }
                }
            }
            for i in range(subnet_count)
        ]
    return {"fvBD": obj}


def _build_large_bd_imdata() -> list[dict]:
    """250 fvBD objects; the one with the most fvSubnet children (index
    _ARGMAX_BD_INDEX) sits well beyond a default query() page."""
    return [
        _make_bd_item(
            i, _ARGMAX_SUBNET_COUNT if i == _ARGMAX_BD_INDEX else (i % 5)
        )
        for i in range(_LARGE_BD_COUNT)
    ]


def _argmax_bd_name(results: list[dict]) -> str:
    return max(results, key=lambda r: len(r.get("_children", [])))["name"]


@pytest.mark.asyncio
async def test_query_default_page_argmax_wrong_fetch_all_argmax_right():
    from niwashi_mcp.main import query

    ctx = _stub_ctx(_build_large_bd_imdata())
    true_argmax = f"bd{_ARGMAX_BD_INDEX:04d}"

    partial = await query("fvBD", ctx, include_children=["fvSubnet"])
    assert partial["truncated"] is True
    assert _argmax_bd_name(partial["results"]) != true_argmax

    complete = await query(
        "fvBD", ctx, include_children=["fvSubnet"], fetch_all=True
    )
    assert complete["truncated"] is False
    assert complete["complete"] is True
    assert complete["total_available"] == _LARGE_BD_COUNT
    assert complete["returned"] == _LARGE_BD_COUNT
    assert _argmax_bd_name(complete["results"]) == true_argmax


# ── query — unknown class (UnknownClassError) ─────────────────────────────────


@pytest.mark.asyncio
async def test_query_unknown_class_raises_unknown_class_error(tool_ctx):
    from niwashi_mcp.main import query

    with pytest.raises(UnknownClassError) as exc_info:
        await query("xyzTotallyFakeClass99", tool_ctx)
    assert exc_info.value.class_name == "xyzTotallyFakeClass99"


@pytest.mark.asyncio
async def test_query_unknown_class_error_includes_suggestions(
    sample_imdata
):
    from niwashi_mcp.main import query

    # Use a registry that contains "fvBD" so "fvBd" (typo) gets a suggestion
    ctx = _stub_ctx(sample_imdata, descriptions=dict(MINIMAL_DESCRIPTIONS))
    with pytest.raises(UnknownClassError) as exc_info:
        await query("fvBd", ctx)  # lowercase 'd' — typo
    # Should suggest fvBD
    assert "fvBD" in exc_info.value.suggestions or "fvBD" in str(exc_info.value)


@pytest.mark.asyncio
async def test_query_unknown_class_logs_warning(tool_ctx):
    from niwashi_mcp.main import query

    with pytest.raises(UnknownClassError):
        await query("xyzFakeClass", tool_ctx)
    tool_ctx.warning.assert_called_once()
    msg = tool_ctx.warning.call_args.args[0]
    assert "unknown class" in msg.lower() and "xyzFakeClass" in msg


@pytest.mark.asyncio
async def test_query_unknown_class_error_carries_registry_size(tool_ctx):
    from niwashi_mcp.main import query

    with pytest.raises(UnknownClassError) as exc_info:
        await query("xyzFakeClass", tool_ctx)
    assert exc_info.value.registry_size == len(
        tool_ctx.lifespan_context["descriptions"]
    )


# ── query — class validation ──────────────────────────────────────────────────
#
# Until 2.0 this validated in two tiers: the descriptions index, then a fallback
# to the schema files, because the two collections disagreed by 213 classes and
# a class missing from the first could still be perfectly queryable. Both now
# come from the same catalogue, so there is one source of truth and no fallback.


@pytest.mark.asyncio
async def test_query_rejects_a_class_absent_from_the_catalogue(sample_imdata):
    """An invented class name is rejected before the backend is ever called.

    This is the guard that matters: the APIC does not error on an unknown class,
    it returns an empty result — indistinguishable from "no such objects".
    """
    from niwashi_mcp.main import query

    ctx = _stub_ctx(sample_imdata, descriptions=dict(MINIMAL_DESCRIPTIONS))

    with pytest.raises(UnknownClassError):
        await query("totallyMadeUpClassNotAnywhere", ctx)
    assert not ctx.lifespan_context["backend"].calls


# ── query — config_only (Task 5) ──────────────────────────────────────────────

# imdata carrying an operational attribute (modTs) the stub strips under
# config_only, mirroring the APIC rsp-prop-include=config-only behaviour.
_CONFIG_IMDATA = [
    {
        "fvBD": {
            "attributes": {
                "name": "servers",
                "dn": "uni/tn-OT/BD-servers",
                "arpFlood": "no",
                "modTs": "2026-07-20T10:00:00.000+00:00",
                "lcOwn": "local",
            }
        }
    },
]


@pytest.mark.asyncio
async def test_query_config_only_passed_through(sample_imdata):
    from niwashi_mcp.main import query

    ctx = _stub_ctx(sample_imdata)
    await query("fvBD", ctx, config_only=True)
    call = ctx.lifespan_context["backend"].calls[-1]
    assert call["config_only"] is True


@pytest.mark.asyncio
async def test_query_config_only_strips_operational_attrs():
    from niwashi_mcp.main import query

    ctx = _stub_ctx(_CONFIG_IMDATA)
    envelope = await query("fvBD", ctx, config_only=True)
    results = envelope["results"]
    assert results[0]["name"] == "servers"
    assert "modTs" not in results[0]
    assert "lcOwn" not in results[0]


@pytest.mark.asyncio
async def test_query_without_config_only_keeps_all_attrs():
    from niwashi_mcp.main import query

    ctx = _stub_ctx(_CONFIG_IMDATA)
    envelope = await query("fvBD", ctx)
    assert "modTs" in envelope["results"][0]


# ── get_by_dn (Task 3) ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_by_dn_found_returns_object(tool_ctx):
    from niwashi_mcp.main import get_by_dn

    obj = await get_by_dn("uni/tn-OT/BD-servers", tool_ctx)
    assert obj["_class"] == "fvBD"
    assert obj["name"] == "servers"
    assert obj["dn"] == "uni/tn-OT/BD-servers"


@pytest.mark.asyncio
async def test_get_by_dn_not_found_returns_structured_error(tool_ctx):
    from niwashi_mcp.main import get_by_dn

    result = await get_by_dn("uni/tn-OT/BD-doesNotExist", tool_ctx)
    assert result["found"] is False
    assert result["dn"] == "uni/tn-OT/BD-doesNotExist"
    assert "No object exists" in result["message"]


@pytest.mark.asyncio
async def test_get_by_dn_not_found_logs_warning(tool_ctx):
    from niwashi_mcp.main import get_by_dn

    await get_by_dn("uni/tn-OT/BD-doesNotExist", tool_ctx)
    tool_ctx.warning.assert_called_once()
    msg = tool_ctx.warning.call_args.args[0]
    assert "uni/tn-OT/BD-doesNotExist" in msg and "not found" in msg


@pytest.mark.asyncio
async def test_get_by_dn_config_only_strips_operational_attrs():
    from niwashi_mcp.main import get_by_dn

    ctx = _stub_ctx(_CONFIG_IMDATA)
    obj = await get_by_dn("uni/tn-OT/BD-servers", ctx, config_only=True)
    assert obj["name"] == "servers"
    assert "modTs" not in obj
    call = ctx.lifespan_context["backend"].calls[-1]
    assert call["config_only"] is True


@pytest.mark.asyncio
async def test_get_by_dn_include_children_embeds_children(tool_ctx):
    from niwashi_mcp.main import get_by_dn

    # fvBD "mgmt" in sample_imdata carries a fvSubnet child
    obj = await get_by_dn("uni/tn-OT/BD-mgmt", tool_ctx, include_children=["fvSubnet"])
    assert obj["_class"] == "fvBD"
    assert "_children" in obj
    assert obj["_children"][0]["_class"] == "fvSubnet"


# ── count (Task 4) ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_count_plain_returns_total(tool_ctx):
    from niwashi_mcp.main import count

    result = await count("fvBD", tool_ctx)
    # sample_imdata carries three fvBD objects
    assert result["class_name"] == "fvBD"
    assert result["count"] == 3
    assert result["scope_dn"] is None
    assert result["filters"] == {}


@pytest.mark.asyncio
async def test_count_filtered(tool_ctx):
    from niwashi_mcp.main import count

    # two of the three BDs have arpFlood=no (servers, mgmt)
    result = await count("fvBD", tool_ctx, filters={"arpFlood": "no"})
    assert result["count"] == 2
    assert result["filters"] == {"arpFlood": "no"}


@pytest.mark.asyncio
async def test_count_scoped(tool_ctx):
    from niwashi_mcp.main import count

    result = await count("fvBD", tool_ctx, scope_dn="uni/tn-OT")
    assert result["count"] == 3
    assert result["scope_dn"] == "uni/tn-OT"


@pytest.mark.asyncio
async def test_count_unknown_class_raises_unknown_class_error(tool_ctx):
    from niwashi_mcp.main import count

    with pytest.raises(UnknownClassError) as exc_info:
        await count("xyzTotallyFakeClass99", tool_ctx)
    assert exc_info.value.class_name == "xyzTotallyFakeClass99"


@pytest.mark.asyncio
async def test_count_unknown_class_logs_warning(tool_ctx):
    from niwashi_mcp.main import count

    with pytest.raises(UnknownClassError):
        await count("xyzFakeClass", tool_ctx)
    tool_ctx.warning.assert_called_once()
    msg = tool_ctx.warning.call_args.args[0]
    assert "unknown class" in msg.lower() and "xyzFakeClass" in msg




# ── query — pagination terminates ─────────────────────────────────────────────
#
# Until 2.0 `truncated` compared total_available to the size of the page in
# hand, never to the offset consumed, so it was permanently true: page 2 of 45
# objects returned 5 and still said truncated, and page 99 returning 0 said it
# too. SKILL.md and docs/tools/query.md both tell an agent to "page until
# truncated is false", so an agent following the documented procedure looped
# until it ran out of turns, spending one APIC call each time.
#
# The suite never caught it because no test called query() past page 0.


def _paged_imdata(n: int) -> list[dict]:
    return [
        {"fvBD": {"attributes": {"dn": f"uni/tn-T/BD-{i}", "name": f"bd{i}"}}}
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_query_last_page_reports_not_truncated():
    """The page that exhausts the set must say so — this is the whole contract."""
    from niwashi_mcp.main import query

    ctx = _stub_ctx(_paged_imdata(45))
    envelope = await query("fvBD", ctx, limit=20, page=2)

    assert envelope["returned"] == 5
    assert envelope["truncated"] is False, "the last page must not claim more remains"
    assert envelope["next_page"] is None


@pytest.mark.asyncio
async def test_query_page_beyond_the_set_terminates():
    """An over-shot page returns nothing and says nothing remains.

    Without this, an agent that pages one step too far is told to keep going,
    forever.
    """
    from niwashi_mcp.main import query

    ctx = _stub_ctx(_paged_imdata(45))
    for page in (3, 99):
        envelope = await query("fvBD", ctx, limit=20, page=page)
        assert envelope["returned"] == 0
        assert envelope["truncated"] is False, f"page {page} still claims truncation"
        assert envelope["next_page"] is None


@pytest.mark.asyncio
async def test_query_intermediate_pages_still_advance():
    """The fix must not break the case pagination exists for."""
    from niwashi_mcp.main import query

    ctx = _stub_ctx(_paged_imdata(45))
    for page, expected_next in ((0, 1), (1, 2)):
        envelope = await query("fvBD", ctx, limit=20, page=page)
        assert envelope["truncated"] is True
        assert envelope["next_page"] == expected_next


@pytest.mark.asyncio
async def test_query_set_smaller_than_one_page_is_never_truncated():
    from niwashi_mcp.main import query

    ctx = _stub_ctx(_paged_imdata(5))
    envelope = await query("fvBD", ctx, limit=20)

    assert envelope["returned"] == 5
    assert envelope["truncated"] is False
    assert envelope["next_page"] is None


@pytest.mark.asyncio
async def test_query_fetch_all_is_never_truncated():
    """fetch_all walks every page itself, so nothing is left to fetch."""
    from niwashi_mcp.main import query

    ctx = _stub_ctx(_paged_imdata(45))
    envelope = await query("fvBD", ctx, fetch_all=True)

    assert envelope["returned"] == 45
    assert envelope["truncated"] is False
    assert envelope["next_page"] is None


@pytest.mark.asyncio
async def test_query_pagination_walk_terminates():
    """Walk the documented loop to exhaustion, as an agent would.

    The regression this pins is not a wrong flag on one page — it is a loop
    that never ends. Bounded at 10 iterations so a reintroduced bug fails the
    test rather than hanging the suite.
    """
    from niwashi_mcp.main import query

    ctx = _stub_ctx(_paged_imdata(45))
    seen, page, iterations = 0, 0, 0

    while iterations < 10:
        iterations += 1
        envelope = await query("fvBD", ctx, limit=20, page=page)
        seen += envelope["returned"]
        if not envelope["truncated"]:
            break
        page = envelope["next_page"]
    else:
        pytest.fail("pagination did not terminate within 10 pages")

    assert seen == 45, f"the walk collected {seen} of 45 objects"
    assert iterations == 3, f"45 objects at 20 per page should take 3 pages, took {iterations}"


# ── query — the two ways a partial answer can pass for a complete one ─────────
#
# These are the worst failure mode this server has: the caller receives fewer
# objects than match, and nothing in the envelope says so. Both paths below were
# unreachable from the test suite. The first because `StubBackend` hardcoded
# `complete=True` on both of its return paths, so the safety-cap branch of the
# `query` tool could never run; deleting that branch outright left the whole
# suite green. The second because no test ever combined `page > 0` with a
# `limit` outside [1, 200], which is where the clamp and the paging arithmetic
# have to agree.


@pytest.mark.asyncio
async def test_query_reports_the_safety_cap_to_the_agent(sample_imdata):
    """fetch_all stopping early must say so, in `complete` and in `note`.

    An agent that asked for everything and silently got part of it will state a
    maximum, a total, or a complete list from it. `complete=False` alone is not
    enough — the note is what tells it what to do next.
    """
    from niwashi_mcp.main import query

    ctx = _stub_ctx(sample_imdata, cap_at=1)
    envelope = await query("fvBD", ctx, fetch_all=True)

    assert envelope["complete"] is False
    assert envelope["returned"] < envelope["total_available"]
    assert envelope["note"], "a capped fetch_all returned no note at all"
    assert "safety cap" in envelope["note"]
    assert str(envelope["total_available"]) in envelope["note"]


@pytest.mark.asyncio
async def test_query_complete_fetch_all_carries_no_cap_note(sample_imdata):
    """The counterpart, so the assertion above cannot pass by always being true."""
    from niwashi_mcp.main import query

    envelope = await query("fvBD", _stub_ctx(sample_imdata), fetch_all=True)

    assert envelope["complete"] is True
    assert envelope["note"] is None


@pytest.mark.asyncio
async def test_query_truncation_survives_a_limit_that_gets_clamped():
    """`truncated` must be computed on the limit actually used, not the one asked for.

    With limit=500 clamped to 200 and page=2, consuming the raw 500 would make
    the server believe 1000 objects had been seen. It would report
    `truncated=false` and `next_page=null` on a result set of 1000 — an agent
    following the documented paging loop stops there, 400 objects short, with
    nothing saying so.
    """
    from niwashi_mcp.main import query

    imdata = [{"fvBD": {"attributes": {"name": f"bd{i}", "dn": f"uni/tn-T/BD-bd{i}"}}}
              for i in range(1000)]
    ctx = _stub_ctx(imdata)

    envelope = await query("fvBD", ctx, limit=500, page=2)

    assert ctx.lifespan_context["backend"].calls[-1]["limit"] == 200
    assert envelope["total_available"] == 1000
    assert envelope["truncated"] is True, "consumed was computed on the unclamped limit"
    assert envelope["next_page"] == 3
    assert envelope["note"] is not None


# ── field projection ──────────────────────────────────────────────────────────
#
# Projection happens in the tool layer, after the backend answers, so the stub
# needs no support for it: these tests assert what the tool does to what any
# backend returned. A wide class is ~40 attributes an object; `fields` exists so
# an agent can ask for the three it will read.

_WIDE_IMDATA = [
    {
        "fvBD": {
            "attributes": {
                "dn": "uni/tn-OT/BD-servers",
                "name": "servers",
                "descr": "prod servers",
                "arpFlood": "no",
                "unicastRoute": "yes",
                "modTs": "2026-01-01T00:00:00.000+00:00",
            },
            "children": [
                {
                    "fvSubnet": {
                        "attributes": {
                            "dn": "uni/tn-OT/BD-servers/subnet-[10.0.1.1/24]",
                            "ip": "10.0.1.1/24",
                            "scope": "private",
                            "descr": "gw",
                        }
                    }
                },
                {
                    "fvRsCtx": {
                        "attributes": {
                            "dn": "uni/tn-OT/BD-servers/rsctx",
                            "tnFvCtxName": "ot.main.vrf",
                            "state": "formed",
                        }
                    }
                },
            ],
        }
    },
    {
        "fvBD": {
            "attributes": {
                "dn": "uni/tn-OT/BD-clients",
                "name": "clients",
                "descr": "",
                "arpFlood": "yes",
                "unicastRoute": "yes",
                "modTs": "2026-01-02T00:00:00.000+00:00",
            }
        }
    },
    {
        "fvAEPg": {
            "attributes": {
                "dn": "uni/tn-OT/ap-prod/epg-web",
                "name": "web",
                "descr": "front end",
                "prio": "level1",
            }
        }
    },
]


@pytest.fixture
def strict_attributes(monkeypatch):
    """Attribute checking on, whatever the developer's shell exports."""
    monkeypatch.delenv("NIWASHI_MCP_STRICT_ATTRIBUTES", raising=False)


@pytest.fixture
def relaxed_attributes(monkeypatch):
    monkeypatch.setenv("NIWASHI_MCP_STRICT_ATTRIBUTES", "false")


@pytest.mark.asyncio
async def test_query_without_fields_returns_the_backends_objects_untouched():
    """`fields` unset must change nothing about the objects an agent receives.

    Compared against what the backend itself returns, serialised, so a stray
    reordering or a dropped key fails — and the envelope's key set is pinned, so
    the one intended difference (`fields: null`) is the only one.
    """
    import json

    from niwashi_mcp.main import query

    ctx = _stub_ctx(_WIDE_IMDATA)
    raw = await StubBackend(_WIDE_IMDATA).query_class(
        "fvBD", {}, include_children=["fvSubnet", "fvRsCtx"]
    )

    omitted = await query("fvBD", ctx, include_children=["fvSubnet", "fvRsCtx"])
    explicit = await query(
        "fvBD", ctx, include_children=["fvSubnet", "fvRsCtx"], fields=None
    )

    assert json.dumps(omitted["results"]) == json.dumps(raw.objects)
    assert omitted == explicit
    assert omitted["fields"] is None
    assert set(omitted) == {
        "results", "returned", "total_available", "truncated",
        "next_page", "complete", "note", "fields",
    }


@pytest.mark.asyncio
async def test_query_fields_reduces_every_result_to_the_listed_attributes(
    strict_attributes,
):
    from niwashi_mcp.main import query

    envelope = await query("fvBD", _stub_ctx(_WIDE_IMDATA), fields=["name", "arpFlood"])

    assert envelope["returned"] == 2
    for obj in envelope["results"]:
        # `_children` is kept wherever the APIC sent children, requested or not.
        assert set(obj) - {"_children"} == {"name", "arpFlood", "dn", "_class"}
        assert "descr" not in obj and "modTs" not in obj
    assert envelope["fields"] == ["name", "arpFlood"]
    assert envelope["note"] is None


@pytest.mark.asyncio
async def test_query_fields_never_drops_dn_class_or_children(strict_attributes):
    """An empty projection keeps exactly the three keys that cannot be removed.

    `dn` is how the object is addressed again, `_class` what it is, `_children`
    the subtree the caller asked to embed. Dropping any of them would leave a
    result nobody can follow up on.
    """
    from niwashi_mcp.main import query

    envelope = await query(
        "fvBD", _stub_ctx(_WIDE_IMDATA), include_children=["fvSubnet"], fields=[]
    )

    servers = next(o for o in envelope["results"] if o["dn"].endswith("BD-servers"))
    clients = next(o for o in envelope["results"] if o["dn"].endswith("BD-clients"))
    assert set(servers) == {"dn", "_class", "_children"}
    assert set(clients) == {"dn", "_class"}, "a `_children` key appeared from nowhere"
    assert envelope["fields"] == []
    assert "fields=[]" in envelope["note"], "an empty projection must say what it is"


@pytest.mark.asyncio
async def test_query_fields_naming_the_always_kept_keys_is_accepted(strict_attributes):
    from niwashi_mcp.main import query

    envelope = await query(
        "fvBD", _stub_ctx(_WIDE_IMDATA), fields=["dn", "_class", "_children", "name"]
    )

    assert {o["name"] for o in envelope["results"]} == {"servers", "clients"}


@pytest.mark.asyncio
async def test_query_fields_reduces_each_child_too(strict_attributes):
    from niwashi_mcp.main import query

    envelope = await query(
        "fvBD",
        _stub_ctx(_WIDE_IMDATA),
        filters={"name": "servers"},
        include_children=["fvSubnet", "fvRsCtx"],
        fields=["name", "ip", "tnFvCtxName"],
    )

    (servers,) = envelope["results"]
    assert set(servers) == {"name", "dn", "_class", "_children"}
    subnet, rsctx = servers["_children"]
    assert subnet == {
        "dn": "uni/tn-OT/BD-servers/subnet-[10.0.1.1/24]",
        "ip": "10.0.1.1/24",
        "_class": "fvSubnet",
    }
    assert rsctx == {
        "dn": "uni/tn-OT/BD-servers/rsctx",
        "tnFvCtxName": "ot.main.vrf",
        "_class": "fvRsCtx",
    }


@pytest.mark.asyncio
async def test_query_fields_valid_only_on_an_included_child_class_is_accepted(
    strict_attributes,
):
    """`ip` is a property of fvSubnet, not of fvBD.

    The same field is refused without the include: the pair is what proves the
    check consults `include_children` rather than being lax, or ignoring it.
    """
    from niwashi_mcp.exceptions import UnknownAttributeError
    from niwashi_mcp.main import query

    ctx = _stub_ctx(_WIDE_IMDATA)
    envelope = await query("fvBD", ctx, include_children=["fvSubnet"], fields=["ip"])
    assert envelope["returned"] == 2

    with pytest.raises(UnknownAttributeError) as exc:
        await query("fvBD", ctx, fields=["ip"])
    assert exc.value.attribute == "ip"


@pytest.mark.asyncio
async def test_query_unknown_field_is_refused_before_the_backend_is_called(
    strict_attributes,
):
    from niwashi_mcp.exceptions import UnknownAttributeError
    from niwashi_mcp.main import query

    ctx = _stub_ctx(_WIDE_IMDATA)

    with pytest.raises(UnknownAttributeError) as exc:
        await query("fvBD", ctx, fields=["name", "arpFlod"])

    assert exc.value.class_name == "fvBD"
    assert exc.value.attribute == "arpFlod"
    assert "arpFlood" in exc.value.suggestions
    assert exc.value.catalogue_version, "the release the check ran against is named"
    assert ctx.lifespan_context["backend"].calls == [], "the APIC was called anyway"
    ctx.warning.assert_called_once()
    assert "arpFlod" in ctx.warning.call_args.args[0]


@pytest.mark.asyncio
async def test_query_unknown_field_suggestions_reach_into_included_children(
    strict_attributes,
):
    from niwashi_mcp.exceptions import UnknownAttributeError
    from niwashi_mcp.main import query

    with pytest.raises(UnknownAttributeError) as exc:
        await query(
            "fvBD",
            _stub_ctx(_WIDE_IMDATA),
            include_children=["fvRsCtx"],
            fields=["tnFvCtxNam"],
        )

    assert exc.value.suggestions[:1] == ["tnFvCtxName"]


@pytest.mark.asyncio
async def test_query_unknown_field_when_relaxed_warns_and_says_so_in_the_note(
    relaxed_attributes,
):
    """Relaxed mode lets the request through; it must not let the doubt vanish.

    An attribute the catalogue does not know may still exist on a fabric running
    another release — or may be a typo. Either way the agent is told, in the
    answer it reads, rather than only in a log.
    """
    from niwashi_mcp.main import query

    ctx = _stub_ctx(_WIDE_IMDATA)
    envelope = await query("fvBD", ctx, fields=["name", "newFangledAttr"])

    assert envelope["returned"] == 2
    assert all(set(o) - {"_children"} == {"name", "dn", "_class"} for o in envelope["results"])
    assert "newFangledAttr" in envelope["note"]
    assert envelope["fields"] == ["name", "newFangledAttr"]
    assert any("newFangledAttr" in c.args[0] for c in ctx.warning.call_args_list)


@pytest.mark.asyncio
async def test_query_relaxed_unknown_field_keeps_the_truncation_note(
    relaxed_attributes,
):
    """The projection note is appended to the truncation note, never over it."""
    from niwashi_mcp.main import query

    envelope = await query(
        "fvBD", _stub_ctx(_WIDE_IMDATA), limit=1, fields=["newFangledAttr"]
    )

    assert envelope["truncated"] is True
    assert "partial page" in envelope["note"], "the truncation warning was clobbered"
    assert "newFangledAttr" in envelope["note"]


@pytest.mark.asyncio
async def test_query_fields_does_not_change_what_matched_or_how_many(strict_attributes):
    from niwashi_mcp.main import query

    ctx = _stub_ctx(_WIDE_IMDATA)
    full = await query("fvBD", ctx, limit=1)
    projected = await query("fvBD", ctx, limit=1, fields=["name"])

    for key in ("returned", "total_available", "truncated", "next_page", "complete"):
        assert projected[key] == full[key], key
    assert projected["results"][0]["dn"] == full["results"][0]["dn"]


def test_project_leaves_the_object_it_was_given_alone():
    import copy

    from niwashi_mcp.main import _project

    obj = {
        "dn": "d",
        "name": "n",
        "_class": "fvBD",
        "_children": [
            {"dn": "c", "ip": "i", "_class": "fvSubnet"},
            {"dn": "f", "severity": "major", "_class": "faultInst"},
        ],
    }
    before = copy.deepcopy(obj)

    out = _project(obj, frozenset({"name"}), frozenset({"fvBD", "fvSubnet"}))

    assert obj == before, "projection mutated the backend's object"
    assert out == {
        "dn": "d",
        "name": "n",
        "_class": "fvBD",
        "_children": [
            {"dn": "c", "_class": "fvSubnet"},
            {"dn": "f", "severity": "major", "_class": "faultInst"},
        ],
    }
    # A child left whole is still a copy: shrinking or editing the answer must
    # not reach back into the backend's dict.
    assert out["_children"][1] is not obj["_children"][1]


# ── field projection — which children it reduces ─────────────────────────────
#
# `fields` is validated against the queried class and the include_children
# classes, so those are the only children it may reduce. Children that came
# with `rsp_subtree_include` (faults, health) were never checked against it:
# `query("fvBD", rsp_subtree_include="faults", fields=["name"])` used to hand
# back each faultInst as `{"dn", "_class"}` — the fault the caller asked for,
# with its severity and code deleted, and no note saying so.

_BD_WITH_FAULT_IMDATA = [
    {
        "fvBD": {
            "attributes": {
                "dn": "uni/tn-OT/BD-servers",
                "name": "servers",
                "arpFlood": "no",
            },
            "children": [
                {
                    "fvSubnet": {
                        "attributes": {
                            "dn": "uni/tn-OT/BD-servers/subnet-[10.0.1.1/24]",
                            "ip": "10.0.1.1/24",
                            "scope": "private",
                        }
                    }
                },
                {
                    "faultInst": {
                        "attributes": {
                            "dn": "uni/tn-OT/BD-servers/fault-F0467",
                            "code": "F0467",
                            "severity": "major",
                            "descr": "invalid path",
                        }
                    }
                },
            ],
        }
    }
]

_FAULT_WHOLE = {
    "dn": "uni/tn-OT/BD-servers/fault-F0467",
    "code": "F0467",
    "severity": "major",
    "descr": "invalid path",
    "_class": "faultInst",
}


def _child(obj: dict, cls: str) -> dict:
    (found,) = [c for c in obj["_children"] if c["_class"] == cls]
    return found


@pytest.mark.asyncio
async def test_query_fields_leaves_rsp_subtree_include_children_whole(
    strict_attributes,
):
    from niwashi_mcp.main import query

    envelope = await query(
        "fvBD",
        _stub_ctx(_BD_WITH_FAULT_IMDATA),
        rsp_subtree_include="faults",
        fields=["name"],
    )

    (bd,) = envelope["results"]
    assert set(bd) == {"name", "dn", "_class", "_children"}
    assert _child(bd, "faultInst") == _FAULT_WHOLE
    # The subnet was not asked for with include_children, so it is not ours to
    # reduce either: only the classes `fields` was checked against are.
    assert _child(bd, "fvSubnet")["scope"] == "private"


@pytest.mark.asyncio
async def test_query_fields_still_reduces_the_include_children_classes(
    strict_attributes,
):
    """The other half of the rule: the same call, with the subnet embedded by
    `include_children`, reduces the subnet and still leaves the fault alone."""
    from niwashi_mcp.main import query

    envelope = await query(
        "fvBD",
        _stub_ctx(_BD_WITH_FAULT_IMDATA),
        include_children=["fvSubnet"],
        rsp_subtree_include="faults",
        fields=["name", "ip"],
    )

    (bd,) = envelope["results"]
    assert _child(bd, "fvSubnet") == {
        "dn": "uni/tn-OT/BD-servers/subnet-[10.0.1.1/24]",
        "ip": "10.0.1.1/24",
        "_class": "fvSubnet",
    }
    assert _child(bd, "faultInst") == _FAULT_WHOLE


@pytest.mark.asyncio
async def test_query_fields_names_valid_only_on_an_rsp_subtree_child_are_refused(
    strict_attributes,
):
    """`severity` belongs to faultInst, which `fields` neither reduces nor
    checks, so it selects nothing on a bridge domain. Refusing it is honest —
    accepting it would read as "severity was projected" when the fault arrives
    whole either way — and the closest-names list points at fvBD's own."""
    from niwashi_mcp.exceptions import UnknownAttributeError
    from niwashi_mcp.main import query

    ctx = _stub_ctx(_BD_WITH_FAULT_IMDATA)
    with pytest.raises(UnknownAttributeError) as exc:
        await query(
            "fvBD", ctx, rsp_subtree_include="faults", fields=["name", "severity"]
        )

    assert exc.value.attribute == "severity"
    assert ctx.lifespan_context["backend"].calls == []


@pytest.mark.asyncio
async def test_get_by_dn_fields_leaves_children_of_other_classes_whole(
    strict_attributes,
):
    from niwashi_mcp.main import get_by_dn

    obj = await get_by_dn(
        "uni/tn-OT/BD-servers",
        _stub_ctx(_BD_WITH_FAULT_IMDATA),
        include_children=["fvSubnet"],
        fields=["name", "ip"],
    )

    assert set(obj) == {"name", "dn", "_class", "_children"}
    assert _child(obj, "fvSubnet") == {
        "dn": "uni/tn-OT/BD-servers/subnet-[10.0.1.1/24]",
        "ip": "10.0.1.1/24",
        "_class": "fvSubnet",
    }
    assert _child(obj, "faultInst") == _FAULT_WHOLE


# A comma-joined item is one entry of the list and two classes on the wire. The
# class check always split it; `fields` did not, so `["fvSubnet,fvRsCtx"]` was
# accepted by one and refused (or mis-projected) by the other.
_COMMA_FORMS = [
    pytest.param(["fvSubnet,fvRsCtx"], id="joined"),
    pytest.param(["fvSubnet, fvRsCtx"], id="joined-with-space"),
    pytest.param(["fvSubnet", "fvRsCtx,"], id="trailing-comma"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("include_children", _COMMA_FORMS)
async def test_query_fields_reads_a_comma_joined_include_children_item_as_classes(
    strict_attributes, include_children
):
    from niwashi_mcp.main import query

    envelope = await query(
        "fvBD",
        _stub_ctx(_WIDE_IMDATA),
        filters={"name": "servers"},
        include_children=include_children,
        fields=["name", "ip"],
    )

    (servers,) = envelope["results"]
    # `ip` is fvSubnet's: it was accepted, and the subnet was reduced with it.
    assert [set(c) for c in servers["_children"]] == [
        {"ip", "dn", "_class"},
        {"dn", "_class"},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("include_children", _COMMA_FORMS)
async def test_get_by_dn_fields_reads_a_comma_joined_include_children_item_as_classes(
    strict_attributes, include_children
):
    from niwashi_mcp.main import get_by_dn

    obj = await get_by_dn(
        "uni/tn-OT/BD-servers",
        _stub_ctx(_WIDE_IMDATA),
        include_children=include_children,
        fields=["name", "ip"],
    )

    assert [set(c) for c in obj["_children"]] == [{"ip", "dn", "_class"}, {"dn", "_class"}]


@pytest.mark.asyncio
async def test_query_fields_suggestions_reach_into_a_comma_joined_item(
    strict_attributes,
):
    from niwashi_mcp.exceptions import UnknownAttributeError
    from niwashi_mcp.main import query

    with pytest.raises(UnknownAttributeError) as exc:
        await query(
            "fvBD",
            _stub_ctx(_WIDE_IMDATA),
            include_children=["fvSubnet,fvRsCtx"],
            fields=["tnFvCtxNam"],
        )

    assert exc.value.suggestions[:1] == ["tnFvCtxName"]


# ── field projection — get_by_dn ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_by_dn_without_fields_is_unchanged():
    from niwashi_mcp.main import get_by_dn

    ctx = _stub_ctx(_WIDE_IMDATA)
    omitted = await get_by_dn("uni/tn-OT/BD-servers", ctx, include_children=["fvSubnet"])
    explicit = await get_by_dn(
        "uni/tn-OT/BD-servers", ctx, include_children=["fvSubnet"], fields=None
    )

    assert omitted == explicit
    assert {"descr", "arpFlood", "modTs", "unicastRoute"} <= set(omitted)


@pytest.mark.asyncio
async def test_get_by_dn_fields_reduces_the_object_and_its_children(strict_attributes):
    from niwashi_mcp.main import get_by_dn

    obj = await get_by_dn(
        "uni/tn-OT/BD-servers",
        _stub_ctx(_WIDE_IMDATA),
        include_children=["fvSubnet", "fvRsCtx"],
        fields=["name", "ip"],
    )

    assert set(obj) == {"name", "dn", "_class", "_children"}
    assert obj["_class"] == "fvBD"
    assert [set(c) for c in obj["_children"]] == [
        {"ip", "dn", "_class"},
        {"dn", "_class"},
    ]
    assert "_note" not in obj


@pytest.mark.asyncio
async def test_get_by_dn_validates_fields_against_the_class_that_came_back(
    strict_attributes,
):
    """The DN carries the class, so the caller never names one to check against.

    `arpFlood` is a property of fvBD and not of fvAEPg; the object at this DN is
    an fvAEPg, so it must be refused — and named as an fvAEPg in the error.
    """
    from niwashi_mcp.exceptions import UnknownAttributeError
    from niwashi_mcp.main import get_by_dn

    ctx = _stub_ctx(_WIDE_IMDATA)

    ok = await get_by_dn("uni/tn-OT/ap-prod/epg-web", ctx, fields=["prio"])
    assert set(ok) == {"prio", "dn", "_class"}

    with pytest.raises(UnknownAttributeError) as exc:
        await get_by_dn("uni/tn-OT/ap-prod/epg-web", ctx, fields=["arpFlood"])
    assert exc.value.class_name == "fvAEPg"
    assert exc.value.attribute == "arpFlood"
    ctx.warning.assert_called_once()


@pytest.mark.asyncio
async def test_get_by_dn_field_valid_only_on_an_included_child_is_accepted(
    strict_attributes,
):
    from niwashi_mcp.exceptions import UnknownAttributeError
    from niwashi_mcp.main import get_by_dn

    ctx = _stub_ctx(_WIDE_IMDATA)
    obj = await get_by_dn(
        "uni/tn-OT/BD-servers", ctx, include_children=["fvSubnet"], fields=["ip"]
    )
    assert obj["_children"][0]["ip"] == "10.0.1.1/24"

    with pytest.raises(UnknownAttributeError):
        await get_by_dn("uni/tn-OT/BD-servers", ctx, fields=["ip"])


@pytest.mark.asyncio
async def test_get_by_dn_unknown_field_when_relaxed_is_noted_on_the_object(
    relaxed_attributes,
):
    from niwashi_mcp.main import get_by_dn

    ctx = _stub_ctx(_WIDE_IMDATA)
    obj = await get_by_dn("uni/tn-OT/BD-servers", ctx, fields=["name", "newFangledAttr"])

    assert set(obj) - {"_children"} == {"name", "dn", "_class", "_note"}
    assert "newFangledAttr" in obj["_note"]
    assert any("newFangledAttr" in c.args[0] for c in ctx.warning.call_args_list)


@pytest.mark.asyncio
async def test_get_by_dn_empty_fields_keeps_only_the_fixed_keys(strict_attributes):
    from niwashi_mcp.main import get_by_dn

    obj = await get_by_dn("uni/tn-OT/BD-clients", _stub_ctx(_WIDE_IMDATA), fields=[])

    assert {k for k in obj if k != "_note"} == {"dn", "_class"}
    assert "fields=[]" in obj["_note"]


@pytest.mark.asyncio
async def test_get_by_dn_not_found_with_fields_still_says_not_found(strict_attributes):
    """No object, so no class to validate against — and no reason to hide that."""
    from niwashi_mcp.main import get_by_dn

    result = await get_by_dn(
        "uni/tn-OT/BD-nope", _stub_ctx(_WIDE_IMDATA), fields=["notAnAttribute"]
    )

    assert result["found"] is False
    assert result["dn"] == "uni/tn-OT/BD-nope"
