# Copyright (C) 2026 Khalid El-Ouiali — MONARK AIOPS srl
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Unit tests for the attribute extractors in registry.filter.

`filter_expr_attributes` and `order_by_attributes` find the `Class.attr` pairs
in strings the caller composed, so that each can be checked against the
catalogue before the request is sent. The property that matters most is the
asymmetry in what a mistake costs: a pair wrongly *extracted* refuses a request
the APIC would have answered; a pair *missed* only leaves one reference
unchecked, which is where it stood before the check existed. These tests lean
on the first — they pin what must NOT be extracted as hard as what must.
"""

import pytest

from niwashi_mcp.registry.filter import (
    filter_expr_attributes,
    is_identifier,
    order_by_attributes,
)

# ── filter_expr_attributes: what is extracted ────────────────────────────────


def test_single_predicate():
    assert filter_expr_attributes('eq(fvBD.name,"x")') == [("fvBD", "name")]


def test_every_operator_shape_the_apic_documents():
    for expr in (
        'ne(fvBD.name,"x")',
        'lt(faultInst.severity,"minor")',
        'le(faultInst.severity,"minor")',
        'gt(faultInst.severity,"minor")',
        'ge(faultInst.severity,"minor")',
        'wcard(fvBD.name,"x")',
        'anybit(fvBD.name,"x")',
        'allbit(fvBD.name,"x")',
        'bw(fvBD.name,"a","b")',
    ):
        assert len(filter_expr_attributes(expr)) == 1, expr


def test_an_operator_that_is_not_enumerated_is_still_checked():
    """Any lower-case operator name qualifies, so one the APIC adds later is
    covered without a code change."""
    assert filter_expr_attributes('futureop(fvBD.name,"x")') == [("fvBD", "name")]


def test_compositions_are_walked_to_every_leaf():
    expr = 'and(eq(fvBD.name,"x"),or(wcard(fvBD.descr,"y"),not(ne(fvBD.dn,"z"))))'
    assert filter_expr_attributes(expr) == [
        ("fvBD", "name"),
        ("fvBD", "descr"),
        ("fvBD", "dn"),
    ]


def test_classes_may_differ_within_one_expression():
    expr = 'and(eq(fvBD.name,"x"),eq(fvTenant.name,"y"))'
    assert filter_expr_attributes(expr) == [("fvBD", "name"), ("fvTenant", "name")]


def test_whitespace_around_the_operand_is_tolerated():
    assert filter_expr_attributes('eq( fvBD.name , "x")') == [("fvBD", "name")]


def test_digits_in_class_and_attribute_names():
    assert filter_expr_attributes('eq(l3extOut.tnL3extDomName,"x")') == [
        ("l3extOut", "tnL3extDomName")
    ]


def test_a_repeated_pair_is_returned_once_in_first_seen_order():
    expr = 'or(eq(fvBD.b,"1"),eq(fvBD.a,"2"),eq(fvBD.b,"3"))'
    assert filter_expr_attributes(expr) == [("fvBD", "b"), ("fvBD", "a")]


# ── filter_expr_attributes: what must NOT be extracted ───────────────────────


def test_a_dot_inside_a_quoted_value_is_not_a_reference():
    """`wcard(fvBD.dn,"uni/tn-a.b")` contains `a.b`; read as Class.attr it
    would be refused as an unknown class `a`."""
    assert filter_expr_attributes('wcard(fvBD.dn,"uni/tn-a.b")') == [("fvBD", "dn")]


def test_a_value_shaped_like_a_predicate_is_not_a_reference():
    """The sharpest form of the same trap: the value *looks like* a predicate."""
    assert filter_expr_attributes('eq(fvBD.descr,"eq(ghost.attr,1)")') == [
        ("fvBD", "descr")
    ]


def test_an_escaped_quote_does_not_end_the_literal_early():
    expr = r'eq(fvBD.descr,"say \"x.y\" then eq(ghost.attr,1)")'
    assert filter_expr_attributes(expr) == [("fvBD", "descr")]


def test_an_escaped_backslash_before_the_closing_quote_ends_the_literal():
    r"""`"a\\"` is the two-character string `a\`; the quote after it closes."""
    expr = r'and(eq(fvBD.descr,"a\\"),eq(fvBD.name,"b"))'
    assert filter_expr_attributes(expr) == [("fvBD", "descr"), ("fvBD", "name")]


def test_an_unquoted_second_argument_is_not_a_reference():
    """Malformed, but the APIC's problem to report — not ours to misread as a
    class `uni` carrying an attribute `tn`."""
    assert filter_expr_attributes("eq(fvBD.dn,uni.tn)") == [("fvBD", "dn")]


def test_an_unterminated_quote_drops_everything_after_it():
    """Past the break nothing can be trusted to be outside a literal."""
    assert filter_expr_attributes('eq(fvBD.name,"x ghost(a.b,1)') == [("fvBD", "name")]


def test_an_unterminated_quote_after_an_escaped_quote_still_drops_the_rest():
    """The opener is followed by an *escaped* quote, so the string holds an even
    number of quote characters even though one literal never closes. Counting
    quotes would call this balanced and read `ghost.attr` as code."""
    expr = r'eq(fvBD.descr,"abc\") or(ghost.attr,1)'
    assert filter_expr_attributes(expr) == [("fvBD", "descr")]


def test_a_trailing_backslash_inside_an_open_literal_is_not_a_crash_or_a_leak():
    assert filter_expr_attributes('eq(fvBD.name,"x\\') == [("fvBD", "name")]


def test_an_unquoted_later_argument_is_not_a_reference():
    """Only the first operand is an attribute reference. A later argument that
    is not quoted is malformed, and must not be read as `uni`.`tn`."""
    assert filter_expr_attributes('bw(fvBD.name,"a",uni.tn)') == [("fvBD", "name")]
    assert filter_expr_attributes("eq(fvBD.dn, uni.tn)") == [("fvBD", "dn")]


@pytest.mark.parametrize(
    "expr",
    [
        "",
        None,
        "not(a filter)",
        "eq(name,\"x\")",  # no class qualifier
        'eq(fvBD,"x")',  # no attribute
        'eq(a.b.c,"x")',  # three-part name
        "fvBD.name",  # not inside a predicate
        'eq(1.5,"x")',
        "((",
    ],
)
def test_anything_unrecognised_passes_through_untouched(expr):
    assert filter_expr_attributes(expr) == []


# ── order_by_attributes ──────────────────────────────────────────────────────


def test_order_by_with_direction():
    assert order_by_attributes("faultInst.severity|desc") == [("faultInst", "severity")]
    assert order_by_attributes("faultInst.severity|asc") == [("faultInst", "severity")]


def test_order_by_without_direction():
    assert order_by_attributes("fvBD.name") == [("fvBD", "name")]


def test_order_by_comma_separated_list():
    assert order_by_attributes("faultInst.severity|desc, faultInst.code|asc") == [
        ("faultInst", "severity"),
        ("faultInst", "code"),
    ]


def test_order_by_term_without_a_class_is_left_alone():
    assert order_by_attributes("severity|desc") == []


def test_order_by_skips_only_the_unrecognised_term():
    assert order_by_attributes("severity,fvBD.name|asc") == [("fvBD", "name")]


@pytest.mark.parametrize("value", ["", None, "|desc", "a.b.c|asc", "fvBD.|asc"])
def test_order_by_unrecognised_shapes_pass_through_untouched(value):
    assert order_by_attributes(value) == []


# ── is_identifier ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["name", "fvBD", "l3extOut", "tnFvCtxName", "a1"])
def test_is_identifier_accepts_what_build_filter_accepts(value):
    assert is_identifier(value)


@pytest.mark.parametrize("value", ["", "bad-attr", "123attr", "a.b", "a b", "a_b"])
def test_is_identifier_rejects_what_build_filter_rejects(value):
    assert not is_identifier(value)
