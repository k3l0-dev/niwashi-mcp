# Copyright (C) 2026 Khalid El-Ouiali — MONARK AIOPS srl
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
tests/live/test_live_redaction.py

The redaction policy against a real APIC: does the tool surface hand an agent a
secret that the APIC actually returned?

Read-only (every call is a GET), and it must never print a secret — including
when it fails, which is exactly when a secret would be in play. Two rules follow
from that and are kept throughout:

* No `assert` has a secret, or anything that holds one, as an operand. pytest
  echoes the operands of a failing assertion — `assert f(secrets, answer) == 0`
  prints `secrets` and `answer` — so every figure is computed on its own line
  first, and the assertion compares plain integers and booleans.
* A check that a secret is *absent* from an answer is made only for secrets that
  appear nowhere else in what the APIC sent. A trap community that is also the
  name of the policy it sits in (measured on the lab fabric) is visible, by
  design, as that name — finding it there is a coincidence of values, not a
  leak, and the policy redacts by position, not by value.

The lab fabric these were written against holds 16 `vnsCCred` with a non-empty
`value`, 7 `snmpTrapDest` with a `secName`, an `snmpCommunityP`, several
`aaaActiveUserSession`, and audit and event records that quote all of those in
their `changeSet`. A class the fabric has none of is skipped, not passed.

Run with:  uv run pytest tests/live/test_live_redaction.py -m live
"""

import json
import re

import pytest
from niwaki.snapshot import REDACTED

from niwashi_mcp.exceptions import SecretAttributeError, SecretDnError
from niwashi_mcp.main import count, get_by_dn, query
from tests.conftest import make_ctx

pytestmark = [pytest.mark.live, pytest.mark.asyncio(loop_scope="session")]


@pytest.fixture
def ctx(live_client, catalogue_index):
    return make_ctx({"descriptions": catalogue_index, "backend": live_client})


async def _raw(live_client, class_name: str) -> list[dict]:
    result = await live_client.query_class(class_name, {}, fetch_all=True)
    if not result.objects:
        pytest.skip(f"the lab fabric has no {class_name}")
    return result.objects


def _values(raw: list[dict], attribute: str) -> set[str]:
    return {o[attribute] for o in raw if o.get(attribute)}


def _distinctive(secrets: set[str], raw: list[dict], *, expected_in: tuple[str, ...]) -> set[str]:
    """The secrets whose appearing in an answer can only be a leak.

    Long enough to mean something, and found nowhere in the raw objects except
    the attributes named in `expected_in` — the ones the policy is meant to cut.
    """
    return {
        s
        for s in secrets
        if len(s) >= 6
        and not any(
            s in v
            for o in raw
            for k, v in o.items()
            if k not in expected_in and isinstance(v, str)
        )
    }


def _how_many_appear(secrets: set[str], answer: object) -> int:
    dump = json.dumps(answer)
    return sum(1 for s in secrets if s in dump)


# ── values ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("class_name", "attribute"),
    [("vnsCCred", "value"), ("snmpTrapDest", "secName")],
)
async def test_a_curated_secret_the_apic_echoes_in_clear_is_withheld(
    live_client, ctx, class_name, attribute
):
    raw = await _raw(live_client, class_name)
    secrets = _values(raw, attribute)
    if not secrets:
        pytest.skip(f"no {class_name} on the lab fabric carries a non-empty {attribute}")

    envelope = await query(class_name, ctx, fetch_all=True)

    sent = [o.get(attribute) for o in raw]
    shown = [o.get(attribute) for o in envelope["results"]]
    marker = shown.count(REDACTED)
    other = len(shown) - marker
    leaked = _how_many_appear(_distinctive(secrets, raw, expected_in=(attribute,)), envelope)
    withheld = envelope["redacted"]
    said_so = "withheld" in (envelope["note"] or "")

    assert other == 0, "an attribute is not the marker"
    assert marker == len(sent), "set and unset values must both be withheld"
    assert withheld == len(sent), "the count does not match what was withheld"
    assert leaked == 0, "a secret is in the envelope"
    assert said_so, "the note does not say anything was withheld"


async def test_the_same_secret_through_every_other_door(live_client, ctx):
    raw = await _raw(live_client, "vnsCCred")
    secrets = _distinctive(_values(raw, "value"), raw, expected_in=("value",))
    if not _values(raw, "value"):
        pytest.skip("no vnsCCred on the lab fabric carries a non-empty value")
    dn = next(o["dn"] for o in raw if o.get("value"))

    outputs = [
        await get_by_dn(dn, ctx),
        await get_by_dn(dn, ctx, fields=["name", "value"]),
        await get_by_dn(dn, ctx, config_only=True),
        await query("vnsCCred", ctx, fields=["value"], fetch_all=True),
        await query("vnsCCred", ctx, config_only=True, limit=200),
        await query("vnsCCred", ctx, order_by="vnsCCred.name"),
        await count("vnsCCred", ctx, group_by="name"),
    ]

    leaked = _how_many_appear(secrets, outputs)
    by_dn = outputs[0]
    projected = outputs[1]
    withheld_by_dn = (by_dn["value"] == REDACTED, by_dn["_redacted"] == 1)
    withheld_projected = projected["value"] == REDACTED

    assert leaked == 0, "a secret came through one of the tools"
    assert all(withheld_by_dn), "get_by_dn did not withhold and say so"
    assert withheld_projected, "a projected secret was not the marker"


# ── DNs ───────────────────────────────────────────────────────────────────────


async def test_a_community_profile_loses_its_name_and_its_dn_segment(live_client, ctx):
    raw = await _raw(live_client, "snmpCommunityP")
    secrets = _distinctive(
        _values(raw, "name"), raw, expected_in=("name", "dn", "rn")
    )

    envelope = await query("snmpCommunityP", ctx, fetch_all=True)

    shown = envelope["results"]
    names_withheld = sum(1 for o in shown if o["name"] == REDACTED)
    dns_cut = sum(1 for o in shown if o["dn"].endswith(f"community-{REDACTED}"))
    expected_dns = sorted(
        o["dn"].replace(f"community-{o['name']}", f"community-{REDACTED}") for o in raw
    )
    dns_exact = sorted(o["dn"] for o in shown) == expected_dns
    leaked = _how_many_appear(secrets, envelope)
    withheld = envelope["redacted"]

    assert names_withheld == len(raw), "a community name is shown"
    assert dns_cut == len(raw), "a community is still in a DN"
    assert dns_exact, "the DN was altered somewhere other than the secret segment"
    assert leaked == 0, "the community string is in the envelope"
    assert withheld >= 2 * len(raw), "fewer than the name and the DN of each profile"


async def test_a_login_session_loses_the_token_that_names_it(live_client, ctx):
    raw = await _raw(live_client, "aaaActiveUserSession")
    tokens = _values(raw, "hashToken") | {o["dn"].rsplit("actsession-", 1)[-1] for o in raw}
    secrets = _distinctive(tokens, raw, expected_in=("hashToken", "dn", "rn"))

    envelope = await query("aaaActiveUserSession", ctx, fetch_all=True)

    shown = envelope["results"]
    dns_cut = sum(1 for o in shown if o["dn"].endswith(f"actsession-{REDACTED}"))
    tokens_withheld = sum(1 for o in shown if o.get("hashToken") == REDACTED)
    leaked = _how_many_appear(secrets, envelope)

    assert dns_cut == len(raw), "a session token is still in a DN"
    assert tokens_withheld == sum(1 for o in raw if o.get("hashToken"))
    assert leaked == 0, "a session token is in the envelope"


async def test_a_dn_that_names_a_secret_is_refused_without_asking_the_apic(live_client, ctx):
    raw = await _raw(live_client, "snmpCommunityP")
    real = raw[0]["dn"]
    absent = real.rsplit("community-", 1)[0] + "community-not-a-community-anyone-set"

    refusals = []
    for dn in (real, absent):
        with pytest.raises(SecretDnError) as caught:
            await get_by_dn(dn, ctx)
        refusals.append(str(caught.value))

    same = len(set(refusals)) == 1
    leaked = _how_many_appear(_distinctive(_values(raw, "name"), raw, expected_in=("name", "dn")), refusals)

    assert same, "an existing and a missing DN got different answers"
    assert leaked == 0, "the refusal echoed the community"


async def test_an_audit_record_of_a_community_does_not_quote_it(live_client, ctx):
    raw = await live_client.query_class(
        "aaaModLR", {}, filter_expr='wcard(aaaModLR.affected,"community-")', limit=200
    )
    if not raw.objects:
        pytest.skip("the lab fabric has no audit record that touches a community")
    communities = {
        m[1]
        for o in raw.objects
        for text in (o["dn"], o["affected"])
        if (m := re.search(r"community-([^/\]]+)", text))
    }
    assert communities, "the record does not name a community the way this test reads it"

    # The bare prefix is not a guess, so the question itself is allowed.
    envelope = await query(
        "aaaModLR", ctx, filter_expr='wcard(aaaModLR.affected,"community-")', limit=200
    )

    shown = envelope["results"]
    whole_word = [re.compile(rf"(?<![A-Za-z0-9]){re.escape(c)}(?![A-Za-z0-9])") for c in communities]
    words_left = sum(
        1
        for o in shown
        for key in ("descr", "changeSet")
        for pattern in whole_word
        if pattern.search(o.get(key, ""))
    )
    segments_left = sum(
        1
        for o in shown
        for key in ("dn", "affected")
        for c in communities
        if f"community-{c}" in o.get(key, "")
    )
    returned_all = envelope["returned"] == len(raw.objects)
    withheld = envelope["redacted"]

    assert returned_all, "the redacted query returned a different set"
    assert segments_left == 0, "a community is still in a DN"
    assert words_left == 0, "an audit record still quotes the community in its text"
    assert withheld >= len(raw.objects), "fewer withheld than there are records"


# ── audit, event and session records ──────────────────────────────────────────

# Where the lab's APIC was measured to write a secret into its own records. The
# properties whose name alone says secret (`value` and `name` are not among them:
# they are generic, and only secret on a credential and a community).
_SECRET_PAIR = re.compile(
    r"(?:^|, )(otpkey|secName|authPass|hashToken|currCertReqPassphrase|oldCertReqPassphrase)"
    r"(?::(.*?)(?=, [A-Za-z][A-Za-z0-9]*:|$)| \(Old: (.*?), New: (.*?)\)(?=, |$))"
)
_SECRET_SEGMENT = re.compile(r"(?:community|actsession|delsession|sitejwtpubkey)-[^/\]]+")
_RECORD_CLASSES = ("aaaModLR", "eventRecord", "aaaSessionLR")
_SOURCES = (
    ("vnsCCred", "value"), ("snmpTrapDest", "secName"), ("snmpCommunityP", "name"),
    ("aaaActiveUserSession", "hashToken"), ("aaaDeletedUserSession", "hashToken"),
    ("aaaUser", "otpkey"), ("aaaUserProfile", "otpkey"),
)


_NAMED_BY_SECRET = ("snmpCommunityP", "aaaActiveUserSession", "aaaDeletedUserSession")


async def _known_secrets(live_client) -> set[str]:
    """The values at known secret positions on the lab, whose appearing in a record is a leak.

    Read from the objects that hold them, and kept only when found *nowhere else*
    in what those objects say: a secret that is also a name on the fabric (the
    lab's trap community is its policy's name, and a user's) is visible there by
    design, and its turning up in a record is no evidence of a leak.
    """
    allowed: dict[str, set[tuple[str, str]]] = {}
    corpus: list[tuple[str, dict]] = []
    for class_name, attribute in _SOURCES:
        raw = (await live_client.query_class(class_name, {}, fetch_all=True)).objects
        corpus += [(class_name, o) for o in raw]
        named_by_it = ("dn", "rn") if class_name in _NAMED_BY_SECRET else ()
        for secret in _values(raw, attribute):
            if len(secret) >= 6:
                allowed.setdefault(secret, set()).update(
                    (class_name, a) for a in (attribute, *named_by_it)
                )
    return {
        secret
        for secret, positions in allowed.items()
        if not any(
            isinstance(v, str) and secret in v
            for class_name, o in corpus
            for k, v in o.items()
            if (class_name, k) not in positions
        )
    }


def _pair_values(text: str) -> set[str]:
    """Values the records' own text gives for the secret properties, by an independent reading."""
    values = set()
    for match in _SECRET_PAIR.finditer(text):
        values |= {v.strip() for v in match.groups()[1:] if v and v.strip() not in ("", "N/A")}
    return values


@pytest.mark.parametrize("class_name", _RECORD_CLASSES)
async def test_no_record_returned_through_the_tools_holds_a_value_at_a_known_secret_position(
    live_client, ctx, class_name
):
    raw = await _raw(live_client, class_name)
    secrets = await _known_secrets(live_client)
    if class_name == "aaaSessionLR":
        secrets |= _distinctive(_values(raw, "sessionId"), raw, expected_in=("sessionId",))
    # Not the values the records' own pairs give: those are checked by position
    # below (`pairs_left`), because a community that is also a policy's name turns
    # up, legitimately, in other records' DNs and text.
    tokens = {m[1] for o in raw for k in ("dn", "affected") if (m := re.search(r"(?:act|del)session-([^/\]]+)", o.get(k, "")))}
    secrets |= tokens
    # A secret that is also the name of something else on the fabric — a policy,
    # an SNMPv3 user, a tenant — shows up as that name in the records' DNs, and is
    # no evidence of a leak. What is left of a DN once its secret segments are
    # taken out says whether the string is a name there.
    named_elsewhere = {
        secret
        for secret in secrets
        if any(secret in _SECRET_SEGMENT.sub("", o.get(k, "")) for o in raw for k in ("dn", "affected"))
    }
    secrets -= named_elsewhere

    envelope = await query(class_name, ctx, fetch_all=True)

    shown = envelope["results"]
    # Not "the same set": the tools' own logins write new session and event records.
    returned_most = len(shown) >= len(raw) * 0.95
    in_raw = sum(1 for o in raw if any(secret in json.dumps(o) for secret in secrets))
    in_shown = sum(1 for o in shown if any(secret in json.dumps(o) for secret in secrets))
    segments_left = sum(
        1
        for o in shown
        for key in ("dn", "affected")
        if re.search(r"(?:actsession|delsession|community|sitejwtpubkey)-(?!<redacted>)[^/\]]", o.get(key, ""))
    )
    pairs_left = sum(
        1
        for o in shown
        for key in ("changeSet", "descr")
        for match in _SECRET_PAIR.finditer(o.get(key, ""))
        if any(v and REDACTED not in v and v.strip() not in ("", "N/A") for v in match.groups()[1:])
    )
    sessions_left = (
        sum(1 for o in shown if o.get("sessionId") not in (None, REDACTED))
        if class_name == "aaaSessionLR"
        else 0
    )

    assert returned_most, "the redacted query returned far fewer records than the APIC holds"
    assert in_shown == 0, "a record still holds a value at a known secret position"
    assert segments_left == 0, "a secret RN segment is still in a DN"
    assert pairs_left == 0, "a secret pair in a changeSet kept its value"
    assert sessions_left == 0, "a sessionId was returned"
    if in_raw == 0 and class_name != "aaaSessionLR":
        pytest.skip(f"no {class_name} on the lab fabric quotes a known secret, so nothing was exercised")


async def test_the_lab_records_that_quote_a_secret_are_the_ones_that_were_scrubbed(live_client, ctx):
    """The positive half: there was something to scrub, and the tools did scrub it."""
    raw = await _raw(live_client, "eventRecord")
    quoting = {o["id"] for o in raw if _pair_values(o.get("changeSet", ""))}
    if not quoting:
        pytest.skip("no eventRecord on the lab fabric quotes a secret property in its changeSet")

    envelope = await query("eventRecord", ctx, fetch_all=True)

    shown = {o["id"]: o for o in envelope["results"]}
    present = sum(1 for i in quoting if i in shown)
    scrubbed = sum(1 for i in quoting if REDACTED in shown.get(i, {}).get("changeSet", ""))
    announced = "best-effort" in (envelope["note"] or "")

    assert present >= len(quoting) * 0.95, "the records that quote a secret did not come back"
    assert scrubbed == present, "a record that quoted a secret has no marker in its changeSet"
    assert announced, "the note does not say that free text is scrubbed on a best-effort basis"


async def test_audit_text_cannot_be_probed(live_client, ctx):
    refusals = 0
    for call in (
        query("aaaModLR", ctx, filter_expr='wcard(aaaModLR.changeSet,"value")', limit=1),
        query("eventRecord", ctx, filters={"changeSet": "x"}, limit=1),
        query("aaaModLR", ctx, order_by="aaaModLR.changeSet", limit=1),
        count("eventRecord", ctx, group_by="changeSet"),
    ):
        with pytest.raises(SecretAttributeError):
            await call
        refusals += 1

    assert refusals == 4


# ── the oracles ───────────────────────────────────────────────────────────────


async def test_a_real_secret_cannot_be_used_as_a_filter_value(live_client, ctx):
    raw = await _raw(live_client, "vnsCCred")
    guess = next((o["value"] for o in raw if o.get("value")), None)
    if guess is None:
        pytest.skip("no vnsCCred on the lab fabric carries a non-empty value")

    refusals = []
    for call in (
        query("vnsCCred", ctx, filters={"value": guess}),
        count("vnsCCred", ctx, filters={"value": guess}),
        query("vnsCCred", ctx, filter_expr=f'eq(vnsCCred.value,"{guess}")'),
        count("vnsCCred", ctx, group_by="value"),
        query("vnsCCred", ctx, order_by="vnsCCred.value"),
    ):
        with pytest.raises(SecretAttributeError) as caught:
            await call
        refusals.append(str(caught.value))

    echoed = _how_many_appear({guess}, refusals)

    assert echoed == 0, "a refusal echoed the secret it was asked about"


# ── ordinary objects ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("class_name", ["fvTenant", "fvBD"])
async def test_ordinary_objects_come_back_exactly_as_the_apic_sent_them(
    live_client, ctx, class_name
):
    sent = await live_client.query_class(class_name, {}, limit=50)
    if not sent.objects:
        pytest.skip(f"the lab fabric has no {class_name}")

    envelope = await query(class_name, ctx, limit=50)

    identical = json.dumps(envelope["results"]) == json.dumps(sent.objects)
    announced = "redacted" in envelope or "withheld" in (envelope["note"] or "")

    assert identical, "an ordinary object was altered"
    assert not announced, "something was reported withheld from objects with no secret"
