# Copyright (C) 2026 Khalid El-Ouiali — MONARK AIOPS srl
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
The MCP contract — what every client is handed, asserted without a network.

Why this file exists
--------------------
An entire class of defect had no coverage: what the server *advertises*. Every
other test drives a tool function directly, in Python, with a stub context. None
of them goes through FastMCP's registry, so none sees the tool list a client
receives — the names, the titles, the annotations, the parameter schemas, the
required fields, or the `ctx` parameter that must never be exposed.

That surface is not internal. It is the entire integration contract, and today
showed twice that clients enforce it differently: Claude Desktop silently skips
a server entry it does not understand, and OpenCode registers the same server
twice under two names without complaining. A defect here does not raise — the
tool simply stops being offered, or is offered with a shape the caller cannot
satisfy.

These tests use FastMCP's own registry rather than reading source, so they break
when the *advertised* contract breaks, not when the code that produces it is
merely reorganised. No APIC, no HTTP, no lifespan.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from niwashi_mcp.main import mcp

_TOOLS = {"search_classes", "get_schema", "query", "get_by_dn", "count"}


def _hint(tool, name: str):
    """One annotation hint, read the way it travels on the wire.

    `model_dump(by_alias=True)` yields the spec's camelCase keys (`readOnlyHint`)
    whatever the attribute is called on the model. The MCP SDK renamed those
    attributes to snake_case in v2 and keeps the old spelling only behind a
    deprecation bridge, so reading `tool.annotations.readOnlyHint` asserts the
    bridge rather than the contract — and stops working when the bridge goes. The
    wire form is the contract, and it is the same on both SDK generations.
    """
    return tool.annotations.model_dump(by_alias=True)[name]

# Tools that reach the fabric, and therefore interact with an open world.
_FABRIC = {"query", "get_by_dn", "count"}


@pytest.fixture(scope="module")
def advertised() -> dict:
    """The tools as a client sees them, from FastMCP's registry."""
    import asyncio

    return {name: asyncio.run(mcp.get_tool(name)) for name in _TOOLS}


class TestTheAdvertisedSurface:
    def test_exactly_five_tools_and_no_others(self, advertised):
        """The five-tool surface is the design, not an accident.

        A sixth tool appearing — a helper someone decorated by habit — changes
        what every agent has to choose between, and the whole premise is that a
        small generic surface beats a large specific one.
        """
        import asyncio

        listed = {t.name for t in asyncio.run(mcp._list_tools())}
        assert listed == _TOOLS, f"advertised tool set changed: {sorted(listed)}"

    def test_the_injected_context_is_never_advertised(self, advertised):
        """`ctx` is FastMCP's injection, not a parameter a caller may pass.

        Every tool takes `ctx: Context` in its signature. If it ever reached the
        published schema, a client would see a required argument it cannot
        construct, and the tool would be unusable — while every existing test,
        which calls the function directly, kept passing.

        Honest about what this guards: FastMCP excludes `ctx` by its *type*, so
        no change on our side reproduces the failure — giving the parameter a
        default does not leak it. This is a guard against the dependency
        changing that behaviour, not against our own code, and it is cheap
        enough to keep for that alone.
        """
        for name, tool in advertised.items():
            props = tool.parameters["properties"]
            assert "ctx" not in props, f"{name} advertises its injected context"

    def test_every_tool_declares_itself_read_only(self, advertised):
        """This server only ever issues GETs; clients should not have to guess.

        A client that does not know a tool is safe must assume it is not, and
        prompts on every call. An agent answering one question makes a dozen.
        """
        for name, tool in advertised.items():
            ann = tool.annotations
            assert ann is not None, f"{name} carries no annotations"
            assert _hint(tool, "readOnlyHint") is True, f"{name} does not declare itself read-only"

    def test_open_world_separates_local_reads_from_fabric_reads(self, advertised):
        """`search_classes` and `get_schema` never leave the process."""
        for name, tool in advertised.items():
            expected = name in _FABRIC
            got = _hint(tool, "openWorldHint")
            assert got is expected, f"{name}: openWorldHint={got}, expected {expected}"

    def test_hints_meaningless_under_read_only_are_absent(self, advertised):
        """The MCP spec defines `destructiveHint` and `idempotentHint` as
        meaningful only when `readOnlyHint` is false. Declaring them anyway is
        noise that reads as rigour."""
        for name, tool in advertised.items():
            assert _hint(tool, "destructiveHint") is None, name
            assert _hint(tool, "idempotentHint") is None, name

    def test_every_tool_has_a_human_title_and_a_description(self, advertised):
        """Both are what a client renders in a tool picker."""
        for name, tool in advertised.items():
            assert tool.title, f"{name} has no title"
            assert tool.description and len(tool.description) > 200, (
                f"{name}'s description is {len(tool.description or '')} chars — "
                f"it is the tool's entire instruction manual for the agent"
            )


class TestParameterSchemas:
    """What a caller must supply, and what it may."""

    REQUIRED: ClassVar[dict[str, list[str]]] = {
        "search_classes": ["keyword"],
        "get_schema": ["class_name"],
        "query": ["class_name"],
        "get_by_dn": ["dn"],
        "count": ["class_name"],
    }

    def test_required_arguments_are_exactly_the_identifying_ones(self, advertised):
        """Anything else required would make the common call fail for a client
        that omits an optional it has no opinion about."""
        for name, expected in self.REQUIRED.items():
            got = advertised[name].parameters.get("required", [])
            assert got == expected, f"{name} requires {got}, expected {expected}"

    def test_optional_arguments_are_nullable_rather_than_absent(self, advertised):
        """`filters`, `scope_dn` and friends default to None, not to a sentinel.

        An LLM omits what it does not need; a schema that forbids null forces it
        to invent a value.
        """
        query = advertised["query"].parameters["properties"]
        for opt in ("filters", "scope_dn", "order_by", "include_children"):
            schema = query[opt]
            assert "anyOf" in schema or schema.get("default") is None, (
                f"query.{opt} is not expressible as absent"
            )

    def test_the_json_string_coercion_is_advertised_where_it_applies(self, advertised):
        """`filters` and `include_children` accept a JSON *string* as well as a
        native object, because models send both. If the schema stopped saying so,
        a strict client would reject the string form before it ever reached the
        coercion."""
        props = advertised["query"].parameters["properties"]
        for name in ("filters", "include_children"):
            assert "anyOf" in props[name] or props[name].get("type"), (
                f"query.{name} advertises no type at all"
            )

    def test_limits_are_integers_with_defaults_a_caller_can_omit(self, advertised):
        assert advertised["query"].parameters["properties"]["limit"]["default"] == 20
        assert advertised["search_classes"].parameters["properties"]["limit"]["default"] == 10
        assert advertised["get_schema"].parameters["properties"]["list_limit"]["default"] == 25


class TestNoListenStreams:
    """The server publishes no change notification, so it offers no stream for one.

    FastMCP 4.2.5 turned `subscriptions/listen` on by default: a supporting client
    holds a stream open for as long as it is connected. Five fixed tools never
    change, so the stream would carry nothing and cost a connection.
    """

    @pytest.fixture
    def capabilities(self, monkeypatch):
        import asyncio

        from fastmcp import Client
        from fastmcp.server.lifespan import lifespan

        from niwashi_mcp import main

        @lifespan
        async def stub(server):
            yield {}

        monkeypatch.setattr(main.mcp, "_lifespan", stub)

        async def read(mode: str) -> tuple[str, dict]:
            async with Client(main.mcp, mode=mode) as client:
                return client.protocol_version, client.server_capabilities.model_dump(
                    by_alias=True
                )

        return lambda mode: asyncio.run(read(mode))

    def test_the_2026_07_28_era_advertises_no_subscription_and_no_list_changes(
        self, capabilities
    ):
        """The era the stream belongs to: `resources.subscribe` and `listChanged`
        are what tell a client it may open one."""
        # "auto" is what negotiates the era through `server/discover`; pinning the
        # version string skips that and yields no capabilities to read.
        version, caps = capabilities("auto")

        assert version == "2026-07-28", "the client did not negotiate the era under test"
        assert caps["resources"]["subscribe"] is False
        for capability in ("tools", "resources", "prompts"):
            assert caps[capability]["listChanged"] is False, capability

    def test_the_handshake_era_never_advertised_a_subscription(self, capabilities):
        """Unaffected either way upstream; pinned so a change there is noticed."""
        version, caps = capabilities("legacy")

        assert version != "2026-07-28"
        assert caps["resources"]["subscribe"] is False


class TestServerIdentity:
    def test_the_server_announces_the_distribution_name(self):
        """It announced `aci-mcp` for five days after the 2.0 rename, because a
        stale process was serving and nothing checked. The name a client sees is
        how it is identified in logs, configs and bug reports."""
        assert mcp.name == "niwashi-mcp"

    def test_instructions_are_shipped_and_carry_the_workflow(self):
        """The instructions block is the largest single artifact sent to an
        agent. Empty or truncated, tools still work and answers quietly get
        worse — the discovery order stops being taught."""
        text = mcp.instructions
        assert text and len(text) > 4_000, f"instructions are {len(text or '')} chars"
        for anchor in (
            "search_classes",
            "get_schema",
            "MANDATORY DISCOVERY WORKFLOW",
            "GROUNDING",
            "RELATION INTEGRITY",
            "FULL-FABRIC AGGREGATION",
        ):
            assert anchor in text, f"instructions no longer mention {anchor!r}"
