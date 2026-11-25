# Copyright (C) 2026 Khalid El-Ouiali — MONARK AIOPS srl
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
registry/filter.py

Build APIC query-target-filter strings from a plain Python dict.

APIC filter syntax reference:
  Single predicate  : eq(fvBD.name,"servers")
  Multiple (AND)    : and(eq(fvBD.name,"servers"),eq(fvBD.arpFlood,"yes"))
  Wildcard on dn    : wcard(fvBD.dn,"uni/tn-OT")
  Greater-than      : gt(faultInst.severity,"minor")

This module only generates eq() predicates from caller-supplied key/value pairs.
More complex predicates (gt, lt, wcard, ne) can be added as needed.
"""

import re

from niwashi_mcp.exceptions import FilterError

# Valid ACI identifiers: letters/digits only, must start with a letter.
_IDENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")


def _validate_ident(value: str, label: str) -> None:
    if not _IDENT_RE.match(value):
        raise FilterError(
            f"Invalid {label} '{value}': must start with a letter and contain only "
            "letters and digits."
        )


def is_identifier(value: str) -> bool:
    """Whether `build_filter` would accept `value` as a class or attribute name.

    The same test as `_validate_ident`, exposed so a caller can tell a key that
    `build_filter` will reject with `FilterError` from one it will send — and
    leave the first kind to raise that, rather than report it as an unknown
    attribute.
    """
    return bool(_IDENT_RE.match(value))


def _escape_value(value: str) -> str:
    """Escape double-quotes inside an APIC filter string value."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_filter(class_name: str, filters: dict[str, str]) -> str:
    """Build an APIC query-target-filter string from a dict of attribute filters.

    Each key/value pair becomes an eq() predicate.  When multiple predicates
    are present they are wrapped in and().  An empty dict returns an empty
    string, which tells the APIC client to omit the filter parameter entirely.

    Args:
        class_name: ACI class name used to qualify attribute names in the filter
                    expression, e.g. "fvBD" → eq(fvBD.name,"servers").
        filters:    Dict of {attribute_name: value} pairs.
                    Use the property names listed by get_schema() as keys.
                    Example: {"name": "servers", "arpFlood": "yes"}

    Returns:
        APIC filter string ready to pass as the query-target-filter parameter,
        or an empty string when filters is empty.

    Raises:
        FilterError: class_name or an attribute key is not a valid ACI identifier.

    Examples:
        >>> build_filter("fvBD", {})
        ''
        >>> build_filter("fvBD", {"name": "servers"})
        'eq(fvBD.name,"servers")'
        >>> build_filter("fvBD", {"name": "servers", "arpFlood": "yes"})
        'and(eq(fvBD.name,"servers"),eq(fvBD.arpFlood,"yes"))'
    """
    if not filters:
        return ""

    _validate_ident(class_name, "class_name")
    for attr in filters:
        _validate_ident(attr, "attribute")

    parts = [
        f'eq({class_name}.{attr},"{_escape_value(val)}")'
        for attr, val in filters.items()
    ]

    if len(parts) == 1:
        return parts[0]

    return "and(" + ",".join(parts) + ")"


# ── Attribute references in free-form strings ────────────────────────────────
#
# `build_filter` only ever sees dict keys, so every key is a bare attribute name
# it can hand to the catalogue as-is. `filter_expr` and `order_by` are strings
# the caller composed, and the attributes in them have to be found first.
#
# The extractors are deliberately conservative: a reference they cannot place
# with certainty is left alone, because a false positive refuses a request the
# APIC would have answered, while a miss merely leaves that one reference
# unchecked — which is exactly where it stood before the check existed.

# A double-quoted APIC string literal, honouring backslash escapes — the same
# escaping `_escape_value` emits, so a value built by this module always lexes
# as one literal. The tail also accepts the end of the string, so a literal that
# never closes is consumed to the end instead of being skipped: the only text
# left over for the extractor is text that provably sits outside any quote.
_STRING_LITERAL_RE = re.compile(r'"(?:[^"\\]|\\.)*(?:(?P<close>")|\\?\Z)')

# `Class.attr` in the *first* argument of a predicate: eq(fvBD.name,"x"),
# wcard(fvBD.dn,"y"), bw(faultInst.lastTransition,"a","b") — and, since the
# operators nest, behind and(...) / or(...) / not(...) too. Anchoring on
# `<operator>(` rather than on any `ident.ident` is what keeps an unquoted
# second argument (`eq(fvBD.dn,uni.tn)`, malformed but harmless) from being
# read as a class and an attribute. The operator is not enumerated: any
# lower-case name qualifies, so one the APIC adds later is still checked.
_PREDICATE_ATTR_RE = re.compile(
    r"\b[a-z]+\(\s*([A-Za-z][A-Za-z0-9]*)\.([A-Za-z][A-Za-z0-9]*)\s*[,)]"
)

# One `Class.attr` or `Class.attr|asc` term of an order-by list.
_ORDER_TERM_RE = re.compile(
    r"^\s*([A-Za-z][A-Za-z0-9]*)\.([A-Za-z][A-Za-z0-9]*)\s*(?:\|.*)?$"
)


def _mask_string_literals(expr: str) -> str:
    """Blank out quoted values so a dot inside one is never read as `Class.attr`.

    `wcard(fvBD.dn,"uni/tn-a.b.c")` holds `a.b` inside the literal; matched as a
    reference it would be reported as an unknown class `a`. A literal that never
    closes cannot be lexed, so it and everything after it is dropped — the APIC
    will reject the expression itself, and nothing past the break can be trusted
    to be outside a literal. (Counting quotes would not find it: an escaped
    quote at the end, `"abc\\")`, leaves the count even.)
    """
    return _STRING_LITERAL_RE.sub(lambda m: '""' if m.group("close") else "", expr)


def filter_expr_attributes(filter_expr: str | None) -> list[tuple[str, str]]:
    """The `(class, attribute)` pairs a raw APIC filter expression refers to.

    Order of first appearance, duplicates removed. Only the attribute operand
    of a predicate is returned — never a value — so the caller can validate
    each pair against the catalogue.

    >>> filter_expr_attributes('and(eq(fvBD.name,"x"),wcard(fvBD.descr,"a.b"))')
    [('fvBD', 'name'), ('fvBD', 'descr')]
    >>> filter_expr_attributes('wcard(fvBD.dn,"uni/tn-a.b.c")')
    [('fvBD', 'dn')]
    >>> filter_expr_attributes(None)
    []
    """
    if not filter_expr:
        return []
    pairs = _PREDICATE_ATTR_RE.findall(_mask_string_literals(filter_expr))
    return list(dict.fromkeys(pairs))


def order_by_attributes(order_by: str | None) -> list[tuple[str, str]]:
    """The `(class, attribute)` pairs of an APIC order-by expression.

    The APIC takes a comma-separated list of `Class.attr` terms, each with an
    optional `|asc` / `|desc`. A term in any other shape — no class qualifier,
    say — is skipped rather than guessed at: the APIC's own handling of it is
    not something this module can vouch for.

    >>> order_by_attributes("faultInst.severity|desc,faultInst.code")
    [('faultInst', 'severity'), ('faultInst', 'code')]
    >>> order_by_attributes("severity|desc")
    []
    """
    if not order_by:
        return []
    pairs = [
        m.groups() for term in order_by.split(",") if (m := _ORDER_TERM_RE.match(term))
    ]
    return list(dict.fromkeys(pairs))
