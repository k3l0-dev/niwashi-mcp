# Copyright (C) 2026 Khalid El-Ouiali — MONARK AIOPS srl
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
The tools never hand an agent a secret — end to end, over the in-memory backend.

`tests/unit/test_redaction.py` pins the policy over synthetic objects. This file
asks the question that matters to the person who runs the server: whatever an
agent calls, with whatever arguments, does anything secret reach it? So every
check goes through the real tool functions and the real MCP client, over a
`StubBackend` that serves the objects a lab APIC was measured to serve.

No secret here is real. Each is the sentinel `zzsecret` (or `zztoken`), and every
probe an "agent" sends is `zzguess`, so a leak — in a result, an error or a log
line — is a substring search.
"""

from __future__ import annotations

import json

import pytest
from fastmcp import Client
from fastmcp.server.lifespan import lifespan
from niwaki.snapshot import REDACTED

from niwashi_mcp import main
from niwashi_mcp.exceptions import SecretAttributeError, SecretDnError
from niwashi_mcp.main import count, get_by_dn, get_schema, query
from tests.conftest import StubBackend, make_ctx

SECRET = "zzsecret"
TOKEN = "zztoken"
OTP = "zzotpseed"
SESSION_ID = "zzsession+id/x=="
GUESS = "zzguess"
COMMUNITY_DN = f"uni/fabric/snmpp-default/community-{SECRET}"
SESSION_DN = f"uni/ac/actsession-{TOKEN}"

_IMDATA = [
    {"vnsCCred": {"attributes": {
        "dn": "uni/tn-a/lDevVip-D/cDev-C/cCred", "name": "admin", "value": SECRET,
        "modTs": "2026-01-01T00:00:00.000+00:00"}}},
    {"vnsCCred": {"attributes": {
        "dn": "uni/tn-a/lDevVip-E/cDev-C/cCred", "name": "admin", "value": ""}}},
    {"snmpTrapDest": {"attributes": {
        "dn": "uni/fabric/snmpp-default/trapdest-10.0.0.1-port-162",
        "host": "10.0.0.1", "port": "162", "secName": SECRET}}},
    {"snmpCommunityP": {
        "attributes": {"dn": COMMUNITY_DN, "name": SECRET, "descr": ""},
        "children": [
            {"faultInst": {"attributes": {
                "dn": f"{COMMUNITY_DN}/fault-F1", "code": "F1", "descr": f"fault on {SECRET}"}}},
        ],
    }},
    {"aaaModLR": {"attributes": {
        "dn": f"subj-[{COMMUNITY_DN}]/mod-1", "affected": COMMUNITY_DN, "code": "E4207843",
        "descr": f"Community {SECRET} created", "changeSet": f"name (Old: N/A, New: {SECRET})"}}},
    {"aaaModLR": {"attributes": {
        "dn": "subj-[uni/tn-OT/BD-servers]/mod-2", "affected": "uni/tn-OT/BD-servers",
        "code": "E4204956", "descr": "Bridge Domain servers created",
        "changeSet": "name (Old: N/A, New: servers)"}}},
    {"aaaActiveUserSession": {"attributes": {
        "dn": SESSION_DN, "hashToken": TOKEN, "name": "admin"}}},
    {"eventRecord": {"attributes": {
        "dn": f"subj-[{SESSION_DN}]/evt-1", "affected": SESSION_DN, "code": "E4208341",
        "descr": f"ActiveUserSession {TOKEN} created",
        "changeSet": f"hashToken:{TOKEN}, name:admin, userdom:all"}}},
    {"eventRecord": {"attributes": {
        "dn": "subj-[uni/userext/user-bob]/evt-2", "affected": "uni/userext/user-bob",
        "code": "E4204979", "descr": "bob created",
        "changeSet": f"name:bob, otpkey:{OTP}, userdom:all"}}},
    {"eventRecord": {"attributes": {
        "dn": "subj-[uni/userext/user-bob]/evt-3", "affected": "uni/userext/user-bob",
        "code": "E4204980", "descr": "bob modified",
        "changeSet": f"otpkey (Old: {OTP}, New: x), descr (Old: , New: hello)"}}},
    {"eventRecord": {"attributes": {
        "dn": "subj-[uni/tn-OT/BD-servers]/evt-4", "affected": "uni/tn-OT/BD-servers",
        "code": "E4204981", "descr": "BD servers created",
        "changeSet": "name:servers, arpFlood:no, userdom:all"}}},
    {"aaaSessionLR": {"attributes": {
        "dn": "subj-[uni/userext]/sess-7", "id": "7", "sessionId": SESSION_ID,
        "descr": "[user admin] From-10.0.0.1-client-type-REST-Success"}}},
    {"fvBD": {
        "attributes": {
            "dn": "uni/tn-OT/BD-servers", "name": "servers", "descr": "prod", "arpFlood": "no"},
        "children": [
            {"fvSubnet": {"attributes": {
                "dn": "uni/tn-OT/BD-servers/subnet-[10.0.1.1/24]", "ip": "10.0.1.1/24"}}},
            {"vnsCCred": {"attributes": {
                "dn": "uni/tn-OT/BD-servers/cCred", "name": "x", "value": SECRET}}},
        ],
    }},
    {"fvBD": {"attributes": {"dn": "uni/tn-OT/BD-clients", "name": "clients", "arpFlood": "yes"}}},
    {"fvTenant": {"attributes": {"dn": "uni/tn-OT", "name": "OT", "descr": ""}}},
]

# What an ordinary fabric returns: nothing here is, or sits under, a secret.
_PLAIN = [
    {"fvBD": {
        "attributes": {
            "dn": "uni/tn-OT/BD-servers", "name": "servers", "descr": "prod", "arpFlood": "no",
            "modTs": "2026-01-01T00:00:00.000+00:00"},
        "children": [
            {"fvSubnet": {"attributes": {
                "dn": "uni/tn-OT/BD-servers/subnet-[10.0.1.1/24]", "ip": "10.0.1.1/24"}}},
        ],
    }},
    {"fvBD": {"attributes": {"dn": "uni/tn-OT/BD-clients", "name": "clients", "arpFlood": "yes"}}},
    {"fvTenant": {"attributes": {"dn": "uni/tn-OT", "name": "OT", "descr": ""}}},
]


@pytest.fixture(autouse=True)
def _strict(monkeypatch):
    monkeypatch.delenv("NIWASHI_MCP_STRICT_ATTRIBUTES", raising=False)


@pytest.fixture
def ctx(catalogue_index):
    return make_ctx({"descriptions": catalogue_index, "backend": StubBackend(_IMDATA)})


def _backend(ctx) -> StubBackend:
    return ctx.lifespan_context["backend"]


def _logged(ctx) -> str:
    return " ".join(str(c) for m in (ctx.info, ctx.warning) for c in m.call_args_list)


async def _raw(class_name: str, **kwargs) -> list[dict]:
    """What the backend sent, before any policy touched it."""
    return (await StubBackend(_IMDATA).query_class(class_name, {}, **kwargs)).objects


# ── query: values ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestQueryWithholdsValues:
    async def test_a_credential_and_a_trap_community_read_as_the_marker(self, ctx):
        creds = await query("vnsCCred", ctx, fetch_all=True)
        traps = await query("snmpTrapDest", ctx)

        by_dn = {o["dn"]: o for o in creds["results"]}
        assert by_dn["uni/tn-a/lDevVip-D/cDev-C/cCred"]["value"] == REDACTED
        assert by_dn["uni/tn-a/lDevVip-D/cDev-C/cCred"]["name"] == "admin"
        assert traps["results"][0]["secName"] == REDACTED
        assert traps["results"][0]["host"] == "10.0.0.1"
        assert SECRET not in json.dumps([creds, traps])

    async def test_an_empty_credential_is_withheld_too_so_set_and_unset_look_alike(self, ctx):
        creds = await query("vnsCCred", ctx, fetch_all=True)

        unset = next(o for o in creds["results"] if o["dn"].endswith("lDevVip-E/cDev-C/cCred"))
        assert unset["value"] == REDACTED
        assert creds["redacted"] == 2, "the set credential and the unset one"

    async def test_the_envelope_says_something_was_withheld_without_saying_what(self, ctx):
        envelope = await query("snmpTrapDest", ctx)

        assert envelope["redacted"] == 1
        assert REDACTED in envelope["note"] and "not an empty value" in envelope["note"]
        assert "secName" not in envelope["note"], "the note named the attribute"

    async def test_the_note_is_appended_not_substituted(self, ctx):
        envelope = await query("vnsCCred", ctx, limit=1)

        assert envelope["truncated"] is True
        assert "partial page" in envelope["note"] and "withheld" in envelope["note"]

    async def test_projecting_a_secret_field_returns_the_marker(self, ctx):
        envelope = await query("vnsCCred", ctx, fields=["name", "value"], fetch_all=True)

        assert {o["value"] for o in envelope["results"]} == {REDACTED}
        assert SECRET not in json.dumps(envelope)

    async def test_a_secret_projected_away_is_neither_present_nor_announced(self, ctx):
        envelope = await query("vnsCCred", ctx, fields=["name"], fetch_all=True)

        assert all("value" not in o for o in envelope["results"])
        assert "redacted" not in envelope and envelope["note"] is None

    async def test_config_only_changes_nothing_about_it(self, ctx):
        envelope = await query("snmpTrapDest", ctx, config_only=True)
        assert envelope["results"][0]["secName"] == REDACTED

    async def test_a_secret_in_an_embedded_child_is_withheld(self, ctx):
        envelope = await query("fvBD", ctx, include_children=["fvSubnet", "vnsCCred"])

        servers = next(o for o in envelope["results"] if o["name"] == "servers")
        cred = next(c for c in servers["_children"] if c["_class"] == "vnsCCred")
        assert cred["value"] == REDACTED
        assert envelope["redacted"] == 1 and SECRET not in json.dumps(envelope)

    async def test_a_secret_in_a_projected_child_is_withheld_too(self, ctx):
        envelope = await query(
            "fvBD", ctx, include_children=["vnsCCred"], fields=["name", "value"]
        )
        servers = next(o for o in envelope["results"] if o["name"] == "servers")
        cred = next(c for c in servers["_children"] if c["_class"] == "vnsCCred")
        assert cred["value"] == REDACTED

    async def test_the_marker_is_niwakis_so_the_sdk_and_this_server_agree(self, ctx):
        envelope = await query("snmpTrapDest", ctx)
        assert envelope["results"][0]["secName"] is not None
        assert envelope["results"][0]["secName"] == "<redacted>" == REDACTED


# ── query: DNs ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestQueryWithholdsDns:
    async def test_a_community_profile_loses_its_name_and_the_secret_in_its_dn(self, ctx):
        envelope = await query("snmpCommunityP", ctx, include_children=["faultInst"])

        (community,) = envelope["results"]
        assert community["name"] == REDACTED
        assert community["dn"] == f"uni/fabric/snmpp-default/community-{REDACTED}"
        assert community["_children"][0]["dn"] == (
            f"uni/fabric/snmpp-default/community-{REDACTED}/fault-F1"
        )
        assert community["_children"][0]["descr"] == f"fault on {REDACTED}"
        assert SECRET not in json.dumps(envelope)

    async def test_a_login_session_loses_the_token_that_names_it(self, ctx):
        envelope = await query("aaaActiveUserSession", ctx)

        (session,) = envelope["results"]
        assert session["dn"] == f"uni/ac/actsession-{REDACTED}"
        assert session["hashToken"] == REDACTED and session["name"] == "admin"
        assert TOKEN not in json.dumps(envelope)

    async def test_an_audit_record_of_a_community_is_cut_everywhere_it_quotes_it(self, ctx):
        envelope = await query("aaaModLR", ctx)

        assert SECRET not in json.dumps(envelope)
        audited = next(o for o in envelope["results"] if "community" in o["affected"])
        assert audited["affected"].endswith(f"community-{REDACTED}")
        assert audited["descr"] == f"Community {REDACTED} created"
        assert audited["code"] == "E4207843"
        assert envelope["redacted"] == 4, "the record's dn, affected, descr and changeSet"

    async def test_a_subtree_scope_still_redacts_what_it_returns(self, ctx):
        envelope = await query("faultInst", ctx, scope_dn="uni/fabric/snmpp-default")
        assert SECRET not in json.dumps(envelope)

    async def test_the_sentence_is_counted_and_the_other_audit_record_is_untouched(self, ctx):
        envelope = await query("aaaModLR", ctx)
        plain = next(o for o in envelope["results"] if o["affected"] == "uni/tn-OT/BD-servers")
        assert plain == (await _raw("aaaModLR"))[1]


# ── query: ordinary objects are not touched ───────────────────────────────────


@pytest.mark.asyncio
class TestOrdinaryObjectsAreByteIdentical:
    async def test_bridge_domains_and_tenants_come_back_exactly_as_the_backend_sent_them(
        self, catalogue_index
    ):
        ctx = make_ctx({"descriptions": catalogue_index, "backend": StubBackend(_PLAIN)})
        for cls in ("fvBD", "fvTenant"):
            envelope = await query(cls, ctx, include_children=["fvSubnet"])
            raw = await StubBackend(_PLAIN).query_class(cls, {}, include_children=["fvSubnet"])
            assert json.dumps(envelope["results"]) == json.dumps(raw.objects)

    async def test_the_envelope_has_no_new_key_when_nothing_was_withheld(self, catalogue_index):
        ctx = make_ctx({"descriptions": catalogue_index, "backend": StubBackend(_PLAIN)})
        envelope = await query("fvBD", ctx)

        assert set(envelope) == {
            "results", "returned", "total_available", "truncated",
            "next_page", "complete", "note", "fields",
        }
        assert envelope["note"] is None

    async def test_get_by_dn_returns_an_ordinary_object_unchanged(self, catalogue_index):
        ctx = make_ctx({"descriptions": catalogue_index, "backend": StubBackend(_PLAIN)})
        obj = await get_by_dn("uni/tn-OT", ctx)
        assert obj == {"dn": "uni/tn-OT", "name": "OT", "descr": "", "_class": "fvTenant"}

    async def test_count_returns_an_ordinary_answer_unchanged(self, catalogue_index):
        ctx = make_ctx({"descriptions": catalogue_index, "backend": StubBackend(_PLAIN)})
        assert await count("fvBD", ctx, group_by="tenant") == {
            "class_name": "fvBD", "count": 2, "scope_dn": None, "filters": {},
            "group_by": "tenant", "groups": {"OT": 2}, "ungrouped": 0,
            "complete": True, "note": None,
        }


# ── the oracles ───────────────────────────────────────────────────────────────

_ATTRIBUTE_PROBES = {
    "query-filters": ("query", {"class_name": "vnsCCred", "filters": {"value": GUESS}}),
    "query-order_by": ("query", {"class_name": "vnsCCred", "order_by": "vnsCCred.value|desc"}),
    "query-filter_expr": ("query", {"class_name": "snmpTrapDest",
                                    "filter_expr": f'eq(snmpTrapDest.secName,"{GUESS}")'}),
    "query-filter_expr-spaced": ("query", {"class_name": "snmpTrapDest",
                                           "filter_expr": f'eq( snmpTrapDest.secName , "{GUESS}" )'}),
    "query-changeSet-filter": ("query", {"class_name": "aaaModLR",
                                         "filters": {"changeSet": GUESS}}),
    "query-changeSet-wcard": ("query", {"class_name": "eventRecord",
                                        "filter_expr": 'wcard(eventRecord.changeSet,"otpkey:zz")'}),
    "query-changeSet-order_by": ("query", {"class_name": "aaaModLR",
                                           "order_by": "aaaModLR.changeSet"}),
    "count-changeSet-group_by": ("count", {"class_name": "eventRecord", "group_by": "changeSet"}),
    "query-sessionId": ("query", {"class_name": "aaaSessionLR", "filters": {"sessionId": GUESS}}),
    "query-the-community-name": ("query", {"class_name": "snmpCommunityP",
                                           "filters": {"name": GUESS}}),
    "query-dn-of-a-secret-class": ("query", {"class_name": "snmpCommunityP",
                                             "filter_expr": 'wcard(snmpCommunityP.dn,"zzgu")'}),
    "count-filters": ("count", {"class_name": "vnsCCred", "filters": {"value": GUESS}}),
    "count-filter_expr": ("count", {"class_name": "vnsCCred",
                                    "filter_expr": f'eq(vnsCCred.value,"{GUESS}")'}),
    "count-group_by": ("count", {"class_name": "vnsCCred", "group_by": "value"}),
    "count-group_by-dn-of-a-secret-class": ("count", {"class_name": "snmpCommunityP",
                                                      "group_by": "dn"}),
}
_DN_PROBES = {
    "get_by_dn": ("get_by_dn", {"dn": f"uni/fabric/snmpp-default/community-{GUESS}"}),
    "get_by_dn-of-a-real-one": ("get_by_dn", {"dn": COMMUNITY_DN}),
    "get_by_dn-a-descendant": ("get_by_dn", {"dn": f"{COMMUNITY_DN}/fault-F1"}),
    "get_by_dn-a-session": ("get_by_dn", {"dn": SESSION_DN}),
    "query-scope_dn": ("query", {"class_name": "faultInst",
                                 "scope_dn": f"uni/fabric/snmpp-default/community-{GUESS}"}),
    "query-filter-value": ("query", {"class_name": "aaaModLR",
                                     "filters": {"affected": f"uni/fabric/snmpp-default/community-{GUESS}"}}),
    "query-filter_expr-literal": ("query", {"class_name": "aaaModLR",
                                            "filter_expr": f'wcard(aaaModLR.affected,"community-{GUESS}")'}),
    "count-scope_dn": ("count", {"class_name": "faultInst",
                                 "scope_dn": f"uni/fabric/snmpp-default/community-{GUESS}"}),
}
_TOOLS = {"query": query, "count": count, "get_by_dn": get_by_dn}


@pytest.mark.asyncio
@pytest.mark.parametrize("strict", ["true", "false"])
class TestNothingCanBeAskedAboutASecret:
    """Whatever NIWASHI_MCP_STRICT_ATTRIBUTES says — it is not a catalogue question."""

    @pytest.mark.parametrize("probe", _ATTRIBUTE_PROBES.values(), ids=_ATTRIBUTE_PROBES)
    async def test_a_predicate_on_a_secret_attribute_is_refused(
        self, probe, strict, ctx, monkeypatch
    ):
        monkeypatch.setenv("NIWASHI_MCP_STRICT_ATTRIBUTES", strict)
        tool, kwargs = probe

        with pytest.raises(SecretAttributeError) as caught:
            await _TOOLS[tool](ctx=ctx, **kwargs)

        assert _backend(ctx).calls == [], "the request reached the backend"
        assert GUESS not in str(caught.value) and GUESS not in _logged(ctx)
        assert "secret" in str(caught.value)

    @pytest.mark.parametrize("probe", _DN_PROBES.values(), ids=_DN_PROBES)
    async def test_a_dn_that_names_a_secret_is_refused_before_the_apic_is_asked(
        self, probe, strict, ctx, monkeypatch
    ):
        monkeypatch.setenv("NIWASHI_MCP_STRICT_ATTRIBUTES", strict)
        tool, kwargs = probe

        with pytest.raises(SecretDnError) as caught:
            await _TOOLS[tool](ctx=ctx, **kwargs)

        assert _backend(ctx).calls == [], "the request reached the backend"
        for leaked in (GUESS, SECRET, TOKEN):
            assert leaked not in str(caught.value) and leaked not in _logged(ctx)


@pytest.mark.asyncio
async def test_a_guess_at_a_community_gets_the_same_answer_whether_or_not_it_is_right(ctx):
    """The property get_by_dn must have: no pair of inputs differs only in existence."""
    answers = []
    for guess in (SECRET, GUESS, "public"):
        with pytest.raises(SecretDnError) as caught:
            await get_by_dn(f"uni/fabric/snmpp-default/community-{guess}", ctx)
        answers.append(str(caught.value))
    assert len(set(answers)) == 1


@pytest.mark.asyncio
async def test_asking_for_the_class_instead_is_the_way_and_it_works(ctx):
    """The refusal is not a dead end: the objects themselves are reachable."""
    assert (await count("snmpCommunityP", ctx))["count"] == 1
    assert (await count("vnsCCred", ctx, group_by="name"))["groups"] == {"admin": 2}
    assert (await query("snmpCommunityP", ctx))["returned"] == 1
    assert (await query("vnsCCred", ctx, filters={"name": "admin"}))["returned"] == 2


# ── free text ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestAuditEventAndSessionRecords:
    """The APIC writes secrets into its own logs; the tools scrub them on the way out."""

    async def test_an_audit_record_of_a_community_is_scrubbed_in_its_changeset(self, ctx):
        envelope = await query("aaaModLR", ctx)

        audited = next(o for o in envelope["results"] if "community" in o["affected"])
        assert audited["changeSet"] == f"name (Old: {REDACTED}, New: {REDACTED})"

    async def test_an_event_record_loses_the_otp_seed_in_both_changeset_formats(self, ctx):
        envelope = await query("eventRecord", ctx, fetch_all=True)

        by_code = {o["code"]: o for o in envelope["results"]}
        assert by_code["E4204979"]["changeSet"] == f"name:bob, otpkey:{REDACTED}, userdom:all"
        assert by_code["E4204980"]["changeSet"] == (
            f"otpkey (Old: {REDACTED}, New: {REDACTED}), descr (Old: , New: hello)"
        )
        assert OTP not in json.dumps(envelope)

    async def test_an_event_record_of_a_session_loses_the_token_in_dn_affected_and_text(self, ctx):
        envelope = await query("eventRecord", ctx, fetch_all=True)

        event = next(o for o in envelope["results"] if o["code"] == "E4208341")
        assert event["dn"] == f"subj-[uni/ac/actsession-{REDACTED}]/evt-1"
        assert event["affected"] == f"uni/ac/actsession-{REDACTED}"
        assert event["descr"] == f"ActiveUserSession {REDACTED} created"
        assert event["changeSet"] == f"hashToken:{REDACTED}, name:admin, userdom:all"

    async def test_the_session_log_loses_the_session_id_whatever_characters_it_has(self, ctx):
        envelope = await query("aaaSessionLR", ctx)

        (record,) = envelope["results"]
        assert record["sessionId"] == REDACTED and record["id"] == "7"
        assert SESSION_ID not in json.dumps(envelope)

    async def test_an_ordinary_event_record_is_byte_identical(self, ctx):
        envelope = await query("eventRecord", ctx, fetch_all=True)
        raw = await _raw("eventRecord")

        plain = next(o for o in envelope["results"] if o["code"] == "E4204981")
        assert json.dumps(plain) == json.dumps(next(o for o in raw if o["code"] == "E4204981"))

    async def test_the_agent_is_told_the_text_is_scrubbed_on_a_best_effort_basis(self, ctx):
        envelope = await query("eventRecord", ctx, fetch_all=True)
        assert "best-effort" in envelope["note"]

    async def test_a_result_whose_text_needed_no_scrubbing_carries_no_such_note(self, ctx):
        envelope = await query("eventRecord", ctx, filters={"code": "E4204981"})
        assert envelope["note"] is None and "redacted" not in envelope

    async def test_the_note_is_about_text_only_when_text_was_scrubbed(self, ctx):
        """A credential's value is withheld outright; no best-effort claim is made about it."""
        envelope = await query("vnsCCred", ctx, fetch_all=True)
        assert "best-effort" not in envelope["note"]

    async def test_get_by_dn_says_it_too(self, ctx):
        obj = await get_by_dn("subj-[uni/userext/user-bob]/evt-2", ctx)
        assert obj["changeSet"] == f"name:bob, otpkey:{REDACTED}, userdom:all"
        assert obj["_redacted"] == 1 and "best-effort" in obj["_note"]

    async def test_a_group_key_cannot_carry_a_secret_out_of_the_text(self, ctx):
        answer = await count("eventRecord", ctx, group_by="descr")
        assert f"ActiveUserSession {REDACTED} created" in answer["groups"]
        assert TOKEN not in json.dumps(answer) and answer["redacted"] == 1

    async def test_the_other_attributes_of_these_records_still_filter(self, ctx):
        assert (await query("eventRecord", ctx, filters={"code": "E4204979"}))["returned"] == 1
        expression = 'wcard(eventRecord.affected,"user-bob")'
        await count("eventRecord", ctx, filter_expr=expression)
        assert _backend(ctx).calls[-1]["filter_expr"] == expression, "a plain wcard was refused"


@pytest.mark.asyncio
async def test_duplicate_rows_are_each_redacted_and_each_counted(catalogue_index):
    """An abstract-class query can return the same object twice; none escapes, none is dropped."""
    row = {"snmpCommunityP": {"attributes": {"dn": COMMUNITY_DN, "name": SECRET}}}
    ctx = make_ctx({"descriptions": catalogue_index, "backend": StubBackend([row, row])})

    envelope = await query("snmpCommunityP", ctx)

    assert envelope["returned"] == 2 and envelope["redacted"] == 4
    assert SECRET not in json.dumps(envelope)


# ── get_by_dn ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestGetByDn:
    async def test_a_credential_is_withheld_and_the_object_says_so(self, ctx):
        obj = await get_by_dn("uni/tn-a/lDevVip-D/cDev-C/cCred", ctx)

        assert obj["value"] == REDACTED and obj["name"] == "admin"
        assert obj["_redacted"] == 1 and "withheld" in obj["_note"]
        assert SECRET not in json.dumps(obj)

    async def test_a_secret_in_a_child_is_withheld(self, ctx):
        obj = await get_by_dn("uni/tn-OT/BD-servers", ctx, include_children=["vnsCCred"])
        cred = next(c for c in obj["_children"] if c["_class"] == "vnsCCred")
        assert cred["value"] == REDACTED and obj["_redacted"] == 1

    async def test_a_projected_secret_is_the_marker_and_a_dropped_one_is_not_announced(self, ctx):
        kept = await get_by_dn("uni/tn-a/lDevVip-D/cDev-C/cCred", ctx, fields=["name", "value"])
        dropped = await get_by_dn("uni/tn-a/lDevVip-D/cDev-C/cCred", ctx, fields=["name"])

        assert kept["value"] == REDACTED and kept["_redacted"] == 1
        assert "value" not in dropped and "_redacted" not in dropped and "_note" not in dropped

    async def test_an_unset_credential_is_withheld_too(self, ctx):
        obj = await get_by_dn("uni/tn-a/lDevVip-E/cDev-C/cCred", ctx)
        assert obj["value"] == REDACTED and obj["_redacted"] == 1

    async def test_an_ordinary_dn_that_does_not_exist_is_still_not_found(self, ctx):
        answer = await get_by_dn("uni/tn-OT/BD-nope", ctx)
        assert answer["found"] is False


# ── count ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestCount:
    async def test_a_group_key_that_would_be_a_dn_with_a_secret_in_it_is_cut(self, ctx):
        answer = await count("aaaModLR", ctx, group_by="affected")

        assert answer["groups"] == {
            f"uni/fabric/snmpp-default/community-{REDACTED}": 1,
            "uni/tn-OT/BD-servers": 1,
        }
        assert answer["redacted"] == 1 and answer["complete"] is True
        assert SECRET not in json.dumps(answer)

    async def test_a_group_key_that_quotes_a_secret_in_text_is_cut(self, ctx):
        answer = await count("aaaModLR", ctx, group_by="descr")
        assert f"Community {REDACTED} created" in answer["groups"]
        assert SECRET not in json.dumps(answer)

    async def test_grouping_by_tenant_is_unaffected_and_adds_no_key(self, ctx):
        answer = await count("vnsCCred", ctx, group_by="tenant")
        assert answer["groups"] == {"a": 2}
        assert "redacted" not in answer

    async def test_a_secret_held_by_the_objects_but_not_in_the_key_is_not_announced(self, ctx):
        """Withholding a password while grouping credentials by name changes nothing read."""
        answer = await count("vnsCCred", ctx, group_by="name")
        assert answer["groups"] == {"admin": 2} and "redacted" not in answer


# ── get_schema ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestSchema:
    async def test_a_secret_property_is_labelled_so_the_agent_knows_not_to_ask(self, ctx):
        schema = await get_schema("vnsCCred", ctx, properties_filter=["value", "name"])

        assert schema["property_details"]["value"]["secret"] is True
        assert "secret" not in schema["property_details"]["name"]
        assert "value" in schema["properties"], "the name must stay listed"

    async def test_a_class_named_by_its_secret_labels_dn_and_rn_too(self, ctx):
        schema = await get_schema("snmpCommunityP", ctx, properties_filter=["dn", "rn", "name"])
        assert all(schema["property_details"][p]["secret"] for p in ("dn", "rn", "name"))

    async def test_an_ordinary_schema_is_exactly_what_it_was(self, ctx):
        from niwashi_mcp.registry import catalog

        schema = await get_schema("fvBD", ctx, include_property_details=True)
        assert not any("secret" in d for d in schema["property_details"].values())
        expected = catalog.annotate_relations(
            catalog.load_schema("fvBD", include_property_details=True)
        )
        assert json.dumps(schema, sort_keys=True) == json.dumps(
            main._bound_property_options(
                main._bound_schema_lists(expected, 25), 100
            ),
            sort_keys=True,
        )


# ── nothing leaks, anywhere ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_tool_output_contains_a_secret_whatever_is_asked(ctx):
    """The sweep: every class the backend serves, every way of asking for it."""
    outputs: list[object] = []
    classes = sorted({next(iter(item)) for item in _IMDATA})
    children = ["faultInst", "vnsCCred", "fvSubnet"]

    for cls in classes:
        for kwargs in (
            {}, {"fetch_all": True}, {"config_only": True},
            {"include_children": children}, {"include_children": children, "fields": []},
            {"rsp_subtree_include": "audit-logs"},
        ):
            outputs.append(await query(cls, ctx, **kwargs))
        for group_by in ("dn", "name", "descr", "affected", "tenant", "code", "changeSet", "sessionId"):
            try:
                outputs.append(await count(cls, ctx, group_by=group_by))
            except (SecretAttributeError, main.UnknownAttributeError) as exc:
                outputs.append(str(exc))
    for item in _IMDATA:
        for body in item.values():
            dn = body["attributes"]["dn"]
            try:
                outputs.append(await get_by_dn(dn, ctx, include_children=children))
            except SecretDnError as exc:
                outputs.append(str(exc))

    dump = json.dumps(outputs) + _logged(ctx)
    for secret in (SECRET, TOKEN, OTP, SESSION_ID):
        assert secret not in dump, f"{secret!r} reached an agent or a log"


# ── the typed output schemas still hold ───────────────────────────────────────


@pytest.fixture
def served(monkeypatch, catalogue_index):
    @lifespan
    async def stub(server):
        yield {"descriptions": catalogue_index, "backend": StubBackend(_IMDATA)}

    monkeypatch.setattr(main.mcp, "_lifespan", stub)
    return main.mcp


async def _call(server, name: str, **arguments):
    async with Client(server) as client:
        return await client.call_tool(name, arguments, raise_on_error=False)


@pytest.mark.asyncio
class TestTheAdvertisedSchemasStillValidate:
    async def test_query_with_a_withheld_secret(self, served):
        result = await _call(served, "query", class_name="vnsCCred", fetch_all=True)

        assert not result.is_error, result.content
        assert result.structured_content["redacted"] == 2
        assert SECRET not in json.dumps(result.structured_content)

    async def test_query_with_nothing_withheld_has_no_extra_key(self, served):
        result = await _call(served, "query", class_name="fvTenant")

        assert not result.is_error, result.content
        assert "redacted" not in result.structured_content

    async def test_count_group_by_with_a_withheld_secret(self, served):
        result = await _call(served, "count", class_name="aaaModLR", group_by="affected")

        assert not result.is_error, result.content
        assert result.structured_content["redacted"] == 1

    async def test_get_by_dn_with_a_withheld_secret(self, served):
        result = await _call(served, "get_by_dn", dn="uni/tn-a/lDevVip-D/cDev-C/cCred")

        assert not result.is_error, result.content
        assert result.structured_content["_redacted"] == 1

    async def test_a_refusal_reaches_the_agent_as_a_tool_error_that_says_why(self, served):
        attribute = await _call(
            served, "query", class_name="vnsCCred", filters={"value": GUESS}
        )
        dn = await _call(served, "get_by_dn", dn=COMMUNITY_DN)

        for refusal in (attribute, dn):
            assert refusal.is_error
            text = refusal.content[0].text
            assert "secret" in text and GUESS not in text and SECRET not in text
