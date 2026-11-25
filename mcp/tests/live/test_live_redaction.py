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
`value`, 7 `snmpTrapDest` with a `secName`, an `snmpCommunityP`, and several
`aaaActiveUserSession`. A class the fabric has none of is skipped, not passed.

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
    set_before = sum(1 for s in sent if s)
    unset_before = len(sent) - set_before
    marker = shown.count(REDACTED)
    empty = shown.count("")
    neither = len(shown) - marker - empty
    leaked = _how_many_appear(_distinctive(secrets, raw, expected_in=(attribute,)), envelope)
    withheld = envelope["redacted"]
    said_so = "withheld" in (envelope["note"] or "")

    assert neither == 0, "an attribute is neither the marker nor empty"
    assert marker == set_before, "not every set value was withheld"
    assert empty == unset_before, "an unset value was not left unset"
    assert withheld == set_before, "the count does not match what was withheld"
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
