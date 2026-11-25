# Copyright (C) 2026 Khalid El-Ouiali — MONARK AIOPS srl
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
Integration tests for attribute validation in `query` and `count`.

The APIC answers an unknown attribute — or an unknown class in
`include_children` — with HTTP 200 and an empty result, which is
indistinguishable from "there are none". `query` and `count` therefore check
every name the caller supplies against the catalogue *before* the request is
built, from four sources: `filters` keys, the `order_by` attribute,
`include_children` classes, and the `Class.attr` references inside
`filter_expr`.

Two modes, switched by NIWASHI_MCP_STRICT_ATTRIBUTES:

  strict (default)  the first unknown name raises, and no request is sent
  not strict        the request is sent, and every problem is reported —
                    `ctx.warning`, plus the result (`note` for query,
                    `warnings` for count). It must never be silent.

Strict-mode tests assert on `backend.calls == []` rather than only on the
exception: an implementation that raised *after* the request had gone out would
satisfy `pytest.raises` and still have spent an APIC round-trip on a name it
knew was wrong.
"""

import pytest

from niwashi_mcp.exceptions import (
    FilterError,
    UnknownAttributeError,
    UnknownClassError,
)
from tests.conftest import StubBackend, make_ctx

_ENV = "NIWASHI_MCP_STRICT_ATTRIBUTES"


@pytest.fixture(autouse=True)
def _default_mode(monkeypatch):
    """Every test starts from the shipped default; relaxed-mode tests opt out."""
    monkeypatch.delenv(_ENV, raising=False)


@pytest.fixture
def relaxed(monkeypatch):
    monkeypatch.setenv(_ENV, "false")


def _backend(ctx) -> StubBackend:
    return ctx.lifespan_context["backend"]


def _primary_guard_suggestions(ctx, name: str) -> list[str]:
    """What the older `class_name` guard suggests for `name`.

    A secondary class is reported through the same `UnknownClassError`, built
    the same way, so a caller sees one kind of answer to one kind of mistake.
    """
    from niwashi_mcp.registry.descriptions import search

    hits = search(name, ctx.lifespan_context["descriptions"], limit=5)
    return [h["class_name"] for h in hits]


def _warned(ctx) -> list[str]:
    return [str(call.args[0]) for call in ctx.warning.await_args_list]


# ── the mode switch itself ───────────────────────────────────────────────────


@pytest.mark.parametrize("value", [None, "", "true", "TRUE", "1", "yes", "on", "garbage"])
def test_anything_but_an_explicit_off_stays_strict(monkeypatch, value):
    """A typo in the variable must not silently disable the check."""
    from niwashi_mcp.main import _strict_attributes

    if value is None:
        monkeypatch.delenv(_ENV, raising=False)
    else:
        monkeypatch.setenv(_ENV, value)
    assert _strict_attributes() is True


@pytest.mark.parametrize("value", ["false", "False", "FALSE", "0", "no", "off", " false "])
def test_the_explicit_off_spellings_relax_it(monkeypatch, value):
    from niwashi_mcp.main import _strict_attributes

    monkeypatch.setenv(_ENV, value)
    assert _strict_attributes() is False


def test_the_mode_is_read_per_call_not_at_import(monkeypatch):
    from niwashi_mcp.main import _strict_attributes

    monkeypatch.setenv(_ENV, "false")
    assert _strict_attributes() is False
    monkeypatch.setenv(_ENV, "true")
    assert _strict_attributes() is True


# ── strict: filters ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_query_unknown_filter_attribute_raises_and_sends_nothing(tool_ctx):
    from niwashi_mcp.main import query

    with pytest.raises(UnknownAttributeError) as caught:
        await query("fvBD", tool_ctx, filters={"nope": "x"})

    assert caught.value.class_name == "fvBD"
    assert caught.value.attribute == "nope"
    assert _backend(tool_ctx).calls == []


@pytest.mark.asyncio
async def test_count_unknown_filter_attribute_raises_and_sends_nothing(tool_ctx):
    from niwashi_mcp.main import count

    with pytest.raises(UnknownAttributeError):
        await count("fvBD", tool_ctx, filters={"nope": "x"})

    assert _backend(tool_ctx).calls == []


@pytest.mark.asyncio
async def test_the_error_carries_the_closest_real_names(tool_ctx):
    """The point of checking locally: the caller can fix it without a
    get_schema() round-trip."""
    from niwashi_mcp.main import query

    with pytest.raises(UnknownAttributeError) as caught:
        await query("fvBD", tool_ctx, filters={"Name": "x"})

    assert "name" in caught.value.suggestions
    assert "6.0(9c)" in str(caught.value)


@pytest.mark.asyncio
async def test_a_bad_key_after_good_ones_is_still_found(tool_ctx):
    from niwashi_mcp.main import count

    with pytest.raises(UnknownAttributeError) as caught:
        await count("fvBD", tool_ctx, filters={"name": "a", "arpFlood": "no", "zzz": "b"})

    assert caught.value.attribute == "zzz"
    assert _backend(tool_ctx).calls == []


@pytest.mark.asyncio
async def test_refusal_is_logged_to_the_client(tool_ctx):
    from niwashi_mcp.main import query

    with pytest.raises(UnknownAttributeError):
        await query("fvBD", tool_ctx, filters={"nope": "x"})

    assert any("nope" in w for w in _warned(tool_ctx))


@pytest.mark.asyncio
async def test_real_attributes_and_system_attributes_pass(tool_ctx):
    """The check must not be a tax on valid requests — least of all `dn`, which
    every APIC object carries and every wcard filter uses."""
    from niwashi_mcp.main import count, query

    envelope = await query(
        "fvBD",
        tool_ctx,
        filters={"name": "servers", "arpFlood": "no", "dn": "uni/tn-OT/BD-servers"},
    )
    assert envelope["returned"] == 1
    assert envelope["note"] is None
    result = await count("fvBD", tool_ctx, filters={"rn": "BD-servers", "status": ""})
    assert "warnings" not in result


@pytest.mark.asyncio
async def test_a_malformed_key_is_still_a_filter_error_not_an_unknown_attribute(tool_ctx):
    """`bad-attr` is a malformed request. Reporting it as a missing attribute
    would hide the error that says what is actually wrong, and change a
    contract the existing FilterError tests pin."""
    from niwashi_mcp.main import query

    backend = _backend(tool_ctx)

    async def refuse(**kwargs):
        raise FilterError("Invalid attribute 'bad-attr'")

    backend.query_class = refuse
    with pytest.raises(FilterError):
        await query("fvBD", tool_ctx, filters={"bad-attr": "x"})


# ── strict: abstract classes ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_abstract_class_accepts_an_attribute_only_a_subclass_declares(tool_ctx):
    """`hostBasedRouting` is declared by concrete fvBD, not by abstract fvABD,
    yet a query against fvABD returns fvBD instances carrying it. Refusing it
    would reject a request the APIC answers correctly."""
    from niwashi_mcp.main import count, query

    envelope = await query("fvABD", tool_ctx, filters={"hostBasedRouting": "yes"})
    assert envelope["returned"] == 0
    assert (await count("fvABD", tool_ctx, filters={"hostBasedRouting": "yes"}))["count"] == 0
    await query("fvABD", tool_ctx, filter_expr='eq(fvABD.hostBasedRouting,"yes")')
    await query("fvABD", tool_ctx, order_by="fvABD.hostBasedRouting|asc")
    assert len(_backend(tool_ctx).calls) == 4


@pytest.mark.asyncio
async def test_an_abstract_class_still_refuses_a_name_no_subclass_has(tool_ctx):
    from niwashi_mcp.main import query

    with pytest.raises(UnknownAttributeError):
        await query("fvABD", tool_ctx, filters={"nope": "x"})
    assert _backend(tool_ctx).calls == []


# ── strict: order_by ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_order_by_attribute_raises_and_sends_nothing(tool_ctx):
    from niwashi_mcp.main import query

    with pytest.raises(UnknownAttributeError) as caught:
        await query("fvBD", tool_ctx, order_by="fvBD.nope|desc")

    assert (caught.value.class_name, caught.value.attribute) == ("fvBD", "nope")
    assert _backend(tool_ctx).calls == []


@pytest.mark.asyncio
async def test_order_by_is_checked_against_the_class_in_the_token(tool_ctx):
    """`severity` is a real attribute — of faultInst. Ordering fvBD results by
    `fvBD.severity` is wrong, and the error must name the class the token
    names."""
    from niwashi_mcp.main import query

    with pytest.raises(UnknownAttributeError) as caught:
        await query("fvBD", tool_ctx, order_by="fvBD.severity|asc")
    assert caught.value.class_name == "fvBD"

    # Conversely, a token naming another class is validated against *that* class
    # rather than against class_name.
    await query("fvBD", tool_ctx, order_by="faultInst.severity|desc")
    assert len(_backend(tool_ctx).calls) == 1


@pytest.mark.asyncio
async def test_every_term_of_a_comma_separated_order_by_is_checked(tool_ctx):
    from niwashi_mcp.main import query

    with pytest.raises(UnknownAttributeError) as caught:
        await query("faultInst", tool_ctx, order_by="faultInst.severity|desc,faultInst.zzz")

    assert caught.value.attribute == "zzz"
    assert _backend(tool_ctx).calls == []


@pytest.mark.asyncio
async def test_valid_order_by_passes_through_unchanged(tool_ctx):
    from niwashi_mcp.main import query

    await query("fvBD", tool_ctx, order_by="fvBD.name|asc")
    assert _backend(tool_ctx).calls[0]["order_by"] == "fvBD.name|asc"


# ── strict: include_children ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_include_children_class_raises_unknown_class_error(tool_ctx):
    from niwashi_mcp.main import query

    with pytest.raises(UnknownClassError) as caught:
        await query("fvBD", tool_ctx, include_children=["fvSubnet", "fvSubnett"])

    assert caught.value.class_name == "fvSubnett"
    assert caught.value.suggestions == _primary_guard_suggestions(tool_ctx, "fvSubnett")
    assert _backend(tool_ctx).calls == []


@pytest.mark.asyncio
async def test_known_include_children_classes_pass(tool_ctx):
    from niwashi_mcp.main import query

    await query("fvBD", tool_ctx, include_children=["fvSubnet", "fvRsCtx"])
    assert _backend(tool_ctx).calls[0]["include_children"] == ["fvSubnet", "fvRsCtx"]


@pytest.mark.asyncio
async def test_a_comma_joined_include_children_item_keeps_working(tool_ctx):
    """The client joins the items with "," into rsp-subtree-class, so
    ["fvSubnet,fvRsCtx"] has always reached the APIC as two classes. Treating
    the whole string as one class name would refuse a request that works."""
    from niwashi_mcp.main import query

    await query("fvBD", tool_ctx, include_children=["fvSubnet,fvRsCtx"])
    assert len(_backend(tool_ctx).calls) == 1

    with pytest.raises(UnknownClassError) as caught:
        await query("fvBD", tool_ctx, include_children=["fvSubnet, fvRsCtxx"])
    assert caught.value.class_name == "fvRsCtxx"


# ── strict: filter_expr ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_attribute_in_filter_expr_raises_and_sends_nothing(tool_ctx):
    from niwashi_mcp.main import query

    expr = 'and(eq(fvBD.name,"x"),wcard(fvBD.nope,"y"))'
    with pytest.raises(UnknownAttributeError) as caught:
        await query("fvBD", tool_ctx, filter_expr=expr)

    assert (caught.value.class_name, caught.value.attribute) == ("fvBD", "nope")
    assert _backend(tool_ctx).calls == []


@pytest.mark.asyncio
async def test_count_unknown_attribute_in_filter_expr_raises(tool_ctx):
    from niwashi_mcp.main import count

    with pytest.raises(UnknownAttributeError):
        await count("fvBD", tool_ctx, filter_expr='eq(fvBD.nope,"y")')
    assert _backend(tool_ctx).calls == []


@pytest.mark.asyncio
async def test_unknown_class_in_filter_expr_raises_unknown_class_error(tool_ctx):
    from niwashi_mcp.main import query

    with pytest.raises(UnknownClassError) as caught:
        await query("fvBD", tool_ctx, filter_expr='eq(fvBDD.name,"x")')

    assert caught.value.class_name == "fvBDD"
    assert caught.value.suggestions == _primary_guard_suggestions(tool_ctx, "fvBDD")
    assert _backend(tool_ctx).calls == []


@pytest.mark.asyncio
async def test_a_reference_to_another_known_class_is_validated_against_that_class(tool_ctx):
    from niwashi_mcp.main import query

    await query("fvBD", tool_ctx, filter_expr='eq(fvTenant.name,"OT")')
    with pytest.raises(UnknownAttributeError) as caught:
        await query("fvBD", tool_ctx, filter_expr='eq(fvTenant.arpFlood,"no")')
    assert caught.value.class_name == "fvTenant"


@pytest.mark.asyncio
async def test_dotted_text_inside_a_quoted_value_is_never_validated(tool_ctx):
    """`10.1.1.1`, `a.b` and `ghost.attr` all sit inside string literals. They
    are values, not references, and must reach the backend untouched."""
    from niwashi_mcp.main import count, query

    for expr in (
        'wcard(fvBD.dn,"uni/tn-a.b.c")',
        'eq(fvBD.descr,"ghost.attr")',
        'eq(fvBD.descr,"eq(ghost.attr,1)")',
        r'eq(fvBD.descr,"say \"x.y\" ghost.attr")',
    ):
        await query("fvBD", tool_ctx, filter_expr=expr)
        await count("fvBD", tool_ctx, filter_expr=expr)

    assert len(_backend(tool_ctx).calls) == 8


@pytest.mark.asyncio
async def test_what_the_extractor_does_not_recognise_is_forwarded_untouched(tool_ctx):
    from niwashi_mcp.main import query

    for expr in ("not(a filter)", 'eq(name,"x")', "garbage", 'eq(fvBD.dn,uni.tn)'):
        await query("fvBD", tool_ctx, filter_expr=expr)

    assert [c["filter_expr"] for c in _backend(tool_ctx).calls] == [
        "not(a filter)",
        'eq(name,"x")',
        "garbage",
        "eq(fvBD.dn,uni.tn)",
    ]


@pytest.mark.asyncio
async def test_filters_and_filter_expr_are_both_checked(tool_ctx):
    """A clean `filters` must not excuse a bad `filter_expr`, or vice versa."""
    from niwashi_mcp.main import query

    with pytest.raises(UnknownAttributeError) as caught:
        await query(
            "fvBD", tool_ctx, filters={"name": "x"}, filter_expr='eq(fvBD.nope,"y")'
        )
    assert caught.value.attribute == "nope"

    with pytest.raises(UnknownAttributeError) as caught:
        await query(
            "fvBD", tool_ctx, filters={"nope2": "x"}, filter_expr='eq(fvBD.name,"y")'
        )
    assert caught.value.attribute == "nope2"
    assert _backend(tool_ctx).calls == []


# ── strict: the default output is untouched ──────────────────────────────────


@pytest.mark.asyncio
async def test_a_valid_request_returns_exactly_the_shape_it_always_did(tool_ctx):
    from niwashi_mcp.main import count, query

    envelope = await query("fvBD", tool_ctx, filters={"name": "servers"})
    assert set(envelope) == {
        "results", "returned", "total_available",
        "truncated", "next_page", "complete", "note", "fields",
    }
    assert envelope["note"] is None
    # `fields` is the projection echo added alongside validation; unset, it is null.
    assert envelope["fields"] is None

    result = await count("fvBD", tool_ctx, filters={"name": "servers"})
    assert result == {
        "class_name": "fvBD",
        "count": 1,
        "scope_dn": None,
        "filters": {"name": "servers"},
    }


# ── not strict ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_relaxed_query_sends_the_request_and_notes_the_problem(tool_ctx, relaxed):
    from niwashi_mcp.main import query

    envelope = await query("fvBD", tool_ctx, filters={"nope": "x"})

    assert _backend(tool_ctx).calls[0]["filters"] == {"nope": "x"}
    assert envelope["returned"] == 0
    assert "nope" in envelope["note"]
    assert "fvBD" in envelope["note"]
    assert any("nope" in w for w in _warned(tool_ctx))


@pytest.mark.asyncio
async def test_relaxed_count_sends_the_request_and_adds_warnings(tool_ctx, relaxed):
    from niwashi_mcp.main import count

    result = await count("fvBD", tool_ctx, filters={"nope": "x"})

    assert _backend(tool_ctx).calls[0]["filters"] == {"nope": "x"}
    assert result["count"] == 0
    assert isinstance(result["warnings"], list)
    assert len(result["warnings"]) == 1
    assert "nope" in result["warnings"][0]
    assert any("nope" in w for w in _warned(tool_ctx))


@pytest.mark.asyncio
async def test_relaxed_count_without_a_problem_has_no_warnings_key(tool_ctx, relaxed):
    """The key exists only when there is something to say, so relaxing the mode
    does not change the shape of a clean answer."""
    from niwashi_mcp.main import count

    result = await count("fvBD", tool_ctx, filters={"name": "servers"})
    assert "warnings" not in result
    assert _warned(tool_ctx) == []


@pytest.mark.asyncio
async def test_relaxed_query_without_a_problem_has_no_note(tool_ctx, relaxed):
    from niwashi_mcp.main import query

    envelope = await query("fvBD", tool_ctx, filters={"name": "servers"})
    assert envelope["note"] is None
    assert _warned(tool_ctx) == []


@pytest.mark.asyncio
async def test_relaxed_mode_reports_all_four_sources(tool_ctx, relaxed):
    """One problem per source, each independently surfaced — in the note and in
    the log — and the request still goes out with every one of them in it."""
    from niwashi_mcp.main import query

    envelope = await query(
        "fvBD",
        tool_ctx,
        filters={"badFilterKey": "x"},
        order_by="fvBD.badOrderKey|asc",
        include_children=["fvBadChild"],
        filter_expr='eq(fvBD.badExprKey,"x")',
    )

    note = envelope["note"]
    for needle in ("badFilterKey", "badOrderKey", "fvBadChild", "badExprKey"):
        assert needle in note, needle
        assert any(needle in w for w in _warned(tool_ctx)), needle
    call = _backend(tool_ctx).calls[0]
    assert call["include_children"] == ["fvBadChild"]
    assert call["order_by"] == "fvBD.badOrderKey|asc"
    assert "badExprKey" in call["filter_expr"]


@pytest.mark.asyncio
async def test_relaxed_mode_reports_an_unknown_class_in_filter_expr(tool_ctx, relaxed):
    from niwashi_mcp.main import count

    result = await count("fvBD", tool_ctx, filter_expr='eq(fvBDD.name,"x")')

    assert len(result["warnings"]) == 1
    assert "fvBDD" in result["warnings"][0]
    assert len(_backend(tool_ctx).calls) == 1


@pytest.mark.asyncio
async def test_relaxed_mode_reports_a_name_once_however_often_it_appears(tool_ctx, relaxed):
    from niwashi_mcp.main import count

    result = await count(
        "fvBD",
        tool_ctx,
        filters={"nope": "x"},
        filter_expr='or(eq(fvBD.nope,"x"),ne(fvBD.nope,"y"))',
    )
    assert len(result["warnings"]) == 1


@pytest.mark.asyncio
async def test_relaxed_note_is_appended_to_the_truncation_note_not_instead_of_it(
    tool_ctx, relaxed
):
    """The truncation note is what stops a partial page being read as a total.
    A warning must never overwrite it."""
    from niwashi_mcp.main import query

    envelope = await query("fvBD", tool_ctx, limit=1, order_by="fvBD.nope|asc")

    assert envelope["truncated"] is True
    assert envelope["note"].startswith("Only 1 of 3 results returned")
    assert "fetch_all=True" in envelope["note"]
    assert "nope" in envelope["note"]


@pytest.mark.asyncio
async def test_relaxed_note_is_appended_to_the_safety_cap_note(sample_imdata, catalogue_index, relaxed):
    from niwashi_mcp.main import query

    ctx = make_ctx(
        {
            "descriptions": catalogue_index,
            "backend": StubBackend(sample_imdata, cap_at=1),
        }
    )
    envelope = await query("fvBD", ctx, fetch_all=True, order_by="fvBD.nope|asc")

    assert envelope["complete"] is False
    assert envelope["note"].startswith("Fetched 1 of 3 objects before hitting")
    assert "nope" in envelope["note"]


@pytest.mark.asyncio
async def test_relaxed_mode_still_refuses_an_unknown_class_name(tool_ctx, relaxed):
    """The switch governs attributes (and the secondary classes around them),
    not the class a request is *about* — that guard is older and a typo there
    has no legitimate reading."""
    from niwashi_mcp.main import count, query

    with pytest.raises(UnknownClassError):
        await query("fvBDD", tool_ctx)
    with pytest.raises(UnknownClassError):
        await count("fvBDD", tool_ctx)
    assert _backend(tool_ctx).calls == []
