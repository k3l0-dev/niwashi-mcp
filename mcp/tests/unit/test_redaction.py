# Copyright (C) 2026 Khalid El-Ouiali — MONARK AIOPS srl
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""The redaction policy: what is secret, and that nothing of it survives.

Why this file exists
--------------------
The APIC returns some secrets in clear — an L4-L7 device credential
(`vnsCCred.value`), an SNMP trap community (`snmpTrapDest.secName`), the
community string that *is* an `snmpCommunityP`'s name — and this server is the
thing that hands an APIC's answers to an LLM. These tests pin each kind of
secret to the rule that withholds it, over synthetic objects, so a policy table
that moves or a rule that stops matching fails here and not in a transcript.

No secret in this file is a real one. Every "secret" is the sentinel `zzsecret`
(or a variation of it), so a failing assertion that prints an object prints
nothing worth hiding.
"""

from __future__ import annotations

import json

import pytest
from niwaki import _secrets
from niwaki.snapshot import REDACTED

from niwashi_mcp.exceptions import (
    NiwashiMcpError,
    RedactionPolicyError,
    RegistryError,
    SecretAttributeError,
    SecretDnError,
)
from niwashi_mcp.registry import catalog, redaction

pytestmark = pytest.mark.catalog

SECRET = "zzsecret"
COMMUNITY_DN = f"uni/fabric/snmpp-default/community-{SECRET}"


@pytest.fixture
def fresh_policy():
    """Rebuild the policy around a test that damages niwaki's tables, then restore it."""
    redaction._policy.cache_clear()
    redaction.secret_attributes.cache_clear()
    yield
    redaction._policy.cache_clear()
    redaction.secret_attributes.cache_clear()


def _redact(obj: dict) -> tuple[dict, int]:
    return redaction.redact_object(obj)


# ── the policy is niwaki's, not a copy ────────────────────────────────────────


class TestThePolicyIsNiwakis:
    def test_the_marker_is_niwakis(self):
        assert redaction.REDACTED is REDACTED

    def test_every_schema_flagged_property_is_withheld(self):
        """The mechanical layer: 169 (class, property) pairs, inherited ones included."""
        flagged = catalog.secure_properties()
        assert sum(map(len, flagged.values())) >= 100, "the flag decoded to almost nothing"
        for cls, props in flagged.items():
            for prop in props:
                out, count = _redact({"_class": cls, prop: SECRET})
                assert out[prop] == REDACTED and count == 1, f"{cls}.{prop} leaks"

    def test_the_schema_flag_is_exactly_niwakis_own_reading_of_it(self):
        """Two independent decoders of one bit must agree on every pair.

        `secure_properties()` is this server's SQL; `_secure_flagged()` is niwaki's.
        Were the flag's bit moved, one of them would follow and the other not.
        """
        ours = {(c, p) for c, props in catalog.secure_properties().items() for p in props}
        assert ours == set(_secrets._secure_flagged())

    def test_every_curated_value_position_is_withheld(self):
        """The 25 positions the flag forgot, `vnsCCred.value` among them."""
        assert ("vnsCCred", "value") in _secrets.SECRET_VALUE_POSITIONS
        for cls, prop in _secrets.SECRET_VALUE_POSITIONS:
            out, count = _redact({"_class": cls, prop: SECRET})
            assert out[prop] == REDACTED and count == 1, f"{cls}.{prop} leaks"

    def test_every_secret_naming_property_is_withheld(self):
        for cls, prop in _secrets.SECRET_DN_POSITIONS:
            out, _ = _redact({"_class": cls, prop: SECRET})
            assert out[prop] == REDACTED, f"{cls}.{prop} leaks"

    def test_a_curated_position_on_an_abstract_class_reaches_its_concrete_children(self):
        """niwaki lists `snmpACommunityP.name`; the APIC returns the children.

        Read literally the entry covers neither concrete class — it names a class
        no response ever carries — and `snmpCommSecP` is named by the community
        string exactly as `snmpCommunityP` is.
        """
        for cls in ("snmpCommunityP", "snmpCommSecP"):
            out, count = _redact({"_class": cls, "name": SECRET})
            assert out["name"] == REDACTED and count == 1, cls
        for cls in _concrete_subclasses("firmwareSource"):
            out, _ = _redact({"_class": cls, "authPass": SECRET})
            assert out["authPass"] == REDACTED, cls

    def test_the_class_set_named_by_a_secret_covers_the_one_niwaki_misses(self):
        policy = redaction._policy()
        assert _secrets.secret_dn_classes() <= policy.dn_classes
        assert "snmpCommSecP" in policy.dn_classes - _secrets.secret_dn_classes()

    def test_no_class_anywhere_is_named_by_a_secret_it_does_not_know_about(self):
        """The drift net: a future niwaki that curates a new naming secret.

        Walks the whole catalogue — every class's naming properties against the
        policy — so a class whose DN carries a secret fails here rather than
        publishing it. The fix is then to teach the policy, not to loosen this.
        """
        policy = redaction._policy()
        escaped = []
        for cls, identified_by in catalog._query("SELECT class_name, identified_by FROM mo"):
            for prop in json.loads(identified_by) if isinstance(identified_by, str) else ():
                if prop in policy.values.get(cls, ()) and cls not in policy.dn_classes:
                    escaped.append((cls, prop))
        assert not escaped, f"named by a secret but not treated as such: {escaped}"

    def test_each_secret_rn_prefix_is_unique_to_the_classes_named_by_a_secret(self):
        """A prefix another class also uses would redact that class's DNs by mistake."""
        policy = redaction._policy()
        for prefix in policy.prefixes:
            users = {
                cls
                for cls, rn in catalog._query("SELECT class_name, rn_format FROM mo")
                if prefix in (rn or "")
            }
            assert users <= policy.dn_classes, (prefix, users - policy.dn_classes)


def _concrete_subclasses(cls: str) -> list[str]:
    return redaction._concrete_subclasses(cls)


# ── values ────────────────────────────────────────────────────────────────────


class TestValues:
    def test_a_curated_value_is_replaced_by_the_marker(self):
        obj = {"_class": "vnsCCred", "dn": "uni/tn-a/lDevVip-b/cCred-admin", "name": "admin",
               "value": SECRET}
        out, count = _redact(obj)
        assert out["value"] == REDACTED and count == 1
        assert out["name"] == "admin" and out["dn"] == obj["dn"]

    def test_the_trap_community_that_the_apic_echoes_in_clear(self):
        out, count = _redact({"_class": "snmpTrapDest", "host": "10.0.0.1", "secName": SECRET})
        assert out["secName"] == REDACTED and out["host"] == "10.0.0.1" and count == 1

    def test_the_input_is_never_modified(self):
        obj = {"_class": "vnsCCred", "value": SECRET}
        _redact(obj)
        assert obj == {"_class": "vnsCCred", "value": SECRET}

    def test_an_empty_secret_stays_empty_and_is_not_counted(self):
        """An unset credential is posture, not a secret — and "is it set?" stays answerable."""
        obj = {"_class": "vnsCCred", "name": "admin", "value": ""}
        out, count = _redact(obj)
        assert out is obj and count == 0

    def test_redacting_twice_changes_nothing_and_counts_nothing(self):
        once, first = _redact({"_class": "vnsCCred", "value": SECRET})
        twice, second = _redact(once)
        assert twice is once and first == 1 and second == 0

    def test_an_ordinary_object_comes_back_as_the_same_object(self):
        """The common case allocates nothing and is byte for byte what the backend sent."""
        bd = {"_class": "fvBD", "dn": "uni/tn-OT/BD-servers", "name": "servers",
              "descr": "prod servers", "arpFlood": "no"}
        out, count = _redact(bd)
        assert out is bd and count == 0

    def test_a_class_the_catalogue_does_not_know_is_not_a_crash(self):
        """A fabric newer than the catalogue: nothing known, nothing invented."""
        obj = {"_class": "futureThing", "dn": "uni/x", "value": SECRET}
        out, count = _redact(obj)
        assert out is obj and count == 0


# ── DNs ───────────────────────────────────────────────────────────────────────


class TestDns:
    def test_a_community_is_cut_out_of_its_own_dn_and_name(self):
        out, count = _redact(
            {"_class": "snmpCommunityP", "dn": COMMUNITY_DN, "name": SECRET, "descr": ""}
        )
        assert out["dn"] == "uni/fabric/snmpp-default/community-" + REDACTED
        assert out["name"] == REDACTED
        assert SECRET not in json.dumps(out) and count == 2

    @pytest.mark.parametrize(
        ("cls", "prefix"),
        [
            ("aaaActiveUserSession", "actsession-"),
            ("aaaDeletedUserSession", "delsession-"),
            ("pkiSiteJwtPubKey", "sitejwtpubkey-"),
        ],
    )
    def test_each_class_named_by_a_secret_loses_it_from_its_dn(self, cls, prefix):
        out, _ = _redact({"_class": cls, "dn": f"uni/x/{prefix}{SECRET}"})
        assert out["dn"] == f"uni/x/{prefix}{REDACTED}"

    def test_a_relative_name_alone_is_redacted(self):
        out, _ = _redact({"_class": "snmpCommunityP", "rn": f"community-{SECRET}"})
        assert out["rn"] == f"community-{REDACTED}"

    def test_a_descendants_dn_passes_through_the_secret_too(self):
        out, count = _redact({"_class": "faultInst", "dn": f"{COMMUNITY_DN}/fault-F1234"})
        assert out["dn"] == f"uni/fabric/snmpp-default/community-{REDACTED}/fault-F1234"
        assert count == 1

    def test_a_tdn_reference_to_a_secret_dn_is_cut(self):
        out, _ = _redact({"_class": "fvRtFoo", "dn": "uni/tn-a/rtfoo", "tDn": COMMUNITY_DN})
        assert SECRET not in json.dumps(out)
        assert out["tDn"].endswith(f"community-{REDACTED}")

    def test_a_dn_embedded_in_brackets_is_cut_and_what_follows_is_kept(self):
        out, _ = _redact(
            {"_class": "aaaModLR", "dn": f"subj-[{COMMUNITY_DN}]/mod-123", "affected": COMMUNITY_DN}
        )
        assert out["dn"] == f"subj-[uni/fabric/snmpp-default/community-{REDACTED}]/mod-123"

    def test_a_word_that_merely_contains_the_prefix_is_not_a_segment(self):
        obj = {"_class": "fvBD", "dn": "uni/tn-a/BD-mycommunity-x", "descr": "a community-y"}
        out, count = _redact(obj)
        assert out is obj and count == 0

    def test_the_secret_is_removed_from_the_text_of_an_audit_record(self):
        """Measured on the lab: `descr` and `changeSet` repeat the community."""
        audit = {
            "_class": "aaaModLR",
            "dn": f"subj-[{COMMUNITY_DN}]/mod-9",
            "affected": COMMUNITY_DN,
            "descr": f"Community {SECRET} created",
            "changeSet": f"name (Old: N/A, New: {SECRET}), descr (Old: , New: )",
            "severity": "info",
        }
        out, _ = _redact(audit)
        assert SECRET not in json.dumps(out)
        assert out["descr"] == f"Community {REDACTED} created"
        assert out["severity"] == "info", "an unrelated attribute was touched"

    def test_the_text_pass_replaces_whole_words_only_and_never_the_class(self):
        """A one-letter community must not eat the letter out of every word."""
        out, _ = _redact(
            {
                "_class": "aaaModLR",
                "affected": "uni/fabric/snmpp-default/community-a",
                "descr": "a community named a was made; data is kept",
            }
        )
        assert out["descr"] == f"{REDACTED} community named {REDACTED} was made; data is kept"
        assert out["_class"] == "aaaModLR"

    def test_children_are_redacted_as_objects_of_their_own_class(self):
        parent = {
            "_class": "fvBD",
            "dn": "uni/tn-a/BD-b",
            "_children": [
                {"_class": "vnsCCred", "dn": "uni/x", "value": SECRET},
                {"_class": "fvSubnet", "dn": "uni/tn-a/BD-b/subnet-[10.0.0.1/24]", "ip": "10.0.0.1/24"},
                {"_class": "faultInst", "dn": f"{COMMUNITY_DN}/fault-F1"},
            ],
        }
        out, count = _redact(parent)
        assert SECRET not in json.dumps(out) and count == 2
        assert out["_children"][1] is parent["_children"][1], "an untouched child was copied"
        assert parent["_children"][0]["value"] == SECRET, "the input was modified"

    def test_a_parent_whose_children_are_all_ordinary_is_the_same_object(self):
        parent = {"_class": "fvBD", "dn": "uni/tn-a/BD-b", "_children": [{"_class": "fvRsCtx"}]}
        out, count = _redact(parent)
        assert out is parent and count == 0


# ── refusing the requests that would reveal one ───────────────────────────────


class TestAttributeOracles:
    @pytest.mark.parametrize(
        ("cls", "attribute"),
        [("vnsCCred", "value"), ("snmpTrapDest", "secName"), ("aaaUser", "pwd"),
         ("snmpCommunityP", "name"), ("aaaActiveUserSession", "hashToken")],
    )
    def test_an_equality_filter_on_a_secret_is_refused(self, cls, attribute):
        with pytest.raises(SecretAttributeError) as caught:
            redaction.check_request(cls, filters={attribute: "zzguess"})
        assert caught.value.attribute == attribute and caught.value.where == "filters"
        assert "zzguess" not in str(caught.value), "the refusal echoed what it was sent"

    def test_order_by_a_secret_is_refused_qualified_or_bare(self):
        for order_by in ("vnsCCred.value", "vnsCCred.value|desc", "value", "fvBD.name,vnsCCred.value"):
            with pytest.raises(SecretAttributeError):
                redaction.check_request("vnsCCred", order_by=order_by)

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
            redaction.check_request("vnsCCred", filter_expr=expression)
        assert caught.value.where == "filter_expr"

    def test_an_operand_on_another_class_is_checked_against_that_class(self):
        with pytest.raises(SecretAttributeError):
            redaction.check_request("fvBD", filter_expr='eq(vnsCCred.value,"guess")')

    def test_a_quoted_value_that_looks_like_a_reference_is_not_an_operand(self):
        redaction.check_request("fvBD", filter_expr='eq(fvBD.descr,"vnsCCred.value")')

    def test_group_by_a_secret_is_refused(self):
        with pytest.raises(SecretAttributeError) as caught:
            redaction.check_request("snmpTrapDest", group_by="secName")
        assert caught.value.where == "group_by"

    def test_the_whole_abstract_family_is_covered(self):
        """A query on the parent returns the children's instances."""
        with pytest.raises(SecretAttributeError):
            redaction.check_request("snmpACommunityP", filters={"name": "guess"})
        with pytest.raises(SecretAttributeError):
            redaction.check_request("firmwareSource", filters={"authPass": "guess"})

    def test_dn_and_rn_of_a_class_named_by_a_secret_cannot_be_probed(self):
        """`wcard(snmpCommunityP.dn, "ubli")` would confirm a substring of the community."""
        for cls in ("snmpCommunityP", "snmpCommSecP", "snmpACommunityP", "aaaActiveUserSession"):
            for attribute in ("dn", "rn"):
                with pytest.raises(SecretAttributeError):
                    redaction.check_request(cls, filter_expr=f'wcard({cls}.{attribute},"ubli")')
        with pytest.raises(SecretAttributeError):
            redaction.check_request("snmpCommunityP", group_by="dn")

    def test_ordinary_requests_are_untouched(self):
        redaction.check_request(
            "fvBD",
            filters={"name": "servers", "dn": "uni/tn-OT/BD-servers"},
            order_by="fvBD.name|asc",
            filter_expr='and(wcard(fvBD.dn,"uni/tn-OT"),eq(fvBD.arpFlood,"no"))',
            scope_dn="uni/tn-OT",
            group_by="tenant",
        )

    def test_dn_and_name_on_a_class_that_is_not_secret_stay_filterable(self):
        redaction.check_request("faultInst", filter_expr='wcard(faultInst.dn,"uni/tn-OT")')
        redaction.check_request("vnsCCred", filters={"name": "admin"})
        redaction.check_request("snmpTrapDest", group_by="host")


class TestDnOracles:
    @pytest.mark.parametrize("dn", [COMMUNITY_DN, f"{COMMUNITY_DN}/fault-F1",
                                    f"subj-[{COMMUNITY_DN}]/mod-1", f"uni/x/actsession-{SECRET}",
                                    f"uni/x/sitejwtpubkey-{SECRET}", f"uni/x/delsession-{SECRET}"])
    def test_a_dn_naming_a_secret_is_refused_on_its_shape(self, dn):
        with pytest.raises(SecretDnError) as caught:
            redaction.check_dn(dn)
        assert SECRET not in str(caught.value), "the refusal echoed what it was sent"

    def test_the_marker_in_a_dn_cannot_be_used_to_address_anything(self):
        with pytest.raises(SecretDnError):
            redaction.check_dn(f"uni/fabric/snmpp-default/community-{REDACTED}")

    def test_the_answer_is_the_same_whether_or_not_the_object_exists(self):
        """The property that matters: no input pair differs only in existence."""
        errors = set()
        for guess in ("public", "private", SECRET, "x"):
            with pytest.raises(SecretDnError) as caught:
                redaction.check_dn(f"uni/fabric/snmpp-default/community-{guess}")
            errors.add(str(caught.value))
        assert len(errors) == 1

    def test_scope_a_filter_value_and_a_quoted_literal_are_all_checked(self):
        for kwargs in (
            {"scope_dn": COMMUNITY_DN},
            {"filters": {"dn": COMMUNITY_DN}},
            {"filter_expr": f'eq(snmpCommunityP.descr,"community-{SECRET}")'},
        ):
            with pytest.raises(SecretDnError):
                redaction.check_request("fvBD", **kwargs)

    def test_a_case_variant_does_not_slip_past(self):
        with pytest.raises(SecretDnError):
            redaction.check_dn("uni/fabric/snmpp-default/COMMUNITY-public")

    def test_the_bare_prefix_and_ordinary_dns_are_fine(self):
        for dn in ("uni/fabric/snmpp-default", "uni/tn-OT/BD-servers", "uni/fabric/snmpp-default/community-"):
            redaction.check_dn(dn)


# ── get_schema labels ─────────────────────────────────────────────────────────


class TestSchemaLabels:
    def test_a_secret_property_is_labelled_and_an_ordinary_one_is_not(self):
        schema = catalog.load_schema("vnsCCred", properties_filter=["value", "name"])
        out = redaction.mark_secret_properties(schema, "vnsCCred")
        assert out["property_details"]["value"]["secret"] is True
        assert "secret" not in out["property_details"]["name"]

    def test_the_names_stay_listed(self):
        schema = redaction.mark_secret_properties(catalog.load_schema("vnsCCred"), "vnsCCred")
        assert "value" in schema["properties"]

    def test_a_schema_without_details_is_left_alone(self):
        schema = catalog.load_schema("vnsCCred")
        assert redaction.mark_secret_properties(dict(schema), "vnsCCred") == schema

    def test_dn_is_labelled_on_a_class_named_by_a_secret(self):
        schema = catalog.load_schema("snmpCommunityP", properties_filter=["dn", "name", "descr"])
        details = redaction.mark_secret_properties(schema, "snmpCommunityP")["property_details"]
        assert details["dn"]["secret"] and details["name"]["secret"]
        assert "secret" not in details["descr"]

    def test_an_ordinary_class_is_byte_identical(self):
        before = catalog.load_schema("fvBD", include_property_details=True)
        after = redaction.mark_secret_properties(
            catalog.load_schema("fvBD", include_property_details=True), "fvBD"
        )
        assert json.dumps(after) == json.dumps(before)


# ── the startup guard ─────────────────────────────────────────────────────────


class TestStartupGuard:
    def test_the_real_policy_verifies(self):
        redaction.verify_policy()

    def test_it_is_idempotent(self):
        redaction.verify_policy()
        redaction.verify_policy()

    @pytest.mark.parametrize(
        "name", ["SECRET_VALUE_POSITIONS", "SECRET_DN_POSITIONS", "secret_dn_classes"]
    )
    def test_a_missing_name_stops_startup_and_names_it(self, name, monkeypatch, fresh_policy):
        monkeypatch.delattr(_secrets, name)
        with pytest.raises(RedactionPolicyError, match=name) as caught:
            redaction.verify_policy()
        assert "niwaki>=2.2,<2.3" in str(caught.value)

    def test_a_missing_module_stops_startup(self, monkeypatch, fresh_policy):
        import sys

        monkeypatch.setitem(sys.modules, "niwaki._secrets", None)
        monkeypatch.delattr("niwaki._secrets", raising=False)
        with pytest.raises(RedactionPolicyError, match=r"niwaki\._secrets"):
            redaction.verify_policy()

    @pytest.mark.parametrize(
        "bad",
        [frozenset(), {("vnsCCred", "value")}, frozenset({"vnsCCred"}), frozenset({("a",)}),
         frozenset({("a", 1)}), ["x"]],
    )
    def test_a_table_that_changed_shape_stops_startup(self, bad, monkeypatch, fresh_policy):
        monkeypatch.setattr(_secrets, "SECRET_VALUE_POSITIONS", bad)
        with pytest.raises(RedactionPolicyError, match="SECRET_VALUE_POSITIONS"):
            redaction.verify_policy()

    def test_a_function_that_changed_what_it_returns_stops_startup(self, monkeypatch, fresh_policy):
        monkeypatch.setattr(_secrets, "secret_dn_classes", lambda: ["snmpCommunityP"])
        with pytest.raises(RedactionPolicyError, match="secret_dn_classes"):
            redaction.verify_policy()

    def test_a_table_that_resolves_but_forgot_the_measured_leak_stops_startup(
        self, monkeypatch, fresh_policy
    ):
        """Present, well-formed and wrong: the case no structural check sees."""
        kept = frozenset(p for p in _secrets.SECRET_VALUE_POSITIONS if p != ("vnsCCred", "value"))
        monkeypatch.setattr(_secrets, "SECRET_VALUE_POSITIONS", kept)
        with pytest.raises(RedactionPolicyError, match=r"vnsCCred\.value"):
            redaction.verify_policy()

    def test_a_table_that_forgot_the_secret_dn_classes_stops_startup(
        self, monkeypatch, fresh_policy
    ):
        monkeypatch.setattr(_secrets, "SECRET_DN_POSITIONS", frozenset({("pkiSiteJwtPubKey", "jwtApiKey")}))
        monkeypatch.setattr(_secrets, "secret_dn_classes", lambda: frozenset({"pkiSiteJwtPubKey"}))
        monkeypatch.setattr(
            _secrets,
            "SECRET_VALUE_POSITIONS",
            frozenset(p for p in _secrets.SECRET_VALUE_POSITIONS if p[0] != "snmpACommunityP"),
        )
        with pytest.raises(RedactionPolicyError, match="snmpCommunityP"):
            redaction.verify_policy()

    def test_a_marker_that_stopped_being_a_string_stops_startup(self, monkeypatch, fresh_policy):
        monkeypatch.setattr(redaction, "REDACTED", None)
        with pytest.raises(RedactionPolicyError, match="REDACTED"):
            redaction.verify_policy()

    def test_a_class_named_by_a_secret_in_a_shape_it_cannot_cut_stops_startup(
        self, monkeypatch, fresh_policy
    ):
        """An RN like `foo-[{a}]-{b}` cannot be found by a literal prefix: refuse, don't guess."""
        real = catalog.load_schema

        def odd(class_name, *args, **kwargs):
            schema = real(class_name, *args, **kwargs)
            if class_name == "pkiSiteJwtPubKey":
                schema = {**schema, "rnFormat": "site-[{jwtApiKey}]-{other}"}
            return schema

        monkeypatch.setattr(catalog, "load_schema", odd)
        with pytest.raises(RedactionPolicyError, match="pkiSiteJwtPubKey"):
            redaction.verify_policy()

    def test_the_failure_is_a_registry_error_so_startup_treats_it_like_a_moved_catalogue(self):
        assert issubclass(RedactionPolicyError, RegistryError)
        assert issubclass(SecretAttributeError, NiwashiMcpError)
        assert issubclass(SecretDnError, NiwashiMcpError)
