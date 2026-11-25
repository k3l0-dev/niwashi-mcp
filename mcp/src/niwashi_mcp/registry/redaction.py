# Copyright (C) 2026 Khalid El-Ouiali — MONARK AIOPS srl
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Keep APIC secrets out of what an LLM agent is handed.

An APIC returns some secrets in clear: an L4-L7 device credential
(``vnsCCred.value``), an SNMP trap community (``snmpTrapDest.secName``), the
community string that *is* an ``snmpCommunityP``'s name, the token hash that
names a login session. This server is read-only, but a read-only tool that
returns them is still a disclosure to whoever the agent is talking to. This
module is the one place that decides what is secret and strips it.

What is secret
--------------
The policy is niwaki's, not a copy of it. ``niwaki.snapshot.take()`` redacts
with two layers and this reuses both, so the SDK and this server cannot
disagree about what a secret is:

* the schema's ``secure`` flag (:func:`catalog.secure_properties`) — what the
  APIC itself omits from reads, 169 properties on 112 classes; and
* ``niwaki._secrets`` — what the flag misses and the APIC echoes in clear:
  ``SECRET_VALUE_POSITIONS`` (25 pairs) and ``SECRET_DN_POSITIONS``, the
  classes whose *name* is the secret, so it is in their DN and in the DN of
  everything beneath them.

That module is private to niwaki, which is pinned to one minor for exactly that
reason, so :func:`verify_policy` runs at startup and refuses to start when the
names or their shape have moved (see :class:`RedactionPolicyError`).

Three things here go beyond what niwaki does, because niwaki redacts a
configuration snapshot and this answers arbitrary queries:

* A curated position on an abstract class applies to its concrete subclasses.
  niwaki lists ``snmpACommunityP.name`` but the APIC only ever returns the
  concrete ``snmpCommunityP`` and ``snmpCommSecP``, so read literally the entry
  covers neither. The second also names its DN by the community string, and
  niwaki's DN list does not have it.
* The DN is redacted, not merely warned about. niwaki's snapshot says "this DN
  carries a secret and cannot be redacted in place" and publishes a warning;
  here the secret segment is replaced wherever a DN appears — the object's own
  ``dn``, a descendant's, a ``tDn``, an audit record's ``affected`` — and the
  secret is then also removed from the object's other text, because an audit
  record of "community created" repeats the community in ``descr`` and
  ``changeSet`` (measured on the lab fabric).
* A request cannot be used to check a guess: see :func:`check_request`.

What is not done
----------------
An empty secret stays empty. It is configuration posture, not a secret — the
same call niwaki makes for ``passwdSet`` — and it keeps "this credential is
unset" answerable. Nothing here knows a secret the catalogue does not: a class
newer than the catalogue is covered by the curated positions by name only.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from types import MappingProxyType
from typing import Any

import niwaki.catalog as niwaki_catalog
from niwaki.snapshot import REDACTED

from niwashi_mcp.exceptions import (
    RedactionPolicyError,
    SecretAttributeError,
    SecretDnError,
)
from niwashi_mcp.registry import catalog
from niwashi_mcp.registry.filter import dotted_references

_PIN = "Pin niwaki>=2.2,<2.3."

# An RN template whose only variable part is one trailing naming property:
# `community-{name}`. That shape is what lets the secret be found in a DN by its
# literal prefix; any other (`foo-[{a}]-{b}`) is refused rather than guessed at.
_SECRET_RN = re.compile(r"(?P<prefix>[^{}\[\]/]+)\{(?P<prop>[A-Za-z0-9]+)\}")

# DN-carrying properties of a class named by a secret. Their *value* is shown
# with the secret cut out (see `redact_object`), so they are not whole-value
# secrets, but a predicate on them would still answer questions about it.
_DN_PROPERTIES = frozenset({"dn", "rn"})

_NO_ATTRIBUTES: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class _Policy:
    """What is secret, resolved against the shipped catalogue once.

    Attributes:
        values: ``{class: attributes whose value is withheld}`` — the schema's
            ``secure`` flag, niwaki's curated value positions and the naming
            property of each secret-DN class, each applied to the class named
            and to its concrete subclasses.
        dn_classes: Classes named by a secret.
        prefixes: The literal RN prefix of each, e.g. ``community-``.
        in_response: Finds a secret RN segment in a DN *we emit*: anchored to a
            segment start, so ``snmpp-default/community-public`` and
            ``subj-[uni/fabric/snmpp-x/community-public]/mod-1`` match and a
            word that merely contains the prefix does not.
        in_request: Finds one in text *an agent sent*: looser on the left
            (``"community-public"`` in a quoted filter value), case-blind, and
            it needs a value, so asking for the bare prefix is not a guess.
    """

    values: Mapping[str, frozenset[str]]
    dn_classes: frozenset[str]
    prefixes: tuple[str, ...]
    in_response: re.Pattern[str] | None
    in_request: re.Pattern[str] | None


# ── The policy: niwaki's tables, validated and resolved ───────────────────────


def _positions(name: str, value: object) -> frozenset[tuple[str, str]]:
    """``value`` as a set of ``(class, property)`` pairs, or the reason it is not."""
    ok = isinstance(value, frozenset) and all(
        isinstance(pair, tuple)
        and len(pair) == 2
        and all(isinstance(part, str) and part for part in pair)
        for pair in value
    )
    if not ok or not value:
        raise RedactionPolicyError(
            f"niwaki._secrets.{name} is no longer a non-empty set of (class, "
            f"property) pairs. This server reads niwaki's redaction policy "
            f"rather than keeping a copy that would go stale. {_PIN}"
        )
    return value  # type: ignore[return-value]


def _concrete_subclasses(class_name: str) -> list[str]:
    """Concrete subclasses of ``class_name``; empty for a class the catalogue lacks."""
    try:
        return niwaki_catalog.concrete_subclasses(class_name)
    except KeyError:  # niwaki's UnknownClassError, which is also a KeyError
        return []


def _load_niwaki_tables() -> tuple[
    frozenset[tuple[str, str]], frozenset[tuple[str, str]], frozenset[str]
]:
    """niwaki's curated tables, or a :class:`RedactionPolicyError` naming what moved."""
    try:
        from niwaki import _secrets

        value_positions = _positions("SECRET_VALUE_POSITIONS", _secrets.SECRET_VALUE_POSITIONS)
        dn_positions = _positions("SECRET_DN_POSITIONS", _secrets.SECRET_DN_POSITIONS)
        dn_classes = _secrets.secret_dn_classes()
    except (ImportError, AttributeError) as exc:
        raise RedactionPolicyError(
            f"niwaki's secret-redaction policy (niwaki._secrets) is missing a name "
            f"this server reads: {exc}. It is private to niwaki and a release may "
            f"move it. {_PIN}"
        ) from exc
    if not isinstance(dn_classes, frozenset) or not all(
        isinstance(name, str) for name in dn_classes
    ):
        raise RedactionPolicyError(
            f"niwaki._secrets.secret_dn_classes() no longer returns a set of class "
            f"names. {_PIN}"
        )
    return value_positions, dn_positions, dn_classes


def _build_policy() -> _Policy:
    value_positions, dn_positions, dn_names = _load_niwaki_tables()

    values: dict[str, set[str]] = {
        cls: set(props) for cls, props in catalog.secure_properties().items()
    }
    dn_classes = set(dn_names)

    # A position on an abstract class is a position on every concrete class that
    # inherits the property: the APIC returns the concrete one, never the parent.
    for cls, prop in value_positions | dn_positions:
        for target in (cls, *_concrete_subclasses(cls)):
            if target == cls or catalog.property_exists(target, prop):
                values.setdefault(target, set()).add(prop)
            if prop in catalog.load_schema(target).get("identifiedBy", ()):
                dn_classes.add(target)
    # ...and a secure-flagged property that names its class puts the secret in
    # the DN just the same. None exists today; this is the drift net.
    for cls, props in catalog.secure_properties().items():
        if props & set(catalog.load_schema(cls).get("identifiedBy", ())):
            dn_classes.add(cls)

    prefixes: set[str] = set()
    for cls in sorted(dn_classes):
        schema = catalog.load_schema(cls)
        rn = schema.get("rnFormat", "")
        match = _SECRET_RN.fullmatch(rn)
        if match is None or match["prop"] not in schema.get("identifiedBy", ()):
            raise RedactionPolicyError(
                f"{cls} is named by a secret, but its RN format {rn!r} is not a "
                "literal prefix followed by its one naming property, so the secret "
                "cannot be located in a DN to redact it. This needs a code change, "
                "not a pin."
            )
        prefixes.add(match["prefix"])

    ordered = tuple(sorted(prefixes, key=lambda p: (-len(p), p)))
    alternatives = "|".join(re.escape(p) for p in ordered)
    return _Policy(
        values=MappingProxyType({cls: frozenset(props) for cls, props in values.items()}),
        dn_classes=frozenset(dn_classes),
        prefixes=ordered,
        in_response=(
            re.compile(rf"(?:^|(?<=[/\[]))({alternatives})([^/\]\s]+)") if ordered else None
        ),
        in_request=(
            re.compile(rf"(?<![A-Za-z0-9])({alternatives})[^/\]\s\"',()]+", re.IGNORECASE)
            if ordered
            else None
        ),
    )


@lru_cache(maxsize=1)
def _policy() -> _Policy:
    return _build_policy()


def verify_policy() -> None:
    """Fail at startup if niwaki's redaction policy has moved, or does not work.

    Structural first — the three names read from ``niwaki._secrets`` and their
    shape — then functional, the way :func:`catalog.verify_catalogue` does: the
    policy is applied to objects for which the answer is known, because a table
    that still exists but no longer says what it did would redact nothing and
    pass every structural check.

    Raises:
        RedactionPolicyError: naming what moved. The server must not start: with
            no policy it cannot say which values are secret, and would hand them
            to an agent.
    """
    import niwaki

    version = getattr(niwaki, "__version__", "unknown")
    _policy.cache_clear()
    secret_attributes.cache_clear()
    try:
        policy = _policy()
    except RedactionPolicyError as exc:
        raise RedactionPolicyError(f"niwaki {version}: {exc}") from exc

    if not isinstance(REDACTED, str) or not REDACTED:
        raise RedactionPolicyError(
            f"niwaki {version}: niwaki.snapshot.REDACTED is no longer a non-empty string. "
            f"{_PIN}"
        )

    # The measured live leak, a flagged secret, a secret that names its object,
    # and an ordinary class that must stay untouched.
    expectations = {
        "a curated value (vnsCCred.value, echoed in clear)": "value"
        in policy.values.get("vnsCCred", _NO_ATTRIBUTES),
        "a schema-flagged secret (aaaUser.pwd)": "pwd"
        in policy.values.get("aaaUser", _NO_ATTRIBUTES),
        "the schema flag agreeing with niwaki's own reader": bool(
            niwaki_catalog.prop_flags("aaaUser")["pwd"].secure
        ),
        "a class named by its secret (snmpCommunityP)": "snmpCommunityP" in policy.dn_classes
        and "community-" in policy.prefixes,
        "an ordinary class left alone (fvBD)": "fvBD" not in policy.values
        and "fvBD" not in policy.dn_classes,
    }
    if broken := [what for what, ok in expectations.items() if not ok]:
        raise RedactionPolicyError(
            f"niwaki {version}'s redaction policy no longer gives the expected answer for: "
            f"{'; '.join(broken)}. The tables or the flag layout moved. {_PIN}"
        )

    sample, withheld = redact_object(
        {
            "_class": "snmpCommunityP",
            "dn": "uni/fabric/snmpp-x/community-zzsecret",
            "name": "zzsecret",
            "descr": "zzsecret",
        }
    )
    if "zzsecret" in repr(sample) or withheld != 3:
        raise RedactionPolicyError(
            f"niwaki {version}'s redaction policy resolves, but redacting a known "
            f"secret-named object did not remove the secret. {_PIN}"
        )


# ── Which attributes a request may not use ────────────────────────────────────


@lru_cache(maxsize=512)
def secret_attributes(class_name: str) -> frozenset[str]:
    """Attributes of ``class_name`` that a filter, an order or a grouping may not use.

    An abstract class answers for its concrete subclasses, as the catalogue's
    ``known_properties`` does: a query on the parent returns their instances, so
    a predicate on the parent is a predicate on theirs. A class named by a secret
    also loses ``dn`` and ``rn``, which carry it.

    Returns an empty set for a class the catalogue does not know — nothing is
    known about it, and refusing every attribute would be a different error.
    """
    policy = _policy()
    attributes = set(policy.values.get(class_name, _NO_ATTRIBUTES))
    subclasses = set(_concrete_subclasses(class_name))
    for cls in subclasses & policy.values.keys():
        attributes |= policy.values[cls]
    if class_name in policy.dn_classes or subclasses & policy.dn_classes:
        attributes |= _DN_PROPERTIES
    return frozenset(attributes)


def mark_secret_properties(schema: dict[str, Any], class_name: str) -> dict[str, Any]:
    """Add ``"secret": true`` to each secret property in ``schema``'s ``property_details``.

    The names stay listed — an agent that sees a property exist is less likely to
    go looking for it another way — and are labelled so it knows not to ask. No
    value is involved. Done here, on top of :func:`catalog.load_schema`, for the
    reason ``annotate_relations`` is: that function is the projection the
    recorded baseline pins. Mutates and returns ``schema``, which is freshly
    built per call.
    """
    details = schema.get("property_details")
    if isinstance(details, dict):
        secret = secret_attributes(class_name)
        for name, detail in details.items():
            if name in secret and isinstance(detail, dict):
                detail["secret"] = True
    return schema


# ── Refusing the requests that would reveal one ───────────────────────────────


def check_dn(text: str, where: str = "dn") -> None:
    """Refuse text that names an object by a secret, on its shape alone.

    ``get_by_dn(".../community-public")`` answering found or not found checks a
    guess. So is ``scope_dn`` rooted there, and so is a filter value spelling it.
    The decision is made on the text, before the APIC is asked, so existing and
    non-existing DNs get the same answer.

    Raises:
        SecretDnError: ``text`` carries a secret RN segment.
    """
    pattern = _policy().in_request
    if pattern is not None and pattern.search(text):
        raise SecretDnError(where)


def check_request(
    class_name: str,
    *,
    filters: Mapping[str, str] | None = None,
    order_by: str | None = None,
    filter_expr: str | None = None,
    scope_dn: str | None = None,
    group_by: str | None = None,
) -> None:
    """Refuse a request whose answer would reveal a secret.

    A secret that is never returned can still be recovered by asking about it:
    ``filters={"value": "guess"}`` is an equality oracle (one hit confirms the
    guess), ``order_by`` leaks the order of values, ``group_by`` prints them as
    keys. None of those is a catalogue question, so unlike an unknown attribute
    this holds with ``NIWASHI_MCP_STRICT_ATTRIBUTES`` off.

    Attribute references in ``filter_expr`` are found by shape
    (:func:`dotted_references`), not by the conservative extractor the typo check
    uses, so a predicate cannot slip past by how it is written.

    Not covered, and said so: a ``wcard`` over a free-text or DN attribute of
    *another* class that happens to quote a secret (an audit record's
    ``affected``) can still be probed by a substring that does not spell the RN
    prefix. What such a record shows is redacted; what it matches is not.

    Raises:
        SecretAttributeError: a predicate, ordering or grouping uses a secret attribute.
        SecretDnError: ``scope_dn``, a filter value or ``filter_expr`` spells a secret DN.
    """
    for where, text in (
        ("scope_dn", scope_dn),
        ("filter_expr", filter_expr),
        *(("filters", value) for value in (filters or {}).values()),
    ):
        if text:
            check_dn(text, where)

    references: list[tuple[str, str, str]] = [
        ("filters", class_name, attribute) for attribute in filters or {}
    ]
    for term in (order_by or "").split(","):
        reference = term.split("|", 1)[0].strip()
        if reference:
            owner, dot, attribute = reference.rpartition(".")
            references.append(("order_by", owner if dot else class_name, attribute))
    references += [
        ("filter_expr", owner, attribute) for owner, attribute in dotted_references(filter_expr)
    ]
    if group_by:
        references.append(("group_by", class_name, group_by))

    for where, owner, attribute in references:
        if attribute in secret_attributes(owner):
            raise SecretAttributeError(owner, attribute, where)


# ── Stripping them from what comes back ───────────────────────────────────────


def _redact_segments(pattern: re.Pattern[str], text: str, found: set[str]) -> tuple[str, int]:
    """``text`` with each secret RN segment's value replaced, and how many were.

    Every secret value met is added to ``found``. A segment already holding the
    marker is left alone and not counted, so redacting twice changes nothing.
    """
    replaced = 0

    def blank(match: re.Match[str]) -> str:
        nonlocal replaced
        secret = match[2]
        if secret == REDACTED:
            return match[0]
        found.add(secret)
        replaced += 1
        return match[1] + REDACTED

    return pattern.sub(blank, text), replaced


def redact_object(obj: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """``obj`` with every secret withheld, and how many things were.

    Three rules, in this order:

    1. A secret *attribute* of the object's class (schema-flagged, curated, or
       the naming property of a class named by its secret) becomes
       ``niwaki.snapshot.REDACTED``, whatever ``fields`` was asked for. An empty
       value is left empty and not counted.
    2. In every other string attribute — ``dn``, ``rn``, a ``tDn``, an audit
       record's ``affected`` — the value after a secret RN prefix is replaced:
       ``.../community-public`` becomes ``.../community-<redacted>``.
    3. A secret value found that way is then removed, as a whole word, from the
       remaining string attributes of the same object: an audit record of the
       community being created repeats it in ``descr`` and ``changeSet``.

    ``_children`` are redacted the same way, each as an object of its own class.

    The input is not modified. An object with nothing to withhold is returned
    as the same object, so the common case allocates nothing and is byte for
    byte what the backend sent.

    Returns:
        ``(object, count)``; ``count`` is the number of values, DN segments and
        text attributes replaced, and is 0 exactly when nothing changed.
    """
    policy = _policy()
    edits: dict[str, Any] = {}
    count = 0

    secrets = policy.values.get(obj.get("_class", ""), _NO_ATTRIBUTES)
    for attribute in secrets:
        value = obj.get(attribute)
        if value and value != REDACTED:
            edits[attribute] = REDACTED
            count += 1

    pattern = policy.in_response
    if pattern is not None and _mentions_a_prefix(policy.prefixes, obj):
        found: set[str] = set()
        for key, value in obj.items():
            if isinstance(value, str) and key not in secrets:
                text, replaced = _redact_segments(pattern, value, found)
                if replaced:
                    edits[key] = text
                    count += replaced
        if found:
            words = [
                re.compile(rf"(?<![A-Za-z0-9]){re.escape(secret)}(?![A-Za-z0-9])")
                for secret in sorted(found, key=len, reverse=True)
            ]
            for key, value in obj.items():
                if isinstance(value, str) and key not in secrets and key not in edits and key != "_class":
                    text = value
                    for word in words:
                        text = word.sub(REDACTED, text)
                    if text != value:
                        edits[key] = text
                        count += 1

    children = obj.get("_children")
    if isinstance(children, list):
        redacted_children = [
            redact_object(child) if isinstance(child, dict) else (child, 0) for child in children
        ]
        if any(new is not old for old, (new, _) in zip(children, redacted_children, strict=True)):
            edits["_children"] = [new for new, _ in redacted_children]
            count += sum(n for _, n in redacted_children)

    return ({**obj, **edits} if edits else obj), count


def _mentions_a_prefix(prefixes: tuple[str, ...], obj: dict[str, Any]) -> bool:
    """A cheap, conservative test for "this object may carry a secret RN segment".

    One join and a substring test per prefix, in C, instead of a regular
    expression per attribute: on a 5,000-object result most objects carry none.
    A false yes costs one regex pass; a false no is impossible, since a segment
    contains its prefix verbatim.
    """
    joined = "\x00".join([v for v in obj.values() if isinstance(v, str)])
    return any(prefix in joined for prefix in prefixes)


def redact_objects(objects: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """:func:`redact_object` over a result set; the count is the total withheld."""
    redacted = [redact_object(obj) for obj in objects]
    return [obj for obj, _ in redacted], sum(n for _, n in redacted)
