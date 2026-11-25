# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""`get_schema` says how many targets a relation allows and how it names them.

`relationTo[*].cardinality` was `""` on every entry — documented as "do not
branch on it" — because the `mo.residual` blob the relation dicts come from
carries no cardinality. The catalogue's `relation` table does: for each Rs
class its cardinality (`n-to-1`), and whether it holds the target's name
(`named`, `tnFvCtxName`) or its DN (`explicit`, `tDn`). `annotate_relations`
reads it on top of `load_schema`, which stays the unannotated projection the
baseline digests and the jsonmeta oracle pin.

Expectations are derived from a direct read of the `relation` table, not from
the implementation's own query, so a bug shared by both would have to be a bug
in two places.
"""

from __future__ import annotations

import pytest

from niwashi_mcp.main import get_schema
from niwashi_mcp.registry import catalog

pytestmark = pytest.mark.catalog

CARDINALITIES = {"1-to-1", "1-to-m", "n-to-1", "n-to-m"}
KINDS = {"named", "explicit"}


def _wire(name: str) -> str:
    return name.replace(":", "")


@pytest.fixture(scope="module")
def table() -> dict[str, tuple[str, str]]:
    """`{relation class: (cardinality, kind)}`, straight from the table."""
    return {
        rs: (cardinality, kind)
        for rs, cardinality, kind in catalog._query(
            "SELECT rs_class, cardinality, rel_type FROM relation "
            "WHERE cardinality IS NOT NULL AND rel_type IS NOT NULL"
        )
    }


def _annotated(class_name: str) -> dict:
    return catalog.annotate_relations(catalog.load_schema(class_name))


class TestRelationTo:
    def test_a_named_relation_reports_cardinality_and_kind(self):
        """`fvBD` -> `fvRsCtx`: many bridge domains, one VRF each, by name."""
        entry = _annotated("fvBD")["relationTo"]["fv:RsCtx"]
        assert entry == {"targetClass": "fv:Ctx", "cardinality": "n-to-1", "kind": "named"}

    def test_an_explicit_relation_reports_cardinality_and_kind(self):
        """`fvRsBdFloodTo` holds a DN (`tDn`) and allows several targets."""
        entry = _annotated("fvBD")["relationTo"]["fv:RsBdFloodTo"]
        assert entry == {"targetClass": "vz:Filter", "cardinality": "n-to-m", "kind": "explicit"}

    def test_kind_agrees_with_the_attributes_the_relation_class_declares(self):
        """`explicit` means no name attribute to filter on, `named` means one.

        What `kind` tells an agent is which attribute of the relation object to
        read. Checked against the properties of the relation classes themselves,
        which is a separate source from the `relation` table.
        """
        explicit = catalog.load_schema("fvRsBdFloodTo")["properties"]
        named = catalog.load_schema("fvRsCtx")["properties"]
        assert "tDn" in explicit
        assert not [p for p in explicit if p.startswith("tn") and p.endswith("Name")]
        assert "tnFvCtxName" in named


class TestRelationFrom:
    def test_an_incoming_relation_gains_cardinality_and_kind(self):
        """`fvCtx` is the target of `fvRsCtx`; its `fvRtCtx` mirrors it."""
        entry = _annotated("fvCtx")["relationFrom"]["fv:RtCtx"]
        assert entry == {"sourceClass": "fv:ABDPol", "cardinality": "n-to-1", "kind": "named"}


class TestNothingExistingChanges:
    def test_keys_and_classes_are_exactly_what_load_schema_returned(self):
        """Additive means additive: swept over every class that has a relation."""
        checked = 0
        for (name,) in catalog._query("SELECT class_name FROM mo"):
            plain = catalog.load_schema(name)
            annotated = catalog.annotate_relations(catalog.load_schema(name))
            for section, class_key in (("relationTo", "targetClass"), ("relationFrom", "sourceClass")):
                if section not in plain:
                    assert section not in annotated, f"{name} gained a {section} section"
                    continue
                assert list(annotated[section]) == list(plain[section]), name
                for rel, entry in plain[section].items():
                    assert annotated[section][rel][class_key] == entry[class_key], (name, rel)
                    checked += 1
            rest = {k: v for k, v in plain.items() if not k.startswith("relation")}
            assert {k: v for k, v in annotated.items() if not k.startswith("relation")} == rest
        assert checked > 6_000, f"only {checked} relations swept — the check stopped covering the corpus"

    def test_load_schema_itself_is_left_unannotated(self):
        """The baseline digests and the jsonmeta oracle pin this, so it must not move."""
        schema = catalog.load_schema("fvBD")
        assert {e["cardinality"] for e in schema["relationTo"].values()} == {""}
        assert all("kind" not in e for e in schema["relationTo"].values())
        assert all("cardinality" not in e for e in catalog.load_schema("fvCtx")["relationFrom"].values())


class TestEveryEntryAgreesWithTheTable:
    def test_every_entry_carries_what_the_table_says(self, table):
        """All of them, in both directions, against a direct read of the table."""
        unannotated = []
        total = 0
        for (name,) in catalog._query("SELECT class_name FROM mo"):
            schema = _annotated(name)
            for section in ("relationTo", "relationFrom"):
                for rel, entry in (schema.get(section) or {}).items():
                    total += 1
                    expected = table.get(_wire(rel))
                    if expected is None:
                        unannotated.append((name, section, rel))
                        assert "kind" not in entry, (name, rel)
                        assert entry.get("cardinality", "") == "", (name, rel)
                        continue
                    assert (entry["cardinality"], entry["kind"]) == expected, (name, rel)
        assert total > 6_000
        # The two relations of fvCtx whose Rs/Rt classes are cloud classes the
        # catalogue does not contain. If this grows, a lookup has stopped matching.
        assert unannotated == [
            ("fvCtx", "relationTo", "fv:RsCtxToCloudVrfRouteLeakPol"),
            ("fvCtx", "relationFrom", "fv:RtCloudEPgCtx"),
        ]

    def test_the_vocabularies_are_the_ones_the_docs_teach(self, table):
        """SKILL.md and the tool docstring name these values; a new one needs documenting."""
        assert {c for c, _ in table.values()} == CARDINALITIES
        assert {k for _, k in table.values()} == KINDS


class TestEdges:
    def test_a_schema_without_relations_is_returned_untouched_without_a_query(self, monkeypatch):
        def refuse(*_args, **_kwargs):
            raise AssertionError("annotate_relations queried the catalogue for a class with no relations")

        monkeypatch.setattr(catalog, "_query", refuse)
        schema = {"label": "Tenant", "properties": ["name"]}
        assert catalog.annotate_relations(schema) == {"label": "Tenant", "properties": ["name"]}

    def test_an_unknown_relation_keeps_an_empty_cardinality_and_gets_no_kind(self):
        schema = {"relationTo": {"zz:RsNothing": {"targetClass": "zz:Nothing", "cardinality": ""}}}
        assert catalog.annotate_relations(schema)["relationTo"]["zz:RsNothing"] == {
            "targetClass": "zz:Nothing",
            "cardinality": "",
        }


@pytest.mark.asyncio
class TestThroughTheTool:
    async def test_get_schema_returns_the_annotated_relations(self, tool_ctx):
        schema = await get_schema("fvBD", tool_ctx)
        assert schema["relationTo"]["fv:RsCtx"]["cardinality"] == "n-to-1"
        assert schema["relationTo"]["fv:RsCtx"]["kind"] == "named"
        assert schema["relationTo"]["fv:RsCtx"]["targetClass"] == "fv:Ctx"

    async def test_the_projection_options_leave_relations_alone(self, tool_ctx):
        """`properties_filter` and `list_limit` bound other keys, not this one."""
        schema = await get_schema("fvBD", tool_ctx, properties_filter=["name"], list_limit=1)
        assert schema["relationTo"]["fv:RsCtx"]["kind"] == "named"
        assert set(schema["property_details"]) == {"name"}

    async def test_an_unknown_class_still_returns_empty(self, tool_ctx):
        assert await get_schema("fvBd", tool_ctx) == {}
