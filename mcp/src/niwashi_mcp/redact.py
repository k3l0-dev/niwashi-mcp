# Copyright (C) 2026 Khalid El-Ouiali — MONARK AIOPS srl
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Keep APIC secrets out of what an LLM agent is handed.

INTERIM — this module stands in for ``niwaki.secrets``, which niwaki 2.3 will
ship as a public module with the same interface. Every decision about what is
secret and how it is scrubbed lives here and nowhere else: the tools and the
request guards call this module and never read ``niwaki._secrets`` themselves,
so the switch is this one file, and its tests are written against the interface
below rather than against niwaki's internals. The names that follow niwaki's
planned signatures are the public ones; the section marked "niwashi" is what
stays when the rest becomes a re-export.

An APIC returns some secrets in clear: an L4-L7 device credential
(``vnsCCred.value``), an SNMP trap community (``snmpTrapDest.secName``), the
community string that *is* an ``snmpCommunityP``'s name, the token hash that names
a login session. It also writes them into its own logs: an audit record of a
credential being created carries the value in ``changeSet``. This server is
read-only, but a read-only tool that returns them is still a disclosure to whoever
the agent is talking to.

What is secret
--------------
The policy is niwaki's, not a copy of it, so the SDK and this server cannot
disagree about what a secret is:

* the schema's ``secure`` flag (:func:`catalog.secure_properties`) — what the APIC
  itself omits from reads, 169 properties on 112 classes; and
* ``niwaki._secrets`` — what the flag misses and the APIC echoes in clear:
  ``SECRET_VALUE_POSITIONS`` (25 pairs) and ``SECRET_DN_POSITIONS``, the classes
  whose *name* is the secret, so it is in their DN and in the DN of everything
  beneath them;
* a short supplement of positions measured live and not yet in niwaki 2.2
  (``_INTERIM_POSITIONS``).

That module is private to niwaki, which is pinned to one minor for exactly that
reason, so :func:`verify_policy` runs at startup and refuses to start when the
names or their shape have moved (see :class:`RedactionPolicyError`).

What it does
------------
* A secret attribute is replaced by :data:`REDACTED` — **empty ones too**, so that
  "set" and "unset" cannot be told apart.
* A DN that passes through a secret (community, session token, API key) has that
  segment cut, wherever a DN appears: ``dn``, ``rn``, ``tDn``, an audit record's
  ``affected``, and inside ``subj-[...]`` brackets. No class is needed: the RN
  prefix is enough.
* Free text is scrubbed: a secret ``prop:value`` or ``prop (Old: x, New: y)`` pair
  in ``changeSet`` has its value replaced, in both formats the APIC writes, and a
  secret met in a DN is removed from the same record's other text. This is best
  effort. A secret in a shape nothing here recognises can remain, and the two
  guards against that fail closed: text that names a secret property but cannot be
  parsed is replaced whole, and so is text that still holds a secret longer than
  six characters once scrubbed.

Three things go beyond what niwaki 2.2 does, because niwaki redacts a
configuration snapshot and this answers arbitrary queries: a curated position on
an abstract class applies to its concrete subclasses (``snmpACommunityP.name``
names a class no response carries; ``snmpCommSecP`` is the one that is named by
its community and niwaki's DN list lacks); the DN is cut, not merely warned
about; and free text is scrubbed at all.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from types import MappingProxyType
from typing import Any, NamedTuple

import niwaki.catalog as niwaki_catalog
from niwaki.snapshot import REDACTED

from niwashi_mcp.exceptions import (
    RedactionPolicyError,
    SecretAttributeError,
    SecretDnError,
)
from niwashi_mcp.registry import catalog
from niwashi_mcp.registry.filter import dotted_references

__all__ = [
    "REDACTED",
    "Redacted",
    "check_dn",
    "check_request",
    "dn_has_secret",
    "free_text_props",
    "has_secret_dn",
    "is_secret",
    "mark_secret_properties",
    "redact",
    "redact_dn",
    "redact_object",
    "redact_one",
    "redact_results",
    "redact_text",
    "refused_props",
    "secret_props",
    "verify_policy",
]

_PIN = "Pin niwaki>=2.2,<2.3."

# Positions the lab APIC was measured to echo in clear, that niwaki 2.2's tables do
# not carry yet (niwaki 2.3 will). The same shape as niwaki's SECRET_VALUE_POSITIONS,
# unioned with it, and dropped from here when 2.3's own tables have them.
_INTERIM_POSITIONS: frozenset[tuple[str, str]] = frozenset(
    {
        # The local user's OTP seed, again: aaaUser.otpkey is curated, its
        # self-service profile sibling is not. Possession forges valid codes.
        ("aaaUserProfile", "otpkey"),
        # The fabric self-signed CA's certificate-request passphrases.
        ("pkiFabricSelfCAEp", "currCertReqPassphrase"),
        ("pkiFabricSelfCAEp", "oldCertReqPassphrase"),
        # A login session's token, in the session log: equals the session's
        # hashToken (measured on 148 of the lab's records).
        ("aaaSessionLR", "sessionId"),
    }
)

# Free text the APIC writes about a change, where a secret gets quoted. Whether a
# class carries it is read from the catalogue (13 concrete and abstract classes
# have `changeSet`: the audit, event, fault and session records).
_FREE_TEXT_PROPS = frozenset({"changeSet"})
# The other attributes of those records that quote what changed.
_TEXT_ATTRIBUTES = ("changeSet", "descr")

# Pseudo-properties the APIC appends to a changeSet that no class declares.
_CHANGESET_EXTRAS = frozenset({"userdom"})

# DN-carrying properties of a class named by a secret. Their value is shown with
# the secret cut out, so they are not whole-value secrets, but a predicate on them
# would still answer questions about it.
_DN_PROPERTIES = frozenset({"dn", "rn"})

# A DN is not text: what the literal-secret pass leaves alone.
_DN_ATTRIBUTES = frozenset({"dn", "rn", "affected", "tDn", "tRn"})

# An RN template whose only variable part is one trailing naming property:
# `community-{name}`. That shape is what lets the secret be found in a DN by its
# literal prefix; any other (`foo-[{a}]-{b}`) is refused rather than guessed at.
_SECRET_RN = re.compile(r"(?P<prefix>[^{}\[\]/]+)\{(?P<prop>[A-Za-z0-9]+)\}")

_NO_ATTRIBUTES: frozenset[str] = frozenset()
_UNKNOWN_MODES = ("heuristic", "keep", "raise")


@dataclass(frozen=True, slots=True)
class _Policy:
    """What is secret, resolved against the shipped catalogue once.

    Attributes:
        values: ``{class: attributes whose value is withheld}`` — the schema's
            ``secure`` flag, niwaki's curated value positions, the interim ones and
            the naming property of each secret-DN class, each applied to the class
            named and to its concrete subclasses.
        dn_classes: Classes named by a secret.
        prefixes: The literal RN prefix of each, e.g. ``community-``.
        secret_names: Every property name that is secret on some class — what a
            changeSet is scrubbed against when its class cannot be told.
        heuristic: Property names that look like secrets, for a class the
            catalogue does not know.
        in_response: Finds a secret RN segment in a DN *we emit*: anchored to a
            segment start, so ``snmpp-default/community-public`` and
            ``subj-[uni/fabric/snmpp-x/community-public]/mod-1`` match and a
            word that merely contains the prefix does not.
        in_request: Finds one in text *an agent sent*: looser on the left
            (``"community-public"`` in a quoted filter value), case-blind, and it
            needs a value, so asking for the bare prefix is not a guess.
    """

    values: Mapping[str, frozenset[str]]
    dn_classes: frozenset[str]
    prefixes: tuple[str, ...]
    secret_names: frozenset[str]
    heuristic: re.Pattern[str]
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
    frozenset[tuple[str, str]], frozenset[tuple[str, str]], frozenset[str], re.Pattern[str]
]:
    """niwaki's curated tables, or a :class:`RedactionPolicyError` naming what moved."""
    try:
        from niwaki import _secrets

        value_positions = _positions("SECRET_VALUE_POSITIONS", _secrets.SECRET_VALUE_POSITIONS)
        dn_positions = _positions("SECRET_DN_POSITIONS", _secrets.SECRET_DN_POSITIONS)
        dn_classes = _secrets.secret_dn_classes()
        prop_pattern = _secrets.PROP_PATTERN
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
    if not isinstance(prop_pattern, re.Pattern):
        raise RedactionPolicyError(
            f"niwaki._secrets.PROP_PATTERN is no longer a compiled pattern. {_PIN}"
        )
    return value_positions, dn_positions, dn_classes, prop_pattern


def _build_policy() -> _Policy:
    value_positions, dn_positions, dn_names, prop_pattern = _load_niwaki_tables()

    values: dict[str, set[str]] = {
        cls: set(props) for cls, props in catalog.secure_properties().items()
    }
    dn_classes = set(dn_names)

    # A position on an abstract class is a position on every concrete class that
    # inherits the property: the APIC returns the concrete one, never the parent.
    for cls, prop in value_positions | dn_positions | _INTERIM_POSITIONS:
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
        secret_names=frozenset(name for props in values.values() for name in props),
        heuristic=re.compile(f"(?:{prop_pattern.pattern})|sessionid", prop_pattern.flags | re.I),
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


@lru_cache(maxsize=4096)
def _known_class(class_name: str) -> bool:
    return catalog.class_exists(class_name)


def _reset() -> None:
    """Forget everything resolved from the policy and the catalogue."""
    for cached in (
        _policy,
        _known_class,
        secret_props,
        free_text_props,
        has_secret_dn,
        refused_props,
        _rn_index,
        _names_for_classes,
        _names_regex,
    ):
        cached.cache_clear()


def verify_policy() -> None:
    """Fail at startup if niwaki's redaction policy has moved, or does not work.

    Structural first — the names read from ``niwaki._secrets`` and their shape —
    then functional, the way :func:`catalog.verify_catalogue` does: the policy is
    applied to objects for which the answer is known, because a table that still
    exists but no longer says what it did would redact nothing and pass every
    structural check.

    Raises:
        RedactionPolicyError: naming what moved. The server must not start: with
            no policy it cannot say which values are secret, and would hand them
            to an agent.
    """
    import niwaki

    version = getattr(niwaki, "__version__", "unknown")
    _reset()
    try:
        policy = _policy()
    except RedactionPolicyError as exc:
        raise RedactionPolicyError(f"niwaki {version}: {exc}") from exc

    if not isinstance(REDACTED, str) or not REDACTED:
        raise RedactionPolicyError(
            f"niwaki {version}: niwaki.snapshot.REDACTED is no longer a non-empty string. "
            f"{_PIN}"
        )

    # The measured live leak, a flagged secret, a secret that names its object, a
    # position added here, and an ordinary class that must stay untouched.
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
        "a session token (aaaSessionLR.sessionId)": "sessionId"
        in policy.values.get("aaaSessionLR", _NO_ATTRIBUTES),
        "an ordinary class left alone (fvBD)": "fvBD" not in policy.values
        and "fvBD" not in policy.dn_classes,
    }
    if broken := [what for what, ok in expectations.items() if not ok]:
        raise RedactionPolicyError(
            f"niwaki {version}'s redaction policy no longer gives the expected answer for: "
            f"{'; '.join(broken)}. The tables or the flag layout moved. {_PIN}"
        )

    sample = redact(
        "snmpCommunityP",
        {"dn": "uni/fabric/snmpp-x/community-zzsecret", "name": "zzsecret", "descr": "zzsecret"},
    )
    audit = redact(
        "aaaModLR",
        {
            "affected": "uni/userext/user-x",
            "changeSet": "name:x, otpkey:zzsecret, userdom:all",
            "descr": "x created",
        },
    )
    if "zzsecret" in repr(sample) or "zzsecret" in repr(audit):
        raise RedactionPolicyError(
            f"niwaki {version}'s redaction policy resolves, but redacting a known "
            f"secret-named object and a known audit record did not remove the secret. {_PIN}"
        )


# ── What is secret ────────────────────────────────────────────────────────────


@lru_cache(maxsize=512)
def secret_props(class_name: str) -> frozenset[str]:
    """Properties of ``class_name`` whose value is withheld.

    Fail-closed on abstraction: an abstract class answers for its concrete
    subclasses, as the catalogue's ``known_properties`` does, because a query on
    the parent returns their instances. Empty for a class the catalogue does not
    know — nothing is known about it; see :func:`is_secret` for what is assumed.
    """
    policy = _policy()
    props = set(policy.values.get(class_name, _NO_ATTRIBUTES))
    for cls in set(_concrete_subclasses(class_name)) & policy.values.keys():
        props |= policy.values[cls]
    return frozenset(props)


def is_secret(class_name: str, prop: str) -> bool:
    """Whether the value of ``prop`` on ``class_name`` objects is a secret.

    True for a schema-flagged property, a curated value position, the naming
    property of a class named by its secret, and a session token. Fail-closed: a
    property of a class the catalogue does not know is a secret when its *name*
    looks like one (``password``, ``community``, ``token``, ...).
    """
    if prop in secret_props(class_name):
        return True
    return not _known_class(class_name) and bool(_policy().heuristic.search(prop))


@lru_cache(maxsize=512)
def free_text_props(class_name: str) -> frozenset[str]:
    """Properties of ``class_name`` that are free text able to quote a secret.

    ``changeSet`` on the audit, event, fault and session records. They are shown
    scrubbed, and — unlike a secret property — still shown; but a predicate on the
    raw text could confirm what was scrubbed, so they cannot be filtered, ordered
    or grouped on either.
    """
    return _FREE_TEXT_PROPS & catalog.known_properties(class_name)


@lru_cache(maxsize=512)
def has_secret_dn(class_name: str) -> bool:
    """Whether ``class_name`` objects are named by a secret, so their DN carries it."""
    dn_classes = _policy().dn_classes
    return class_name in dn_classes or bool(set(_concrete_subclasses(class_name)) & dn_classes)


@lru_cache(maxsize=512)
def refused_props(class_name: str) -> frozenset[str]:
    """Properties of ``class_name`` that a filter, an order or a grouping may not use.

    The secret ones, the free-text ones, and — on a class named by its secret —
    ``dn`` and ``rn``, which carry it.
    """
    refused = secret_props(class_name) | free_text_props(class_name)
    return refused | _DN_PROPERTIES if has_secret_dn(class_name) else refused


# ── DNs ───────────────────────────────────────────────────────────────────────


def dn_has_secret(dn: str) -> bool:
    """Whether ``dn`` still carries a secret RN segment — one not already cut.

    By RN prefix alone, with no class resolution, anywhere a segment can stand,
    ``subj-[uni/.../community-x]/mod-1`` included.
    """
    pattern = _policy().in_response
    return pattern is not None and any(m[2] != REDACTED for m in pattern.finditer(dn))


def _cut_segments(pattern: re.Pattern[str], text: str, found: set[str]) -> tuple[str, int]:
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


def redact_dn(dn: str) -> str:
    """``dn`` with the value of every secret RN segment replaced by :data:`REDACTED`.

    ``uni/fabric/snmpp-default/community-public`` becomes
    ``uni/fabric/snmpp-default/community-<redacted>``, and the same inside
    brackets: ``subj-[.../actsession-TOKEN]/mod-1``. Idempotent.
    """
    pattern = _policy().in_response
    return dn if pattern is None else _cut_segments(pattern, dn, set())[0]


def _mentions_a_prefix(prefixes: tuple[str, ...], attrs: Mapping[str, Any]) -> bool:
    """A cheap, conservative test for "this object may carry a secret RN segment".

    One join and a substring test per prefix, in C, instead of a regular
    expression per attribute: on a 5,000-object result most objects carry none.
    A false yes costs one regex pass; a false no is impossible, since a segment
    contains its prefix verbatim.
    """
    joined = "\x00".join([v for v in attrs.values() if isinstance(v, str)])
    return any(prefix in joined for prefix in prefixes)


# ── Free text ─────────────────────────────────────────────────────────────────

# A property name where a changeSet pair would start: `prop:` or `prop (Old:`.
_PAIR_NAME = re.compile(r"\b([A-Za-z][A-Za-z0-9]*)(?=\s*:|\s*\(\s*(?:Old|New)\s*:)")
# The seam between two pairs.
_PAIR_SEAM = re.compile(r",\s*([A-Za-z][A-Za-z0-9]*)(?=\s*:|\s*\(\s*(?:Old|New)\s*:)")
_OLD_NEW = re.compile(r"\b(Old|New)\s*:\s*")


@lru_cache(maxsize=1)
def _rn_index() -> tuple[Mapping[str, frozenset[str]], Mapping[str, frozenset[str]]]:
    """``(by literal prefix, by whole fixed name)`` for every class's RN format."""
    by_prefix: dict[str, set[str]] = {}
    fixed: dict[str, set[str]] = {}
    for cls, rn_format in catalog.rn_formats().items():
        head, brace, _ = rn_format.partition("{")
        if head:
            (by_prefix if brace else fixed).setdefault(head, set()).add(cls)
    return (
        {head: frozenset(classes) for head, classes in by_prefix.items()},
        {head: frozenset(classes) for head, classes in fixed.items()},
    )


def _last_rn(dn: str) -> str:
    """The last relative name of ``dn``, splitting on `/` only outside brackets."""
    depth = start = 0
    for i, ch in enumerate(dn):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        elif ch == "/" and depth == 0:
            start = i + 1
    return dn[start:]


def _classes_for_dn(dn: str) -> frozenset[str]:
    """The classes an object at ``dn`` could be, from its last RN alone.

    Every class whose RN template that name fits — so a name two classes share is
    both of them, which is the cautious reading — and empty when none does.
    """
    rn = _last_rn(dn)
    by_prefix, fixed = _rn_index()
    classes: set[str] = set(fixed.get(rn, ()))
    for end in range(1, min(len(rn), 48) + 1):
        classes |= by_prefix.get(rn[:end], frozenset())
    return frozenset(classes)


@lru_cache(maxsize=256)
def _names_for_classes(classes: frozenset[str]) -> tuple[frozenset[str], frozenset[str]]:
    """``(secret names, names that may start a pair)`` for the classes an affected DN could be."""
    secret: set[str] = set()
    known: set[str] = set(_CHANGESET_EXTRAS)
    for cls in classes:
        secret |= secret_props(cls)
        known |= catalog.known_properties(cls)
    return frozenset(secret), frozenset(known)


@lru_cache(maxsize=256)
def _names_regex(names: frozenset[str]) -> re.Pattern[str]:
    alternatives = "|".join(re.escape(n) for n in sorted(names, key=lambda n: (-len(n), n)))
    return re.compile(
        rf"\b({alternatives})\b(?=\s*:|\s*\(\s*(?:Old|New)\s*:)"
    )


def _names_for(
    affected_class: str | None, affected: str | None
) -> tuple[frozenset[str], frozenset[str] | None]:
    """Which property names are secret in a text, and which names may start a pair.

    The class comes from ``affected_class``, else from the last RN of ``affected``,
    else the answer is every property name that is secret on any class — which
    over-masks and never raises. A class named but unknown to the catalogue, or an
    ``affected`` no class fits, is the same last case.
    """
    classes: frozenset[str] | None = None
    if affected_class:
        classes = frozenset({affected_class}) if _known_class(affected_class) else None
    elif affected:
        classes = _classes_for_dn(affected) or None
    if classes is None:
        return _policy().secret_names, None
    return _names_for_classes(classes)


def _pair_end(text: str, start: int, boundary: frozenset[str] | None) -> int:
    """Where the value that begins at ``start`` ends: at the next pair, or the text's end.

    A seam counts only when the name after it is one the class declares (or, with
    no class, any name), so a secret that happens to contain `, word:` is not cut
    short and left half-visible.
    """
    for seam in _PAIR_SEAM.finditer(text, start):
        if boundary is None or seam[1] in boundary:
            return seam.start()
    return len(text)


def _group_end(text: str, open_at: int) -> int:
    """Index just past the `)` matching the `(` at ``open_at``, or the text's end."""
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i + 1
    return len(text)


def _scrub_pairs(
    text: str, names: frozenset[str], boundary: frozenset[str] | None, found: set[str]
) -> tuple[str, bool]:
    """Replace the value of every secret pair in a changeSet; say if any could not be parsed.

    Two formats, both tolerated: the creation format ``prop:value, prop:value``
    and the modification format ``prop (Old: x, New: y)``, whose parentheses may
    nest and which the APIC truncates mid-way. A secret name that is not where a
    pair starts is *unparseable*, and the caller replaces the whole text.
    """
    pattern = _names_regex(names)
    out: list[str] = []
    position = 0
    unparseable = False
    while match := pattern.search(text, position):
        before = text[: match.start()].rstrip()
        if before and not before.endswith(","):
            unparseable = True
            out.append(text[position : match.end()])
            position = match.end()
            continue
        after = match.end()
        while after < len(text) and text[after].isspace():
            after += 1
        if text[after] == ":":
            value_at = after + 1
            end = _pair_end(text, value_at, boundary)
            value = text[value_at:end]
            if value.strip() and value.strip() != REDACTED:
                found.add(value.strip())
                out.append(text[position:value_at] + REDACTED)
            else:
                out.append(text[position:end])
        else:
            end = _group_end(text, after)
            group = text[after:end]
            labels = list(dict.fromkeys(_OLD_NEW.findall(group)))
            for part in _OLD_NEW.split(group)[2::2]:
                value = part.strip().rstrip(",)").strip()
                if value and value not in (REDACTED, "N/A"):
                    found.add(value)
            kept = ", ".join(f"{label}: {REDACTED}" for label in labels) or REDACTED
            out.append(text[position:after] + f"({kept})")
        position = end
    out.append(text[position:])
    return "".join(out), unparseable


def _word(secret: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(secret)}(?![A-Za-z0-9])")


def _scrub_text(
    text: str, affected_class: str | None, affected: str | None, found: set[str]
) -> str:
    """The whole of :func:`redact_text`, sharing the secrets already met in the record.

    ``found`` goes in with the secrets the record's DNs gave, and comes out with
    the ones its pairs added, so a later field of the same record is scrubbed of
    all of them.
    """
    policy = _policy()
    if policy.in_response is not None and any(p in text for p in policy.prefixes):
        text, _ = _cut_segments(policy.in_response, text, found)

    names, boundary = _names_for(affected_class, affected)
    if names and names & set(_PAIR_NAME.findall(text)):
        text, unparseable = _scrub_pairs(text, names, boundary, found)
        if unparseable:
            return REDACTED

    for secret in sorted(found, key=len, reverse=True):
        text = _word(secret).sub(REDACTED, text)
    # What a whole-word pass cannot reach: a long secret embedded in a bigger token.
    # Fail closed — the field is not worth a leak.
    if any(len(secret) > 6 and secret in text for secret in found):
        return REDACTED
    return text


def redact_text(
    text: str, *, affected_class: str | None = None, affected: str | None = None
) -> str:
    """``text`` — an audit record's ``changeSet`` or ``descr`` — with its secrets scrubbed.

    Best effort, and says so. The secret *pairs* are found in both formats of
    ``changeSet`` (``prop:value, prop:value`` and ``prop (Old: x, New: y)``); the
    class whose properties are secret comes from ``affected_class``, else from the
    last RN of the ``affected`` DN, else it is any class's (over-masking, never
    raising). Secret RN segments in DNs are cut, and every secret met is then
    removed, as a whole word, from the rest of the text. Text that names a secret
    property but cannot be parsed, or that still holds a secret once scrubbed, is
    replaced whole.
    """
    found: set[str] = set()
    policy = _policy()
    if affected and policy.in_response is not None:
        _cut_segments(policy.in_response, affected, found)
    return _scrub_text(text, affected_class, affected, found)


# ── Objects ───────────────────────────────────────────────────────────────────


@lru_cache(maxsize=512)
def _carries_free_text(class_name: str) -> bool:
    return bool(free_text_props(class_name))


def _edits(
    class_name: str, attrs: Mapping[str, Any], on_unknown_class: str
) -> tuple[dict[str, Any], bool]:
    """The attributes of ``attrs`` that change, and whether free text was among them."""
    policy = _policy()
    edits: dict[str, Any] = {}
    scrubbed = False

    secrets = policy.values.get(class_name)
    if secrets is None:
        secrets = _NO_ATTRIBUTES
        if not _known_class(class_name):
            if on_unknown_class == "raise":
                raise KeyError(class_name)
            if on_unknown_class == "heuristic":
                secrets = frozenset(k for k in attrs if policy.heuristic.search(k))
    for attribute in secrets:
        if attribute in attrs:
            edits[attribute] = REDACTED

    pattern = policy.in_response
    mentions = pattern is not None and _mentions_a_prefix(policy.prefixes, attrs)
    carrier = _carries_free_text(class_name)
    if not mentions and not carrier:
        return edits, scrubbed

    found: set[str] = set()
    if pattern is not None and mentions:
        for key, value in attrs.items():
            if isinstance(value, str) and key[:1] != "_" and key not in secrets:
                text, replaced = _cut_segments(pattern, value, found)
                if replaced:
                    edits[key] = text

    affected = attrs.get("affected")
    for key in _TEXT_ATTRIBUTES:
        value = attrs.get(key)
        if not isinstance(value, str) or not value or key in secrets:
            continue
        text = edits.get(key, value)
        if carrier:
            text = _scrub_text(text, None, affected if isinstance(affected, str) else None, found)
        elif found:
            for secret in sorted(found, key=len, reverse=True):
                text = _word(secret).sub(REDACTED, text)
        if text != value:
            edits[key] = text
            scrubbed = True
    return edits, scrubbed


def redact(
    class_name: str, attrs: Mapping[str, Any], *, on_unknown_class: str = "heuristic"
) -> dict[str, Any]:
    """``attrs`` of a ``class_name`` object with every secret withheld. Pure.

    * A secret attribute is replaced by :data:`REDACTED` **even when empty**, so
      "set" and "unset" cannot be told apart. An attribute that is absent stays
      absent.
    * A secret RN segment in any string value is cut: the naming property, ``dn``,
      ``rn``, a ``tDn``, an ``affected``.
    * On the classes that carry free text (``changeSet``), the text is scrubbed:
      see :func:`redact_text`.

    ``on_unknown_class`` says what to do about a class the catalogue does not
    know — a fabric newer than it: ``"heuristic"`` (default) withholds attributes
    whose *name* looks like a secret, ``"keep"`` leaves them, ``"raise"`` raises
    ``KeyError``. The DN and text rules apply to every class.
    """
    if on_unknown_class not in _UNKNOWN_MODES:
        raise ValueError(f"on_unknown_class must be one of {_UNKNOWN_MODES}, not {on_unknown_class!r}")
    edits, _ = _edits(class_name, attrs, on_unknown_class)
    return {**attrs, **edits}


def redact_object(item: Mapping[str, Any], *, on_unknown_class: str = "heuristic") -> dict[str, Any]:
    """An APIC ``imdata`` item — ``{cls: {"attributes": {...}, "children": [...]}}`` — redacted.

    Recursive: each child is an item of its own class.
    """
    ((class_name, body),) = item.items()
    out = {**body, "attributes": redact(class_name, body.get("attributes", {}), on_unknown_class=on_unknown_class)}
    if "children" in body:
        out["children"] = [redact_object(child, on_unknown_class=on_unknown_class) for child in body["children"]]
    return {class_name: out}


# ── niwashi: the shape the tools hold, and what they tell the agent ───────────


class Redacted[T](NamedTuple):
    """A result set after redaction, with what the agent is told about it.

    Attributes:
        value: The objects, or the one object. Unchanged objects are the same
            objects: nothing is copied when there is nothing to withhold.
        withheld: How many attribute values were replaced, children included.
        scrubbed_text: Whether any of them was free text, which is scrubbed on a
            best-effort basis rather than withheld outright.
    """

    value: T
    withheld: int
    scrubbed_text: bool


def _redact_result(obj: dict[str, Any]) -> tuple[dict[str, Any], int, bool]:
    edits, scrubbed = _edits(obj.get("_class", ""), obj, "heuristic")
    count = len(edits)
    children = obj.get("_children")
    if isinstance(children, list):
        done = [_redact_result(c) if isinstance(c, dict) else (c, 0, False) for c in children]
        if any(new is not old for old, (new, _, _) in zip(children, done, strict=True)):
            edits["_children"] = [new for new, _, _ in done]
            count += sum(n for _, n, _ in done)
            scrubbed = scrubbed or any(s for _, _, s in done)
    return ({**obj, **edits} if edits else obj), count, scrubbed


def redact_results(objects: list[dict[str, Any]]) -> Redacted[list[dict[str, Any]]]:
    """Redact the flat objects ``ApicClient`` returns (``_class``, attributes, ``_children``).

    ``value`` is the list of objects. This, not :func:`redact_object`, is what the
    tools call: it works on the shape they hold and counts what it changed.
    """
    done = [_redact_result(obj) for obj in objects]
    return Redacted(
        [obj for obj, _, _ in done],
        sum(n for _, n, _ in done),
        any(s for _, _, s in done),
    )


def redact_one(obj: dict[str, Any]) -> Redacted[dict[str, Any]]:
    """:func:`redact_results` for a single object."""
    new, count, scrubbed = _redact_result(obj)
    return Redacted(new, count, scrubbed)


# ── niwashi: refusing the requests that would reveal one ──────────────────────


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
    keys, and a ``wcard`` on an audit record's ``changeSet`` confirms what was
    scrubbed out of it. None of those is a catalogue question, so unlike an
    unknown attribute this holds with ``NIWASHI_MCP_STRICT_ATTRIBUTES`` off.

    Attribute references in ``filter_expr`` are found by shape
    (:func:`dotted_references`), not by the conservative extractor the typo check
    uses, so a predicate cannot slip past by how it is written.

    Not covered, and said so: a ``wcard`` over a DN or free-text attribute of
    *another* class that happens to quote a secret (an audit record's
    ``affected`` or ``descr``) can still be probed by a substring that does not
    spell the RN prefix. What such a record shows is scrubbed; what it matches is
    not.

    Raises:
        SecretAttributeError: a predicate, ordering or grouping uses a secret or
            free-text attribute.
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
        if attribute in free_text_props(owner):
            raise SecretAttributeError(owner, attribute, where, free_text=True)
        if attribute in refused_props(owner) or is_secret(owner, attribute):
            raise SecretAttributeError(owner, attribute, where)


# ── niwashi: what get_schema says ─────────────────────────────────────────────


def mark_secret_properties(schema: dict[str, Any], class_name: str) -> dict[str, Any]:
    """Label the properties of ``schema``'s ``property_details`` the server will not return raw.

    ``"secret": true`` on a secret property (and on ``dn`` / ``rn`` of a class named
    by its secret), ``"scrubbed": true`` on free text. The names stay listed — an
    agent that sees a property exist is less likely to go looking for it another
    way — and are labelled so it knows not to ask. No value is involved. Done
    here, on top of :func:`catalog.load_schema`, for the reason
    ``annotate_relations`` is: that function is the projection the recorded
    baseline pins. Mutates and returns ``schema``, which is freshly built per call.
    """
    details = schema.get("property_details")
    if isinstance(details, dict):
        secret = secret_props(class_name) | (_DN_PROPERTIES if has_secret_dn(class_name) else _NO_ATTRIBUTES)
        text = free_text_props(class_name)
        for name, detail in details.items():
            if isinstance(detail, dict):
                if name in secret:
                    detail["secret"] = True
                if name in text:
                    detail["scrubbed"] = True
    return schema
