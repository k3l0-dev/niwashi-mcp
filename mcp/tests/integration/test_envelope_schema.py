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

FastMCP 4 no longer validates a tool's output on the server, so the server does
it itself: `query` and `count` check their envelope against its TypedDict before
returning (`main._guarded`), and a slip is a tool error instead of a response a
client may or may not choke on. A key the TypedDict does not declare would
otherwise be dropped by serialisation without a trace.

So all of it is asserted here. The schema must name the keys, the guard must
refuse a wrong envelope, and every return path must validate against the schema
end to end — through a real in-memory MCP client, because that is what a caller
sees. Calling the tool function directly, as the other integration tests do,
never touches the schema.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from fastmcp.server.lifespan import lifespan

from niwashi_mcp import main
from niwashi_mcp.apic.client import QueryResult
from niwashi_mcp.main import CountEnvelope, QueryEnvelope
from tests.conftest import StubBackend
from tests.integration.test_tools import _WIDE_IMDATA

_QUERY_KEYS = {
    "results", "returned", "total_available", "truncated",
    "next_page", "complete", "note", "fields",
}
_COUNT_REQUIRED = {"class_name", "count", "scope_dn", "filters"}
_COUNT_OPTIONAL = {
    "warnings", "group_by", "groups", "ungrouped", "complete", "note", "group_names",
    "redacted",
}
# `query` adds `redacted` too, but as an optional key: the required set is what
# every response carries, and a result with nothing withheld carries no such key.
_QUERY_OPTIONAL = {"redacted"}


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
        assert set(QueryEnvelope.__optional_keys__) == _QUERY_OPTIONAL
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


class _SlippedBackend(StubBackend):
    """A backend whose answers break the envelope the tools build from them.

    `QueryResult.complete` is a bool and `count_class` returns an int; both are
    copied into the envelope as they come, so a backend that gets either wrong
    is how a real slip reaches the wire.
    """

    async def query_class(self, *args, **kwargs):
        result = await super().query_class(*args, **kwargs)
        return QueryResult(
            objects=result.objects,
            total_available=result.total_available,
            complete=None,  # type: ignore[arg-type]
        )

    async def count_class(self, *args, **kwargs):
        return str(await super().count_class(*args, **kwargs))  # type: ignore[return-value]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "key"), [("query", "complete"), ("count", "count")]
)
async def test_a_wrong_envelope_is_refused_so_the_checks_below_mean_something(
    monkeypatch, catalogue_index, tool, key
):
    """Control: if nothing validated, every test below would pass vacuously.

    This goes through the real tools, served in memory, with a backend that
    answers with the wrong type. FastMCP 3 refused such a return on the server;
    FastMCP 4 sends it, so the refusal here is the server's own guard — and the
    key it names is the one at fault.
    """

    @lifespan
    async def slipped(server):
        yield {"descriptions": catalogue_index, "backend": _SlippedBackend(_WIDE_IMDATA)}

    monkeypatch.setattr(main.mcp, "_lifespan", slipped)
    monkeypatch.delenv("NIWASHI_MCP_STRICT_ATTRIBUTES", raising=False)

    result = await _call(main.mcp, tool, class_name="fvBD")

    assert result.is_error
    text = result.content[0].text
    assert "breaks its own schema" in text and f"{key}:" in text


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


# ── the guard itself ──────────────────────────────────────────────────────────


def _query_envelope(**overrides):
    base = {
        "results": [{"dn": "uni/tn-A", "_class": "fvTenant"}],
        "returned": 1, "total_available": 1, "truncated": False,
        "next_page": None, "complete": True, "note": None, "fields": None,
    }
    return {**base, **overrides}


def _count_envelope(**overrides):
    base = {"class_name": "fvBD", "count": 2, "scope_dn": None, "filters": {}}
    return {**base, **overrides}


class TestTheGuard:
    def test_a_conforming_envelope_comes_back_as_the_same_object(self):
        """Only the verdict is wanted. Returning the validated copy would double
        the memory of a 5,000-object answer for nothing."""
        for tool, guard, envelope in (
            ("query", main._QUERY_GUARD, _query_envelope(redacted=3)),
            ("count", main._COUNT_GUARD, _count_envelope(group_by="tenant", complete=True)),
        ):
            assert main._guarded(tool, guard, envelope) is envelope

    @pytest.mark.parametrize(
        ("tool", "guard", "envelope"),
        [
            ("query", "_QUERY_GUARD", _query_envelope(surprise=1)),
            ("count", "_COUNT_GUARD", _count_envelope(surprise=1)),
        ],
    )
    def test_a_key_the_typed_dict_does_not_declare_is_refused(self, tool, guard, envelope):
        """FastMCP's serialisation would drop it silently; the client would never
        learn the server meant to say something."""
        with pytest.raises(ToolError, match=r"surprise: Extra inputs are not permitted"):
            main._guarded(tool, getattr(main, guard), envelope)

    @pytest.mark.parametrize(
        "envelope",
        [
            _query_envelope(returned="1"),    # a string where the client wants an integer
            _query_envelope(returned=True),   # and a bool is not an integer on the wire
            _query_envelope(truncated=None),  # null where the schema says boolean
            _query_envelope(next_page="2"),
            _query_envelope(results=["not an object"]),
        ],
    )
    def test_a_wrongly_typed_value_is_refused_without_coercion(self, envelope):
        with pytest.raises(ToolError, match="breaks its own schema"):
            main._guarded("query", main._QUERY_GUARD, envelope)

    def test_a_missing_required_key_is_refused(self):
        envelope = _count_envelope()
        del envelope["filters"]

        with pytest.raises(ToolError, match=r"filters: Field required"):
            main._guarded("count", main._COUNT_GUARD, envelope)

    def test_the_message_names_the_keys_and_never_the_values(self):
        """For `results` the value is the whole answer, and it can run to 5,000
        objects: it must not be echoed into an error."""
        envelope = _query_envelope(
            results=[{"dn": "uni/tn-A"}, "LEAKED-VALUE"], returned="LEAKED-COUNT"
        )

        with pytest.raises(ToolError) as caught:
            main._guarded("query", main._QUERY_GUARD, envelope)

        message = str(caught.value)
        assert "returned:" in message and "results.1:" in message
        assert "LEAKED" not in message and "uni/tn-A" not in message

    def test_a_flood_of_identical_problems_is_summarised(self):
        envelope = _query_envelope(results=["x"] * 50)

        with pytest.raises(ToolError) as caught:
            main._guarded("query", main._QUERY_GUARD, envelope)

        assert "(and 45 more)" in str(caught.value)
        assert len(str(caught.value)) < 700

    def test_the_guards_declare_exactly_the_keys_the_typed_dicts_do(self):
        """The guards subclass the advertised types, so they can only drift if
        someone redeclares a key — which this fails on."""
        for advertised, guard in (
            (QueryEnvelope, main._QueryEnvelopeGuard),
            (CountEnvelope, main._CountEnvelopeGuard),
        ):
            assert guard.__required_keys__ == advertised.__required_keys__
            assert guard.__optional_keys__ == advertised.__optional_keys__
