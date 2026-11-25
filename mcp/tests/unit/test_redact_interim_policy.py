# Copyright (C) 2026 Khalid El-Ouiali — MONARK AIOPS srl
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
INTERIM — the tests that read niwaki's private policy tables. Deleted at the switch.

`niwashi_mcp.redact` is the interim form of niwaki 2.3's public `niwaki.secrets`.
While it is, it is built on `niwaki._secrets` (the curated tables) and on the
schema's `secure` flag, and these tests pin that coupling: that the policy *is*
niwaki's rather than a copy of it, that a table which moves stops startup, and
that the supplement of positions niwaki 2.2 lacks is still needed.

Everything about what the module *does* is in `test_redact.py`, written against
the interface alone. When `redact.py` becomes a re-export of `niwaki.secrets`,
this file is deleted and that one is not touched.
"""

from __future__ import annotations

import json
import sys

import pytest
from niwaki import _secrets
from niwaki.snapshot import REDACTED

from niwashi_mcp import redact
from niwashi_mcp.exceptions import RedactionPolicyError, RegistryError
from niwashi_mcp.registry import catalog

pytestmark = pytest.mark.catalog

SECRET = "zzsecret"


@pytest.fixture
def fresh_policy():
    """Rebuild the policy around a test that damages niwaki's tables, then restore it."""
    redact._reset()
    yield
    redact._reset()


def _redacts(cls: str, prop: str) -> bool:
    return redact.redact(cls, {prop: SECRET})[prop] == REDACTED


# ── the policy is niwaki's, not a copy ────────────────────────────────────────


class TestThePolicyIsNiwakis:
    def test_every_schema_flagged_property_is_withheld(self):
        """The mechanical layer: 169 (class, property) pairs, inherited ones included."""
        flagged = catalog.secure_properties()
        assert sum(map(len, flagged.values())) >= 100, "the flag decoded to almost nothing"
        for cls, props in flagged.items():
            for prop in props:
                assert _redacts(cls, prop), f"{cls}.{prop} leaks"
                assert redact.is_secret(cls, prop)

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
            assert _redacts(cls, prop), f"{cls}.{prop} leaks"

    def test_every_secret_naming_property_is_withheld(self):
        for cls, prop in _secrets.SECRET_DN_POSITIONS:
            assert _redacts(cls, prop), f"{cls}.{prop} leaks"

    def test_niwakis_curated_position_on_an_abstract_class_reaches_its_concrete_children(self):
        """niwaki lists `snmpACommunityP.name`; the APIC returns the children."""
        for cls in ("snmpCommunityP", "snmpCommSecP"):
            assert _redacts(cls, "name"), cls
        for cls in redact._concrete_subclasses("firmwareSource"):
            assert _redacts(cls, "authPass"), cls

    def test_the_class_set_named_by_a_secret_covers_the_one_niwaki_misses(self):
        policy = redact._policy()
        assert _secrets.secret_dn_classes() <= policy.dn_classes
        assert "snmpCommSecP" in policy.dn_classes - _secrets.secret_dn_classes()

    def test_no_class_anywhere_is_named_by_a_secret_it_does_not_know_about(self):
        """The drift net: a future niwaki that curates a new naming secret.

        Walks the whole catalogue — every class's naming properties against the
        policy — so a class whose DN carries a secret fails here rather than
        publishing it. The fix is then to teach the policy, not to loosen this.
        """
        policy = redact._policy()
        escaped = []
        for cls, identified_by in catalog._query("SELECT class_name, identified_by FROM mo"):
            for prop in json.loads(identified_by) if isinstance(identified_by, str) else ():
                if prop in policy.values.get(cls, ()) and cls not in policy.dn_classes:
                    escaped.append((cls, prop))
        assert not escaped, f"named by a secret but not treated as such: {escaped}"

    def test_each_secret_rn_prefix_is_unique_to_the_classes_named_by_a_secret(self):
        """A prefix another class also uses would redact that class's DNs by mistake."""
        policy = redact._policy()
        for prefix in policy.prefixes:
            users = {
                cls
                for cls, rn in catalog._query("SELECT class_name, rn_format FROM mo")
                if prefix in (rn or "")
            }
            assert users <= policy.dn_classes, (prefix, users - policy.dn_classes)

    def test_the_heuristic_is_niwakis_own_name_sweep(self):
        for name in ("password", "pwd", "passphrase", "community", "secName", "otpkey", "authKey"):
            assert _secrets.PROP_PATTERN.search(name), name
            assert redact.is_secret("futureThing", name), name


class TestTheSupplementIsStillNeeded:
    """`_INTERIM_POSITIONS` is what niwaki 2.2 lacks. When niwaki carries one, drop it.

    The two sets are asserted *disjoint* rather than merely overlapping, so that a
    niwaki release which absorbs a position fails here and the entry is deleted,
    instead of the supplement quietly becoming a second, unexplained source of
    truth beside niwaki's.
    """

    def test_no_interim_position_is_already_niwakis(self):
        flagged = {(c, p) for c, props in catalog.secure_properties().items() for p in props}
        known = flagged | _secrets.SECRET_VALUE_POSITIONS | _secrets.SECRET_DN_POSITIONS
        absorbed = redact._INTERIM_POSITIONS & known
        assert not absorbed, f"niwaki now carries {sorted(absorbed)}: drop them from _INTERIM_POSITIONS"


# ── the startup guard ─────────────────────────────────────────────────────────


class TestStartupGuard:
    def test_the_real_policy_verifies(self):
        redact.verify_policy()

    def test_it_is_idempotent(self):
        redact.verify_policy()
        redact.verify_policy()

    @pytest.mark.parametrize(
        "name",
        ["SECRET_VALUE_POSITIONS", "SECRET_DN_POSITIONS", "secret_dn_classes", "PROP_PATTERN"],
    )
    def test_a_missing_name_stops_startup_and_names_it(self, name, monkeypatch, fresh_policy):
        monkeypatch.delattr(_secrets, name)
        with pytest.raises(RedactionPolicyError, match=name) as caught:
            redact.verify_policy()
        assert "niwaki>=2.2,<2.3" in str(caught.value)

    def test_a_missing_module_stops_startup(self, monkeypatch, fresh_policy):
        monkeypatch.setitem(sys.modules, "niwaki._secrets", None)
        monkeypatch.delattr("niwaki._secrets", raising=False)
        with pytest.raises(RedactionPolicyError, match=r"niwaki\._secrets"):
            redact.verify_policy()

    @pytest.mark.parametrize(
        "bad",
        [frozenset(), {("vnsCCred", "value")}, frozenset({"vnsCCred"}), frozenset({("a",)}),
         frozenset({("a", 1)}), ["x"]],
    )
    def test_a_table_that_changed_shape_stops_startup(self, bad, monkeypatch, fresh_policy):
        monkeypatch.setattr(_secrets, "SECRET_VALUE_POSITIONS", bad)
        with pytest.raises(RedactionPolicyError, match="SECRET_VALUE_POSITIONS"):
            redact.verify_policy()

    def test_a_function_that_changed_what_it_returns_stops_startup(self, monkeypatch, fresh_policy):
        monkeypatch.setattr(_secrets, "secret_dn_classes", lambda: ["snmpCommunityP"])
        with pytest.raises(RedactionPolicyError, match="secret_dn_classes"):
            redact.verify_policy()

    def test_a_pattern_that_is_no_longer_one_stops_startup(self, monkeypatch, fresh_policy):
        monkeypatch.setattr(_secrets, "PROP_PATTERN", "passw|pwd")
        with pytest.raises(RedactionPolicyError, match="PROP_PATTERN"):
            redact.verify_policy()

    def test_a_table_that_resolves_but_forgot_the_measured_leak_stops_startup(
        self, monkeypatch, fresh_policy
    ):
        """Present, well-formed and wrong: the case no structural check sees."""
        kept = frozenset(p for p in _secrets.SECRET_VALUE_POSITIONS if p != ("vnsCCred", "value"))
        monkeypatch.setattr(_secrets, "SECRET_VALUE_POSITIONS", kept)
        with pytest.raises(RedactionPolicyError, match=r"vnsCCred\.value"):
            redact.verify_policy()

    def test_a_table_that_forgot_the_secret_dn_classes_stops_startup(
        self, monkeypatch, fresh_policy
    ):
        monkeypatch.setattr(
            _secrets, "SECRET_DN_POSITIONS", frozenset({("pkiSiteJwtPubKey", "jwtApiKey")})
        )
        monkeypatch.setattr(_secrets, "secret_dn_classes", lambda: frozenset({"pkiSiteJwtPubKey"}))
        monkeypatch.setattr(
            _secrets,
            "SECRET_VALUE_POSITIONS",
            frozenset(p for p in _secrets.SECRET_VALUE_POSITIONS if p[0] != "snmpACommunityP"),
        )
        with pytest.raises(RedactionPolicyError, match="snmpCommunityP"):
            redact.verify_policy()

    def test_a_marker_that_stopped_being_a_string_stops_startup(self, monkeypatch, fresh_policy):
        monkeypatch.setattr(redact, "REDACTED", None)
        with pytest.raises(RedactionPolicyError, match="REDACTED"):
            redact.verify_policy()

    def test_a_policy_that_resolves_but_scrubs_nothing_stops_startup(
        self, monkeypatch, fresh_policy
    ):
        """The functional probe: a changeSet scrubber that no longer scrubs is not a guard."""
        monkeypatch.setattr(redact, "_scrub_pairs", lambda text, *a, **k: (text, False))
        with pytest.raises(RedactionPolicyError, match="did not remove the secret"):
            redact.verify_policy()

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
            redact.verify_policy()

    def test_the_failure_is_a_registry_error_so_startup_treats_it_like_a_moved_catalogue(self):
        assert issubclass(RedactionPolicyError, RegistryError)
