# Copyright (C) 2026 Khalid El-Ouiali — MONARK AIOPS srl
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
The redaction interface: what is secret, and that nothing of it survives.

Why this file exists
--------------------
The APIC returns some secrets in clear — an L4-L7 device credential
(`vnsCCred.value`), an SNMP trap community (`snmpTrapDest.secName`), the
community string that *is* an `snmpCommunityP`'s name — and writes them into its
own audit log (`aaaModLR.changeSet`). This server is the thing that hands an
APIC's answers to an LLM. These tests pin each kind of secret to the rule that
withholds it, over synthetic objects, so a rule that stops matching fails here
and not in a transcript.

Written against `niwashi_mcp.redact` and nothing of niwaki's: that module is the
interim form of niwaki 2.3's `niwaki.secrets`, and these tests are meant to
survive the switch. The tests that *do* read niwaki's tables are in
`test_redact_interim_policy.py`, which goes when the switch comes.

No secret in this file is a real one. Every "secret" is the sentinel `zzsecret`
(or a variation of it), so a failing assertion that prints an object prints
nothing worth hiding.
"""

from __future__ import annotations

import json

import pytest

from niwashi_mcp import redact
from niwashi_mcp.exceptions import SecretAttributeError, SecretDnError
from niwashi_mcp.registry import catalog

pytestmark = pytest.mark.catalog

R = redact.REDACTED
SECRET = "zzsecret"
# The shape of a session hash: base64, so `+` and `=`, and never a `/` in a DN.
TOKEN = "zzTok+enx0=="
COMMUNITY_DN = f"uni/fabric/snmpp-default/community-{SECRET}"
USER_DN = "uni/userext/user-bob"
CRED_DN = "uni/tn-a/lDevVip-D/cDev-C/cCred"
TRAP_DN = "uni/fabric/snmpgroup-g/trapdest-10.0.0.1-port-162"


# ── the marker and the vocabulary ─────────────────────────────────────────────


def test_the_marker_is_niwakis_so_the_sdk_and_this_server_agree():
    from niwaki.snapshot import REDACTED

    assert redact.REDACTED == REDACTED == "<redacted>"


class TestWhatIsSecret:
    @pytest.mark.parametrize(
        ("cls", "prop"),
        [
            ("vnsCCred", "value"),  # curated: echoed in clear
            ("snmpTrapDest", "secName"),  # curated: the trap community
            ("aaaUser", "pwd"),  # schema-flagged
            ("aaaUser", "otpkey"),  # curated
            ("snmpCommunityP", "name"),  # the naming property of a class named by it
            ("snmpCommSecP", "name"),  # ...niwaki 2.2's DN list lacks this one
            ("aaaActiveUserSession", "hashToken"),  # a session token
            ("aaaSessionLR", "sessionId"),  # a session token, in the session log
            ("aaaUserProfile", "otpkey"),  # measured live, not in niwaki 2.2
            ("pkiFabricSelfCAEp", "currCertReqPassphrase"),
            ("pkiFabricSelfCAEp", "oldCertReqPassphrase"),
        ],
    )
    def test_the_known_secrets(self, cls, prop):
        assert redact.is_secret(cls, prop)
        assert prop in redact.secret_props(cls)

    def test_an_ordinary_property_is_not(self):
        for cls, prop in (("fvBD", "name"), ("fvBD", "arpFlood"), ("vnsCCred", "name"),
                          ("snmpTrapDest", "host"), ("aaaModLR", "changeSet")):
            assert not redact.is_secret(cls, prop), f"{cls}.{prop}"

    def test_an_abstract_class_answers_for_its_concrete_children(self):
        """Fail-closed: a query on the parent returns the children's instances."""
        assert redact.is_secret("snmpACommunityP", "name")
        assert redact.is_secret("firmwareSource", "authPass")
        assert redact.has_secret_dn("snmpACommunityP")

    def test_the_curated_position_on_an_abstract_class_reaches_the_concrete_one_niwaki_misses(self):
        """niwaki lists `snmpACommunityP.name`, a class no response carries."""
        assert "name" in redact.secret_props("snmpCommSecP")
        assert redact.has_secret_dn("snmpCommSecP")

    def test_a_class_the_catalogue_does_not_know_is_judged_by_the_name(self):
        """Fail-closed on ambiguity: a newer fabric's `communityString` is not worth a guess."""
        assert redact.is_secret("futureThing", "communityString")
        assert redact.is_secret("futureThing", "password")
        assert not redact.is_secret("futureThing", "name")

    def test_who_is_named_by_a_secret(self):
        for cls in ("snmpCommunityP", "snmpCommSecP", "aaaActiveUserSession",
                    "aaaDeletedUserSession", "pkiSiteJwtPubKey"):
            assert redact.has_secret_dn(cls), cls
        for cls in ("fvBD", "vnsCCred", "snmpTrapDest", "aaaModLR"):
            assert not redact.has_secret_dn(cls), cls

    def test_free_text_is_changeset_on_exactly_the_thirteen_record_classes(self):
        """The audit, event, fault and session records — niwaki counts 13 as well."""
        carriers = {
            cls for (cls,) in catalog._query(
                "SELECT m.class_name FROM class_prop cp JOIN prop_sig p ON p.id = cp.sig_id "
                "JOIN mo m ON m.id = cp.class_id WHERE p.wire_name = 'changeSet'"
            )
        }
        assert len(carriers) == 13
        assert {"aaaModLR", "eventRecord", "aaaSessionLR", "faultRecord", "faultInst"} <= carriers
        for cls in carriers:
            assert redact.free_text_props(cls) == {"changeSet"}, cls
        assert redact.free_text_props("fvBD") == frozenset()

    def test_every_interim_position_is_a_real_class_and_property(self):
        """The supplement must not drift from the catalogue it is applied to."""
        for cls, prop in redact._INTERIM_POSITIONS:
            assert catalog.class_exists(cls), cls
            assert catalog.property_exists(cls, prop), (cls, prop)


# ── values ────────────────────────────────────────────────────────────────────


class TestValues:
    def test_a_curated_value_is_replaced_by_the_marker(self):
        out = redact.redact(
            "vnsCCred", {"dn": CRED_DN, "name": "admin", "value": SECRET, "modTs": "t"}
        )
        assert out == {"dn": CRED_DN, "name": "admin", "value": R, "modTs": "t"}

    def test_the_trap_community_that_the_apic_echoes_in_clear(self):
        out = redact.redact("snmpTrapDest", {"host": "10.0.0.1", "secName": SECRET})
        assert out == {"host": "10.0.0.1", "secName": R}

    def test_an_empty_secret_is_withheld_too_so_set_and_unset_cannot_be_told_apart(self):
        assert redact.redact("vnsCCred", {"name": "admin", "value": ""})["value"] == R
        assert redact.redact("aaaUser", {"otpkey": "", "pwd": ""}) == {"otpkey": R, "pwd": R}

    def test_an_absent_attribute_stays_absent(self):
        """`fields` or `config_only` removed it: there is nothing to withhold."""
        assert redact.redact("vnsCCred", {"name": "admin"}) == {"name": "admin"}

    def test_the_input_is_never_modified(self):
        attrs = {"name": "admin", "value": SECRET}
        redact.redact("vnsCCred", attrs)
        assert attrs == {"name": "admin", "value": SECRET}

    def test_redacting_twice_changes_nothing(self):
        once = redact.redact("vnsCCred", {"value": SECRET})
        assert redact.redact("vnsCCred", once) == once

    def test_an_ordinary_object_is_returned_equal(self):
        bd = {"dn": "uni/tn-OT/BD-servers", "name": "servers", "descr": "prod", "arpFlood": "no"}
        assert redact.redact("fvBD", bd) == bd

    @pytest.mark.parametrize("mode", ["heuristic", "keep"])
    def test_a_class_the_catalogue_does_not_know_is_not_a_crash(self, mode):
        attrs = {"dn": "uni/x", "name": "n", "communityString": SECRET}
        out = redact.redact("futureThing", attrs, on_unknown_class=mode)
        assert out["communityString"] == (R if mode == "heuristic" else SECRET)
        assert out["dn"] == "uni/x" and out["name"] == "n"

    def test_the_default_for_an_unknown_class_is_the_heuristic(self):
        assert redact.redact("futureThing", {"password": SECRET})["password"] == R

    def test_raise_names_the_class(self):
        with pytest.raises(KeyError, match="futureThing"):
            redact.redact("futureThing", {"x": "y"}, on_unknown_class="raise")

    def test_a_known_class_never_raises_in_any_mode(self):
        for mode in ("heuristic", "keep", "raise"):
            redact.redact("fvBD", {"name": "x"}, on_unknown_class=mode)

    def test_an_unknown_mode_is_refused(self):
        with pytest.raises(ValueError, match="on_unknown_class"):
            redact.redact("fvBD", {}, on_unknown_class="shrug")

    def test_the_dn_and_text_rules_apply_to_an_unknown_class_too(self):
        out = redact.redact("futureThing", {"dn": COMMUNITY_DN}, on_unknown_class="keep")
        assert out["dn"].endswith(f"community-{R}")


# ── DNs ───────────────────────────────────────────────────────────────────────


class TestDns:
    def test_a_community_is_cut_out_of_its_own_dn_and_name(self):
        out = redact.redact("snmpCommunityP", {"dn": COMMUNITY_DN, "name": SECRET, "descr": ""})
        assert out == {"dn": f"uni/fabric/snmpp-default/community-{R}", "name": R, "descr": ""}

    @pytest.mark.parametrize(
        ("cls", "prefix"),
        [
            ("aaaActiveUserSession", "actsession-"),
            ("aaaDeletedUserSession", "delsession-"),
            ("pkiSiteJwtPubKey", "sitejwtpubkey-"),
            ("snmpCommSecP", "community-"),
        ],
    )
    def test_each_class_named_by_a_secret_loses_it_from_its_dn(self, cls, prefix):
        out = redact.redact(cls, {"dn": f"uni/x/{prefix}{SECRET}"})
        assert out["dn"] == f"uni/x/{prefix}{R}"

    def test_a_token_with_the_characters_a_base64_hash_has(self):
        out = redact.redact("aaaActiveUserSession", {"dn": f"uni/ac/actsession-{TOKEN}"})
        assert out["dn"] == f"uni/ac/actsession-{R}"

    def test_a_relative_name_alone_is_redacted(self):
        assert redact.redact("snmpCommunityP", {"rn": f"community-{SECRET}"})["rn"] == f"community-{R}"

    def test_a_descendants_dn_passes_through_the_secret_too(self):
        out = redact.redact("faultInst", {"dn": f"{COMMUNITY_DN}/fault-F1234"})
        assert out["dn"] == f"uni/fabric/snmpp-default/community-{R}/fault-F1234"

    def test_every_string_value_goes_through_the_dn_rule(self):
        out = redact.redact("fvRtFoo", {"dn": "uni/tn-a/rtfoo", "tDn": COMMUNITY_DN, "x": COMMUNITY_DN})
        assert SECRET not in json.dumps(out)

    def test_a_dn_embedded_in_brackets_is_cut_and_what_follows_is_kept(self):
        out = redact.redact(
            "aaaModLR", {"dn": f"subj-[{COMMUNITY_DN}]/mod-123", "affected": COMMUNITY_DN}
        )
        assert out["dn"] == f"subj-[uni/fabric/snmpp-default/community-{R}]/mod-123"

    def test_a_word_that_merely_contains_the_prefix_is_not_a_segment(self):
        attrs = {"dn": "uni/tn-a/BD-mycommunity-x", "descr": "a community-y"}
        assert redact.redact("fvBD", attrs) == attrs


class TestRedactDn:
    def test_cuts_a_secret_segment(self):
        assert redact.redact_dn(COMMUNITY_DN) == f"uni/fabric/snmpp-default/community-{R}"

    def test_cuts_inside_brackets_with_no_class_in_sight(self):
        dn = f"subj-[uni/ac/actsession-{TOKEN}]/mod-9"
        assert redact.redact_dn(dn) == f"subj-[uni/ac/actsession-{R}]/mod-9"

    def test_cuts_every_segment_not_just_the_first(self):
        dn = f"x/community-{SECRET}/y/actsession-{TOKEN}"
        assert redact.redact_dn(dn) == f"x/community-{R}/y/actsession-{R}"

    def test_an_ordinary_dn_is_unchanged(self):
        dn = "uni/tn-OT/BD-servers/subnet-[10.0.1.1/24]"
        assert redact.redact_dn(dn) == dn

    def test_is_idempotent(self):
        once = redact.redact_dn(COMMUNITY_DN)
        assert redact.redact_dn(once) == once

    def test_dn_has_secret_says_so_until_it_is_cut(self):
        assert redact.dn_has_secret(COMMUNITY_DN)
        assert redact.dn_has_secret(f"subj-[{COMMUNITY_DN}]/mod-1")
        assert not redact.dn_has_secret(redact.redact_dn(COMMUNITY_DN))
        assert not redact.dn_has_secret("uni/tn-OT/BD-servers")


# ── imdata shape ──────────────────────────────────────────────────────────────


class TestImdataShape:
    def test_an_item_is_redacted_recursively(self):
        item = {
            "snmpCommunityP": {
                "attributes": {"dn": COMMUNITY_DN, "name": SECRET},
                "children": [
                    {"faultInst": {"attributes": {"dn": f"{COMMUNITY_DN}/fault-F1", "code": "F1"}}},
                    {"vnsCCred": {"attributes": {"dn": CRED_DN, "value": SECRET}}},
                ],
            }
        }
        out = redact.redact_object(item)
        assert SECRET not in json.dumps(out)
        assert out["snmpCommunityP"]["attributes"]["name"] == R
        assert out["snmpCommunityP"]["children"][1]["vnsCCred"]["attributes"]["value"] == R
        assert item["snmpCommunityP"]["attributes"]["name"] == SECRET, "the input was modified"

    def test_an_item_without_children_has_none_added(self):
        out = redact.redact_object({"fvBD": {"attributes": {"name": "x"}}})
        assert out == {"fvBD": {"attributes": {"name": "x"}}}


# ── the flat shape the tools hold ─────────────────────────────────────────────


class TestFlatResults:
    def test_withheld_counts_attributes_changed_and_children_are_included(self):
        parent = {
            "_class": "fvBD",
            "dn": "uni/tn-a/BD-b",
            "_children": [
                {"_class": "vnsCCred", "dn": CRED_DN, "value": SECRET},
                {"_class": "fvSubnet", "dn": "uni/tn-a/BD-b/subnet-[10.0.0.1/24]", "ip": "10.0.0.1/24"},
                {"_class": "faultInst", "dn": f"{COMMUNITY_DN}/fault-F1"},
            ],
        }
        done = redact.redact_one(parent)
        assert done.withheld == 2 and SECRET not in json.dumps(done.value)
        assert done.value["_children"][1] is parent["_children"][1], "an untouched child was copied"
        assert parent["_children"][0]["value"] == SECRET, "the input was modified"

    def test_an_ordinary_result_set_comes_back_as_the_same_objects(self):
        objects = [{"_class": "fvBD", "dn": f"uni/tn-a/BD-{i}", "name": str(i)} for i in range(5)]
        done = redact.redact_results(objects)
        assert done.withheld == 0 and not done.scrubbed_text
        assert all(a is b for a, b in zip(done.value, objects, strict=True))

    def test_a_parent_whose_children_are_all_ordinary_is_the_same_object(self):
        parent = {"_class": "fvBD", "dn": "uni/tn-a/BD-b", "_children": [{"_class": "fvRsCtx"}]}
        assert redact.redact_one(parent).value is parent

    def test_free_text_is_reported_as_scrubbed_not_withheld(self):
        audit = {"_class": "aaaModLR", "affected": USER_DN, "changeSet": f"otpkey:{SECRET}"}
        done = redact.redact_one(audit)
        assert done.withheld == 1 and done.scrubbed_text

    def test_duplicates_are_each_redacted_and_each_counted(self):
        """An abstract-class query can return the same object twice."""
        row = {"_class": "snmpCommunityP", "dn": COMMUNITY_DN, "name": SECRET}
        done = redact.redact_results([dict(row), dict(row)])
        assert [o["name"] for o in done.value] == [R, R] and done.withheld == 4

    def test_two_secrets_that_redact_to_the_same_dn_stay_two_objects(self):
        rows = [{"_class": "snmpCommunityP", "dn": f"uni/fabric/snmpp-d/community-{s}", "name": s}
                for s in (SECRET, "zzother")]
        done = redact.redact_results(rows)
        assert len(done.value) == 2 and done.value[0]["dn"] == done.value[1]["dn"]


# ── free text ─────────────────────────────────────────────────────────────────


class TestChangeSetCreationFormat:
    """`prop:value, prop:value` — 12,437 of the lab's 12,493 changeSets."""

    def test_the_secret_pair_loses_its_value_and_the_others_do_not(self):
        text = redact.redact_text(
            f"name:bob, otpkey:{SECRET}, userdom:all", affected=USER_DN
        )
        assert text == f"name:bob, otpkey:{R}, userdom:all"

    def test_a_pair_at_the_start_and_at_the_end(self):
        assert redact.redact_text(f"otpkey:{SECRET}, name:bob", affected=USER_DN) == f"otpkey:{R}, name:bob"
        assert redact.redact_text(f"name:bob, otpkey:{SECRET}", affected=USER_DN) == f"name:bob, otpkey:{R}"

    def test_a_value_with_commas_and_spaces_goes_whole(self):
        """The seam is the next property the class declares, not the next comma."""
        secret = "pa ss, w0rd:x, y"
        text = redact.redact_text(f"name:bob, otpkey:{secret}, userdom:all", affected=USER_DN)
        assert "w0rd" not in text and "pa ss" not in text
        assert text == f"name:bob, otpkey:{R}, userdom:all"

    def test_a_list_value_in_a_pair_that_is_not_secret_is_untouched(self):
        text = f"allowedVlans:1,253,272, otpkey:{SECRET}"
        assert redact.redact_text(text, affected=USER_DN) == f"allowedVlans:1,253,272, otpkey:{R}"

    def test_a_double_colon_value_is_a_value(self):
        assert redact.redact_text(f"otpkey::{SECRET}:, name:bob", affected=USER_DN) == f"otpkey:{R}, name:bob"

    def test_several_secret_pairs_in_one_text(self):
        text = redact.redact_text(
            f"secName:{SECRET}, host:10.0.0.1, port:162", affected=TRAP_DN
        )
        assert text == f"secName:{R}, host:10.0.0.1, port:162"

    def test_the_name_on_a_class_where_it_is_not_secret_is_left_alone(self):
        """`key`, `value` and `name` are secret somewhere; here they are a tag."""
        text = "key:env, value:prod, userdom:all"
        assert redact.redact_text(text, affected="uni/tn-a/tag-t") == text
        assert redact.redact_text("name:servers", affected="uni/tn-OT/BD-servers") == "name:servers"

    def test_a_secret_name_that_is_a_substring_of_another_is_not_a_hit(self):
        assert redact.redact_text("fooalue:x", affected=CRED_DN) == "fooalue:x"


class TestChangeSetModificationFormat:
    """`prop (Old: x, New: y)` — 38 of the lab's 12,493."""

    def test_both_sides_of_a_secret_pair_go(self):
        text = redact.redact_text(
            f"name (Old: N/A, New: {SECRET}), descr (Old: , New: )", affected=COMMUNITY_DN
        )
        assert text == f"name (Old: {R}, New: {R}), descr (Old: , New: )"

    def test_a_value_with_nested_parentheses_goes_whole(self):
        text = redact.redact_text(f"value (Old: {SECRET} (x), New: a, b (c)), name (Old: , New: d)", affected=CRED_DN)
        assert SECRET not in text and "(c)" not in text
        assert text == f"value (Old: {R}, New: {R}), name (Old: , New: d)"

    def test_a_group_the_apic_truncated_goes_to_the_end(self):
        text = redact.redact_text(f"value (Old: {SECRET}", affected=CRED_DN)
        assert SECRET not in text and text.startswith("value (Old: ")

    def test_only_one_side_present(self):
        assert redact.redact_text(f"value (New: {SECRET})", affected=CRED_DN) == f"value (New: {R})"

    def test_the_two_formats_in_one_text(self):
        text = redact.redact_text(
            f"name:x, value (Old: {SECRET}, New: y), secName:{SECRET}", affected=CRED_DN
        )
        assert SECRET not in text


class TestWhichClassTheTextIsAbout:
    def test_the_affected_class_wins_when_given(self):
        text = f"value:{SECRET}"
        assert SECRET not in redact.redact_text(text, affected_class="vnsCCred")
        assert redact.redact_text("value:x", affected_class="tagTag") == "value:x"

    def test_the_class_is_read_from_the_last_rn_of_affected(self):
        assert SECRET not in redact.redact_text(f"value:{SECRET}", affected=CRED_DN)
        assert redact.redact_text(f"value:{SECRET}", affected="uni/tn-a/tag-t") == f"value:{SECRET}"

    def test_a_rn_two_classes_share_is_both_of_them(self):
        """`user-{name}` is aaaUser's and the SNMPv3 users': the cautious reading is the union."""
        assert SECRET not in redact.redact_text(f"otpkey:{SECRET}", affected=USER_DN)

    def test_a_bracketed_rn_is_split_where_the_brackets_end(self):
        dn = "uni/tn-a/BD-b/subnet-[10.0.0.1/24]"
        assert redact._last_rn(dn) == "subnet-[10.0.0.1/24]"

    @pytest.mark.parametrize("affected", [None, "", "uni/zzNoSuchObject-x", "garbage"])
    def test_with_no_class_every_secret_name_is_masked_and_nothing_raises(self, affected):
        """Over-masks: that is the price of not knowing, and it never raises."""
        out = redact.redact_text(f"name:public, value:{SECRET}, host:h", affected=affected)
        assert out == f"name:{R}, value:{R}, host:h"

    def test_an_affected_class_the_catalogue_does_not_know_is_the_same_case(self):
        assert redact.redact_text(f"value:{SECRET}", affected_class="futureThing") == f"value:{R}"

    def test_a_secret_dn_as_affected_gives_the_class_that_is_named_by_it(self):
        text = redact.redact_text(f"name:{SECRET}, descr:", affected=COMMUNITY_DN)
        assert text == f"name:{R}, descr:"


class TestWhatCannotBeParsed:
    def test_text_that_names_a_secret_property_outside_a_pair_is_replaced_whole(self):
        assert redact.redact_text(f"set otpkey:{SECRET} later", affected=USER_DN) == R
        assert redact.redact_text(f"name:bob; otpkey:{SECRET}", affected=USER_DN) == R

    def test_prose_that_names_no_secret_is_left_alone(self):
        text = "Port is down. Reason: disabled"
        assert redact.redact_text(text, affected=USER_DN) == text

    def test_an_already_scrubbed_pair_is_not_unparseable_text(self):
        scrubbed = f"otpkey:{R}, name:bob"
        assert redact.redact_text(scrubbed, affected=USER_DN) == scrubbed

    def test_a_secret_still_in_the_text_after_scrubbing_replaces_the_whole_field(self):
        """Embedded in a bigger token, so no word-pass reaches it: fail closed."""
        text = redact.redact_text(
            f"name:{SECRET}, descr:see tmp{SECRET}copy", affected=COMMUNITY_DN
        )
        assert text == R

    def test_the_empty_text_is_the_empty_text(self):
        assert redact.redact_text("", affected=USER_DN) == ""

    def test_redacting_twice_changes_nothing(self):
        once = redact.redact_text(f"name:x, otpkey:{SECRET}", affected=USER_DN)
        assert redact.redact_text(once, affected=USER_DN) == once


class TestRecordsAreScrubbedAsAWhole:
    def test_a_community_created_audit_record(self):
        audit = {
            "dn": f"subj-[{COMMUNITY_DN}]/mod-9",
            "affected": COMMUNITY_DN,
            "descr": f"Community {SECRET} created From IP:10.0.0.1",
            "changeSet": f"name:{SECRET}, userdom::all:",
            "code": "E4207843",
        }
        out = redact.redact("aaaModLR", audit)
        assert SECRET not in json.dumps(out)
        assert out["descr"] == f"Community {R} created From IP:10.0.0.1"
        assert out["changeSet"] == f"name:{R}, userdom::all:"
        assert out["code"] == "E4207843"

    def test_a_credential_audit_record_in_both_formats(self):
        for change in (f"name:admin, value:{SECRET}", f"value (Old: {SECRET}, New: x)"):
            out = redact.redact("aaaModLR", {"affected": CRED_DN, "changeSet": change})
            assert SECRET not in json.dumps(out), change

    def test_an_event_record_of_a_session_loses_the_token_everywhere(self):
        event = {
            "dn": f"subj-[uni/ac/actsession-{TOKEN}]/evt-1",
            "affected": f"uni/ac/actsession-{TOKEN}",
            "descr": f"ActiveUserSession {TOKEN} created",
            "changeSet": f"hashToken:{TOKEN}, name:admin",
        }
        out = redact.redact("eventRecord", event)
        assert TOKEN not in json.dumps(out)
        assert out["descr"] == f"ActiveUserSession {R} created"

    def test_a_session_log_record(self):
        out = redact.redact("aaaSessionLR", {"sessionId": TOKEN, "dn": "subj-[uni/userext]/sess-7", "id": "7"})
        assert out == {"sessionId": R, "dn": "subj-[uni/userext]/sess-7", "id": "7"}

    @pytest.mark.parametrize("cls", ["aaaModLR", "eventRecord", "aaaSessionLR", "faultRecord", "faultInst",
                                     "aaaUserAction", "epRecord", "polDeploymentRecord"])
    def test_every_free_text_carrier_is_scrubbed_automatically(self, cls):
        out = redact.redact(cls, {"affected": USER_DN, "changeSet": f"otpkey:{SECRET}"})
        assert out["changeSet"] == f"otpkey:{R}"

    def test_a_record_with_nothing_to_scrub_is_unchanged(self):
        audit = {"dn": "subj-[uni/tn-OT/BD-servers]/mod-2", "affected": "uni/tn-OT/BD-servers",
                 "descr": "Bridge Domain servers created", "changeSet": "name:servers, userdom:all"}
        assert redact.redact("aaaModLR", audit) == audit

    def test_text_on_a_class_that_carries_none_is_not_parsed(self):
        attrs = {"affected": USER_DN, "changeSet": f"otpkey:{SECRET}"}
        assert redact.redact("fvBD", attrs) == attrs

    def test_a_secret_met_in_a_dn_is_removed_from_the_text_of_any_class(self):
        out = redact.redact("fvBD", {"dn": COMMUNITY_DN, "descr": f"for {SECRET}"})
        assert out["descr"] == f"for {R}"

    def test_the_text_pass_replaces_whole_words_only_and_never_a_dn(self):
        """A one-letter community must not eat a letter out of every word, nor a DN."""
        out = redact.redact(
            "aaaModLR",
            {
                "affected": "uni/fabric/snmpp-a/community-a",
                "descr": "a community named a was made; data is kept",
            },
        )
        assert out["descr"] == f"{R} community named {R} was made; data is kept"
        assert out["affected"] == f"uni/fabric/snmpp-a/community-{R}"

    def test_a_secret_that_is_also_a_policy_name_is_not_cut_out_of_a_dn(self):
        """The lab's trap community is also its policy's name; a DN is not text."""
        out = redact.redact(
            "aaaModLR",
            {"affected": "uni/fabric/snmpgroup-niwaki/trapdest-10.0.0.1-port-162",
             "changeSet": "secName:niwaki, host:10.0.0.1"},
        )
        assert out["affected"] == "uni/fabric/snmpgroup-niwaki/trapdest-10.0.0.1-port-162"
        assert out["changeSet"] == f"secName:{R}, host:10.0.0.1"


# ── refusing the requests that would reveal one ───────────────────────────────


class TestAttributeOracles:
    @pytest.mark.parametrize(
        ("cls", "attribute"),
        [("vnsCCred", "value"), ("snmpTrapDest", "secName"), ("aaaUser", "pwd"),
         ("snmpCommunityP", "name"), ("aaaActiveUserSession", "hashToken"),
         ("aaaSessionLR", "sessionId")],
    )
    def test_an_equality_filter_on_a_secret_is_refused(self, cls, attribute):
        with pytest.raises(SecretAttributeError) as caught:
            redact.check_request(cls, filters={attribute: "zzguess"})
        assert caught.value.attribute == attribute and caught.value.where == "filters"
        assert "zzguess" not in str(caught.value), "the refusal echoed what it was sent"

    def test_order_by_a_secret_is_refused_qualified_or_bare(self):
        for order_by in ("vnsCCred.value", "vnsCCred.value|desc", "value", "fvBD.name,vnsCCred.value"):
            with pytest.raises(SecretAttributeError):
                redact.check_request("vnsCCred", order_by=order_by)

    @pytest.mark.parametrize(
        "expression",
        [
            'eq(vnsCCred.value,"guess")',
            'wcard(vnsCCred.value,"gu")',
            'ne(vnsCCred.value,"")',
            'and(eq(vnsCCred.name,"admin"),eq(vnsCCred.value,"guess"))',
            'not(or(eq(fvBD.name,"x"),eq(vnsCCred.value,"guess")))',
            'eq( vnsCCred.value , "guess" )',
            'EQ(vnsCCred.value,"guess")',
            'eq((vnsCCred.value),"guess")',
        ],
    )
    def test_a_filter_expression_cannot_hide_its_operand(self, expression):
        """However the predicate is written — spacing, nesting, case, parentheses."""
        with pytest.raises(SecretAttributeError) as caught:
            redact.check_request("vnsCCred", filter_expr=expression)
        assert caught.value.where == "filter_expr"

    def test_an_operand_on_another_class_is_checked_against_that_class(self):
        with pytest.raises(SecretAttributeError):
            redact.check_request("fvBD", filter_expr='eq(vnsCCred.value,"guess")')

    def test_a_quoted_value_that_looks_like_a_reference_is_not_an_operand(self):
        redact.check_request("fvBD", filter_expr='eq(fvBD.descr,"vnsCCred.value")')

    def test_group_by_a_secret_is_refused(self):
        with pytest.raises(SecretAttributeError) as caught:
            redact.check_request("snmpTrapDest", group_by="secName")
        assert caught.value.where == "group_by"

    def test_the_whole_abstract_family_is_covered(self):
        for cls, attribute in (("snmpACommunityP", "name"), ("firmwareSource", "authPass")):
            with pytest.raises(SecretAttributeError):
                redact.check_request(cls, filters={attribute: "guess"})

    def test_dn_and_rn_of_a_class_named_by_a_secret_cannot_be_probed(self):
        """`wcard(snmpCommunityP.dn, "ubli")` would confirm a substring of the community."""
        for cls in ("snmpCommunityP", "snmpCommSecP", "snmpACommunityP", "aaaActiveUserSession"):
            for attribute in ("dn", "rn"):
                with pytest.raises(SecretAttributeError):
                    redact.check_request(cls, filter_expr=f'wcard({cls}.{attribute},"ubli")')
        with pytest.raises(SecretAttributeError):
            redact.check_request("snmpCommunityP", group_by="dn")

    @pytest.mark.parametrize("cls", ["aaaModLR", "eventRecord", "faultRecord", "aaaSessionLR"])
    def test_free_text_cannot_be_probed_either(self, cls):
        """A wcard on a changeSet confirms what was scrubbed out of it."""
        for kwargs in (
            {"filters": {"changeSet": "x"}},
            {"filter_expr": f'wcard({cls}.changeSet,"otpkey")'},
            {"order_by": f"{cls}.changeSet|desc"},
            {"group_by": "changeSet"},
        ):
            with pytest.raises(SecretAttributeError) as caught:
                redact.check_request(cls, **kwargs)
            assert caught.value.free_text and "best-effort" in str(caught.value)

    def test_the_other_attributes_of_those_records_stay_usable(self):
        redact.check_request(
            "aaaModLR",
            filters={"user": "admin", "code": "E4207843"},
            order_by="aaaModLR.created|desc",
            filter_expr='wcard(aaaModLR.affected,"uni/tn-OT")',
            group_by="user",
        )

    def test_a_property_of_a_class_the_catalogue_does_not_know_is_judged_by_its_name(self):
        with pytest.raises(SecretAttributeError):
            redact.check_request("fvBD", filter_expr='eq(futureThing.password,"x")')
        redact.check_request("fvBD", filter_expr='eq(futureThing.color,"x")')

    def test_ordinary_requests_are_untouched(self):
        redact.check_request(
            "fvBD",
            filters={"name": "servers", "dn": "uni/tn-OT/BD-servers"},
            order_by="fvBD.name|asc",
            filter_expr='and(wcard(fvBD.dn,"uni/tn-OT"),eq(fvBD.arpFlood,"no"))',
            scope_dn="uni/tn-OT",
            group_by="tenant",
        )

    def test_dn_and_name_on_a_class_that_is_not_secret_stay_filterable(self):
        redact.check_request("faultInst", filter_expr='wcard(faultInst.dn,"uni/tn-OT")')
        redact.check_request("vnsCCred", filters={"name": "admin"})
        redact.check_request("snmpTrapDest", group_by="host")


class TestDnOracles:
    @pytest.mark.parametrize("dn", [COMMUNITY_DN, f"{COMMUNITY_DN}/fault-F1",
                                    f"subj-[{COMMUNITY_DN}]/mod-1", f"uni/x/actsession-{SECRET}",
                                    f"uni/x/sitejwtpubkey-{SECRET}", f"uni/x/delsession-{SECRET}"])
    def test_a_dn_naming_a_secret_is_refused_on_its_shape(self, dn):
        with pytest.raises(SecretDnError) as caught:
            redact.check_dn(dn)
        assert SECRET not in str(caught.value), "the refusal echoed what it was sent"

    def test_the_marker_in_a_dn_cannot_be_used_to_address_anything(self):
        with pytest.raises(SecretDnError):
            redact.check_dn(f"uni/fabric/snmpp-default/community-{R}")

    def test_the_answer_is_the_same_whether_or_not_the_object_exists(self):
        """The property that matters: no input pair differs only in existence."""
        errors = set()
        for guess in ("public", "private", SECRET, "x"):
            with pytest.raises(SecretDnError) as caught:
                redact.check_dn(f"uni/fabric/snmpp-default/community-{guess}")
            errors.add(str(caught.value))
        assert len(errors) == 1

    def test_scope_a_filter_value_and_a_quoted_literal_are_all_checked(self):
        for kwargs in (
            {"scope_dn": COMMUNITY_DN},
            {"filters": {"dn": COMMUNITY_DN}},
            {"filter_expr": f'eq(snmpCommunityP.descr,"community-{SECRET}")'},
        ):
            with pytest.raises(SecretDnError):
                redact.check_request("fvBD", **kwargs)

    def test_a_case_variant_does_not_slip_past(self):
        with pytest.raises(SecretDnError):
            redact.check_dn("uni/fabric/snmpp-default/COMMUNITY-public")

    def test_the_bare_prefix_and_ordinary_dns_are_fine(self):
        for dn in ("uni/fabric/snmpp-default", "uni/tn-OT/BD-servers", "uni/fabric/snmpp-default/community-"):
            redact.check_dn(dn)


# ── get_schema labels ─────────────────────────────────────────────────────────


class TestSchemaLabels:
    def test_a_secret_property_is_labelled_and_an_ordinary_one_is_not(self):
        schema = catalog.load_schema("vnsCCred", properties_filter=["value", "name"])
        out = redact.mark_secret_properties(schema, "vnsCCred")
        assert out["property_details"]["value"]["secret"] is True
        assert "secret" not in out["property_details"]["name"]

    def test_the_names_stay_listed(self):
        schema = redact.mark_secret_properties(catalog.load_schema("vnsCCred"), "vnsCCred")
        assert "value" in schema["properties"]

    def test_free_text_is_labelled_scrubbed_not_secret(self):
        schema = catalog.load_schema("aaaModLR", properties_filter=["changeSet", "descr"])
        details = redact.mark_secret_properties(schema, "aaaModLR")["property_details"]
        assert details["changeSet"] == {**details["changeSet"], "scrubbed": True}
        assert "secret" not in details["changeSet"] and "scrubbed" not in details["descr"]

    def test_a_schema_without_details_is_left_alone(self):
        schema = catalog.load_schema("vnsCCred")
        assert redact.mark_secret_properties(dict(schema), "vnsCCred") == schema

    def test_dn_is_labelled_on_a_class_named_by_a_secret(self):
        schema = catalog.load_schema("snmpCommunityP", properties_filter=["dn", "name", "descr"])
        details = redact.mark_secret_properties(schema, "snmpCommunityP")["property_details"]
        assert details["dn"]["secret"] and details["name"]["secret"]
        assert "secret" not in details["descr"]

    def test_an_ordinary_class_is_byte_identical(self):
        before = catalog.load_schema("fvBD", include_property_details=True)
        after = redact.mark_secret_properties(
            catalog.load_schema("fvBD", include_property_details=True), "fvBD"
        )
        assert json.dumps(after) == json.dumps(before)
