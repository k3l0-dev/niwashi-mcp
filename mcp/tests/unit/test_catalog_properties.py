# Copyright (C) 2026 Khalid El-Ouiali — MONARK AIOPS srl
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Attribute lookups on the catalogue: `known_properties`, `property_exists`,
`closest_properties`.

APIC answers a filter on an invented attribute with `[]` and no error, so a
caller must be able to ask whether a name exists before it is sent. The part
that needs tests rather than trust is the abstract class: a query on one
returns instances of its concrete subclasses, and those carry attributes the
abstract class never declares.

The real example used throughout is `fvABD`, an abstract bridge-domain base.
It declares 34 properties; its concrete descendants `fvBD`, `fvBDDef` and
`fvSvcBD` bring the union to 54. `hostBasedRouting` is the cleanest probe — on
`fvBD` and absent from `fvABD`'s own list.

Expectations are derived from `load_schema` (a separate code path, reading
`class_prop` by class id) rather than copied from the implementation's own SQL, so a
bug shared by both would still have to be a bug in two places.
"""

from __future__ import annotations

import pytest

from niwashi_mcp.exceptions import UnknownAttributeError
from niwashi_mcp.registry import catalog

pytestmark = pytest.mark.catalog

ABSTRACT = "fvABD"
SUBCLASS_ONLY = "hostBasedRouting"
CONCRETE_DESCENDANTS = ("fvBD", "fvBDDef", "fvSvcBD")

# Concrete classes across packages. For each, `known_properties` must equal the
# class's own property list exactly — no more (descendants leaking in), no less.
CONCRETE_SAMPLE = [
    "fvBD", "fvAEPg", "fvTenant", "fvSubnet", "fvCtx", "fvRsCtx",
    "l3extOut", "vzBrCP", "vzEntry", "fabricNode", "l1PhysIf", "polUni",
]


def _own(class_name: str) -> frozenset[str]:
    return frozenset(catalog.load_schema(class_name)["properties"])


class TestConcreteClass:
    def test_a_real_property_exists(self):
        assert catalog.property_exists("fvBD", "arpFlood") is True

    def test_a_misspelt_property_does_not(self):
        assert catalog.property_exists("fvBD", "arpFlod") is False

    def test_the_match_is_case_sensitive(self):
        """`arpflood` reaches the APIC, is syntactically valid, and returns `[]`."""
        assert catalog.property_exists("fvBD", "arpflood") is False

    def test_a_readable_alias_is_not_a_property(self):
        """The catalogue is bilingual; this server is wire-only (module docstring).

        `description` is the readable name of the wire attribute `descr`.
        """
        assert catalog.property_exists("fvBD", "descr") is True
        assert catalog.property_exists("fvBD", "description") is False

    @pytest.mark.parametrize("name", ["dn", "rn", "status", "childAction", "uid", "modTs"])
    def test_the_bookkeeping_attributes_every_filter_may_use_exist(self, name):
        """A `False` here would make a validating caller reject `dn` and `status`."""
        assert catalog.property_exists("fvBD", name) is True

    @pytest.mark.parametrize("class_name", CONCRETE_SAMPLE)
    def test_it_is_exactly_the_classs_own_properties(self, class_name):
        assert catalog.known_properties(class_name) == _own(class_name)

    def test_a_sibling_is_not_absorbed(self):
        """`fvBDDef` shares an ancestor with `fvBD` but is not under it."""
        assert catalog.known_properties("fvBD") != catalog.known_properties("fvBDDef")
        assert catalog.known_properties("fvBD") == _own("fvBD")


class TestAbstractClass:
    def test_the_probe_is_a_real_abstract_class_with_a_subclass_only_property(self):
        """Pins the fixture facts, so a catalogue change fails here and not
        as a baffling assertion further down."""
        schema = catalog.load_schema(ABSTRACT)
        assert schema["isAbstract"] is True
        assert SUBCLASS_ONLY not in schema["properties"]
        assert SUBCLASS_ONLY in catalog.load_schema("fvBD")["properties"]

    def test_the_union_includes_a_property_only_a_subclass_declares(self):
        assert SUBCLASS_ONLY in catalog.known_properties(ABSTRACT)

    def test_it_is_the_own_properties_plus_every_concrete_descendants(self):
        expected = _own(ABSTRACT).union(*(_own(c) for c in CONCRETE_DESCENDANTS))
        assert catalog.known_properties(ABSTRACT) == expected
        assert len(expected) > len(_own(ABSTRACT)), "the union must actually be wider"

    def test_the_abstract_classs_own_names_are_kept(self):
        assert _own(ABSTRACT) <= catalog.known_properties(ABSTRACT)

    def test_property_exists_agrees_with_known_properties(self):
        """The two answer the same question; a disagreement lets a filter
        through that `closest_properties` could never suggest."""
        known = catalog.known_properties(ABSTRACT)
        assert catalog.property_exists(ABSTRACT, SUBCLASS_ONLY) is True
        for name in ("enableRogueExceptMac", "descr", "notAnAttribute"):
            assert catalog.property_exists(ABSTRACT, name) is (name in known)

    def test_an_invented_name_is_still_rejected(self):
        """The union widens the set; it must not become a wildcard."""
        assert catalog.property_exists(ABSTRACT, "notAnAttribute") is False


class TestUnknownClass:
    def test_known_properties_is_empty(self):
        result = catalog.known_properties("fvNoSuchClass")
        assert result == frozenset()
        assert isinstance(result, frozenset)

    def test_property_exists_is_false_even_for_universal_attributes(self):
        assert catalog.property_exists("fvNoSuchClass", "dn") is False

    def test_closest_properties_is_empty(self):
        assert catalog.closest_properties("fvNoSuchClass", "dn") == []

    def test_the_class_name_is_case_sensitive(self):
        """`fvBd` must not resolve to `fvBD`'s properties."""
        assert catalog.known_properties("fvBd") == frozenset()


class TestClosestProperties:
    def test_a_typo_suggests_the_real_name_first(self):
        assert catalog.closest_properties("fvBD", "arpFlod")[0] == "arpFlood"

    def test_a_case_slip_suggests_the_wire_name(self):
        assert catalog.closest_properties("fvBD", "arpflood") == ["arpFlood"]
        assert catalog.closest_properties("fvBD", "Name")[0] == "name"

    def test_an_all_caps_name_is_folded_to_the_wire_name(self):
        """`difflib` is case-sensitive: `DN` shares no character with `dn`, so
        its ratio is 0 and only the explicit case-insensitive pass finds it."""
        assert catalog.closest_properties("fvBD", "DN") == ["dn"]
        assert catalog.closest_properties("fvBD", "STATUS") == ["status"]

    def test_a_readable_name_reaches_its_wire_name(self):
        """The mistake the bilingual catalogue invites: `description` for `descr`."""
        assert "descr" in catalog.closest_properties("fvBD", "description")

    def test_an_abstract_class_suggests_a_subclass_only_property(self):
        """Suggestions draw from the same set as `property_exists`."""
        assert catalog.closest_properties(ABSTRACT, "hostBasedRoutng") == [SUBCLASS_ONLY]

    def test_a_suggestion_is_always_an_existing_property(self):
        for typo in ("arpFlod", "descrr", "unicastRout", "mcastAllow"):
            for suggestion in catalog.closest_properties("fvBD", typo):
                assert catalog.property_exists("fvBD", suggestion)

    def test_nothing_close_gives_an_empty_list_not_a_guess(self):
        assert catalog.closest_properties("fvBD", "zzzzqqq") == []

    def test_it_honours_n(self):
        many = catalog.closest_properties("fvBD", "unk", n=5)
        assert len(catalog.closest_properties("fvBD", "unk", n=1)) <= 1
        assert len(many) <= 5

    def test_a_non_positive_n_returns_nothing_rather_than_raising(self):
        assert catalog.closest_properties("fvBD", "arpFlod", n=0) == []

    def test_the_result_is_deterministic(self):
        first = catalog.closest_properties("fvBD", "unk")
        assert catalog.closest_properties("fvBD", "unk") == first


class TestCache:
    def test_it_is_bounded(self):
        """An abstract union can be 4,475 names (`statsItem`); an unbounded
        cache would keep every class ever probed resident."""
        maxsize = catalog.known_properties.cache_info().maxsize
        assert maxsize is not None
        assert maxsize <= 4096

    def test_a_repeat_call_is_served_from_the_cache(self):
        catalog.known_properties.cache_clear()
        catalog.known_properties("fvBD")
        before = catalog.known_properties.cache_info().hits
        catalog.known_properties("fvBD")
        assert catalog.known_properties.cache_info().hits == before + 1

    def test_the_cached_value_cannot_be_mutated_by_a_caller(self):
        """Callers share one object per class; a mutable set would let one
        request's edit become every later request's answer."""
        with pytest.raises(AttributeError):
            catalog.known_properties("fvBD").add("x")  # type: ignore[attr-defined]


class TestAgainstTheErrorType:
    def test_the_helpers_feed_the_error_the_way_a_caller_would_use_them(self):
        attribute = "arpFlod"
        assert not catalog.property_exists("fvBD", attribute)
        error = UnknownAttributeError(
            "fvBD",
            attribute,
            catalog.closest_properties("fvBD", attribute),
            catalog.apic_version(),
        )
        assert error.suggestions[0] == "arpFlood"
        assert "arpFlood" in str(error)
        assert catalog.apic_version() in str(error)
