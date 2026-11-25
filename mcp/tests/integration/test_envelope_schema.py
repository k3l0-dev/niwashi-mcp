# Copyright (C) 2026 Khalid El-Ouiali — MONARK AIOPS srl
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
The envelopes `query` and `count` advertise, and whether what they return obeys.

Why this file exists
--------------------
FastMCP builds a tool's `outputSchema` from its return annotation. With a bare
`dict[str, Any]` the schema was `{"type": "object", "additionalProperties":
true}` — it told a client nothing about `truncated`, `complete` or `next_page`,
the three keys an agent must read before trusting an answer. Naming the keys
turns the schema into a contract, and a contract can be wrong: a key declared
non-null that is sometimes null, or required and sometimes absent, fails *at
runtime* in the client's validator, on exactly the return path nobody tried.

So both halves are asserted here. The schema must name the keys, and every
return path must validate against it end to end — through a real in-memory MCP
client, because that is what runs the validator. Calling the tool function
directly, as the other integration tests do, never touches the schema.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastmcp import Client, FastMCP
from fastmcp.server.lifespan import lifespan

from niwashi_mcp import main
from niwashi_mcp.main import CountEnvelope, QueryEnvelope
from tests.conftest import StubBackend
from tests.integration.test_tools import _WIDE_IMDATA

_QUERY_KEYS = {
    "results", "returned", "total_available", "truncated",
    "next_page", "complete", "note", "fields",
}
_COUNT_REQUIRED = {"class_name", "count", "scope_dn", "filters"}
_COUNT_OPTIONAL = {"warnings", "group_by", "groups", "ungrouped", "complete", "note"}


def _output_schema(name: str) -> dict:
    return asyncio.run(main.mcp.get_tool(name)).output_schema


# ── what is advertised ────────────────────────────────────────────────────────


class TestTheAdvertisedOutputSchema:
    def test_query_names_every_envelope_key(self):
        schema = _output_schema("query")

        assert set(schema["properties"]) >= _QUERY_KEYS
        assert set(schema["required"]) >= _QUERY_KEYS, (
            "a key query always returns is advertised as optional"
        )

    def test_query_null_valued_keys_admit_null(self):
        """`next_page`, `note` and `fields` are present-but-null on the default path.

        Declared as plain `int` / `str` / `list`, the default call — the one
        every agent makes first — would fail the client's validation.
        """
        props = _output_schema("query")["properties"]
        for key in ("next_page", "note", "fields"):
            kinds = {branch.get("type") for branch in props[key]["anyOf"]}
            assert "null" in kinds, f"query.{key} cannot be null but is on the wire"

    def test_query_results_items_stay_free_form(self):
        """Which attributes an object carries depends on its class — not for the
        envelope to enumerate."""
        items = _output_schema("query")["properties"]["results"]["items"]
        assert items["type"] == "object"
        assert items.get("additionalProperties") is True

    def test_count_names_its_keys_and_keeps_the_added_ones_optional(self):
        """The four keys count always returns are required; the ones other
        changes may add are not, so adding one never invalidates a client."""
        schema = _output_schema("count")

        assert set(schema["required"]) == _COUNT_REQUIRED
        assert set(schema["properties"]) == _COUNT_REQUIRED | _COUNT_OPTIONAL

    def test_the_schema_tolerates_a_key_it_has_not_heard_of(self):
        """Not `additionalProperties: false` — a key added by a newer server must
        not make an older client's validator reject the whole answer."""
        for name in ("query", "count"):
            assert _output_schema(name).get("additionalProperties") is not False, name

    def test_typed_dicts_and_schema_agree(self):
        """The TypedDicts are the source of the schema; this fails if a key is
        added to one and the tool's return annotation is left on the old type."""
        assert set(QueryEnvelope.__required_keys__) == _QUERY_KEYS
        assert set(CountEnvelope.__required_keys__) == _COUNT_REQUIRED
        assert set(CountEnvelope.__optional_keys__) == _COUNT_OPTIONAL


# ── what is returned obeys it ─────────────────────────────────────────────────


@pytest.fixture
def served(monkeypatch, catalogue_index):
    """The real `mcp` object, served in memory over a stub backend.

    Swaps only the lifespan: the tools, their annotations and their output
    schemas are the ones a client gets in production. The search index is the
    session-scoped one: rebuilding it per test is ~45 MB each, and the footprint
    budget in tests/perf reads the whole process's high-water mark.
    """

    @lifespan
    async def stub(server):
        yield {
            "descriptions": catalogue_index,
            "backend": StubBackend(_WIDE_IMDATA),
        }

    monkeypatch.setattr(main.mcp, "_lifespan", stub)
    monkeypatch.delenv("NIWASHI_MCP_STRICT_ATTRIBUTES", raising=False)
    return main.mcp


async def _call(server, name: str, **arguments):
    async with Client(server) as client:
        result = await client.call_tool(name, arguments, raise_on_error=False)
    return result


@pytest.mark.asyncio
async def test_a_wrong_envelope_is_refused_so_the_checks_below_mean_something():
    """Control: if the client never validated, every test below would pass vacuously."""
    bad = FastMCP("control")

    @bad.tool
    async def broken() -> QueryEnvelope:
        return {"results": [], "returned": "three"}  # type: ignore[typeddict-item]

    async with Client(bad) as client:
        result = await client.call_tool("broken", {}, raise_on_error=False)

    assert result.is_error
    assert "total_available" in result.content[0].text


@pytest.mark.asyncio
async def test_query_default_path_validates_with_its_null_keys(served):
    result = await _call(served, "query", class_name="fvBD")

    assert not result.is_error, result.content
    body = result.structured_content
    assert body["next_page"] is None and body["note"] is None and body["fields"] is None
    assert body["returned"] == 2


@pytest.mark.asyncio
async def test_query_truncated_path_validates_with_a_page_and_a_note(served):
    result = await _call(served, "query", class_name="fvBD", limit=1)

    assert not result.is_error, result.content
    assert result.structured_content["truncated"] is True
    assert result.structured_content["next_page"] == 1
    assert "partial page" in result.structured_content["note"]


@pytest.mark.asyncio
async def test_query_projected_path_validates(served):
    result = await _call(
        served, "query", class_name="fvBD", fields=["name"], include_children=["fvSubnet"]
    )

    assert not result.is_error, result.content
    assert result.structured_content["fields"] == ["name"]
    for obj in result.structured_content["results"]:
        assert set(obj) - {"_children"} == {"name", "dn", "_class"}


@pytest.mark.asyncio
async def test_query_fields_sent_as_a_json_string_are_coerced(served):
    """Models send list arguments as JSON strings; `_JsonList` must unwrap this one."""
    result = await _call(served, "query", class_name="fvBD", fields=json.dumps(["name"]))

    assert not result.is_error, result.content
    assert result.structured_content["fields"] == ["name"]


@pytest.mark.asyncio
async def test_query_unknown_field_surfaces_as_a_tool_error_naming_the_fix(served):
    result = await _call(served, "query", class_name="fvBD", fields=["arpFlod"])

    assert result.is_error
    text = result.content[0].text
    assert "arpFlod" in text and "arpFlood" in text


@pytest.mark.asyncio
async def test_count_validates(served):
    result = await _call(served, "count", class_name="fvBD")

    assert not result.is_error, result.content
    assert result.structured_content == {
        "class_name": "fvBD", "count": 2, "scope_dn": None, "filters": {},
    }


@pytest.mark.asyncio
async def test_count_group_by_validates_when_complete(served):
    result = await _call(served, "count", class_name="fvBD", group_by="tenant")

    assert not result.is_error, result.content
    assert result.structured_content["complete"] is True
    assert result.structured_content["groups"] == {"OT": 2}
    assert result.structured_content["ungrouped"] == 0


@pytest.mark.asyncio
async def test_count_group_by_validates_when_capped_with_null_groups(
    monkeypatch, catalogue_index
):
    """The incomplete path returns `groups`/`ungrouped`/`note` as JSON null or a
    string. Declaring them non-nullable in `CountEnvelope` made FastMCP refuse
    this very response at the client — the honest "could not tally" became a
    tool error. Only a round trip through a real Client catches that."""

    @lifespan
    async def capped(server):
        yield {
            "descriptions": catalogue_index,
            "backend": StubBackend(_WIDE_IMDATA, cap_at=1),
        }

    monkeypatch.setattr(main.mcp, "_lifespan", capped)
    monkeypatch.delenv("NIWASHI_MCP_STRICT_ATTRIBUTES", raising=False)
    result = await _call(main.mcp, "count", class_name="fvBD", group_by="tenant")

    assert not result.is_error, result.content
    body = result.structured_content
    assert body["complete"] is False
    assert body["groups"] is None and body["ungrouped"] is None
    assert body["count"] == 2 and isinstance(body["note"], str)


@pytest.mark.asyncio
async def test_get_by_dn_projection_and_not_found_both_validate(served):
    found = await _call(served, "get_by_dn", dn="uni/tn-OT/BD-clients", fields=["name"])
    missing = await _call(served, "get_by_dn", dn="uni/tn-OT/BD-nope", fields=["name"])

    assert not found.is_error, found.content
    assert found.structured_content == {
        "name": "clients", "dn": "uni/tn-OT/BD-clients", "_class": "fvBD",
    }
    assert not missing.is_error, missing.content
    assert missing.structured_content["found"] is False
