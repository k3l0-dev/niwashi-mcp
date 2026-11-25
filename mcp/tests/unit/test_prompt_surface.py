# Copyright (C) 2026 Khalid El-Ouiali — MONARK AIOPS srl
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
Tests for the prompt surface — the text this server ships to every agent.

Why this file exists
--------------------
Roughly 7,800 words reach an LLM on a normal session: the `mcp.instructions`
block, the five tool docstrings FastMCP forwards as tool descriptions, and
`client/SKILL.md`. Nothing imported any of it. Line coverage cannot help —
`instructions` is a single string literal, so `main.py` reported 98 % while its
largest agent-facing artifact was unexamined.

It had already rotted. Until this file existed, `get_schema`'s docstring told
every agent it read "the APIC jsonmeta schema file" and returned `{}` when "the
class file is not found in the local schema collection". Neither has existed
since 2.0 — the object model is a SQLite catalogue inside a dependency. And
`property_details` was documented as carrying a `mandatory` key that **zero of
332,297 properties** in the shipped catalogue can produce.

Documentation that lies to a human wastes their afternoon. Documentation that
lies to an agent becomes the agent's model of the system, and this whole server
exists to stop an agent answering confidently from a wrong model.

These tests pin the claims against the code that has to honour them. They are
deliberately narrow: they check facts that can be mechanically compared, not
prose quality.
"""

import ast
import inspect
import textwrap
from pathlib import Path

import pytest

from niwashi_mcp import main as main_mod
from niwashi_mcp.registry import catalog

pytestmark = pytest.mark.catalog

_TOOLS = ("search_classes", "get_schema", "query", "get_by_dn", "count")
_SKILL = Path(__file__).resolve().parents[2] / "client" / "SKILL.md"


def _doc(name: str) -> str:
    return inspect.getdoc(getattr(main_mod, name)) or ""


def _all_prompt_text() -> str:
    return main_mod.mcp.instructions + "\n" + "\n".join(_doc(t) for t in _TOOLS)


# ── the data layer the docs describe ─────────────────────────────────────────


def test_no_prompt_text_claims_the_server_reads_files_from_disk():
    """The 2.0 data layer is a catalogue in a dependency, not a directory.

    Any of these words reaching an agent gives it a wrong model of where its
    answers come from — and one that suggests a fix (point me at the files)
    that does not exist.
    """
    text = _all_prompt_text().lower()
    for phrase in ("jsonmeta", "schema file", "schema collection", "schemas dir", "data directory"):
        assert phrase not in text, (
            f"the prompt surface still tells an agent about {phrase!r}; "
            f"the object model has been a SQLite catalogue since 2.0"
        )


def test_skill_file_does_not_describe_a_schema_directory():
    """Same rule for SKILL.md, which is the larger half of the surface."""
    text = _SKILL.read_text().lower()
    for phrase in ("jsonmeta", "data/schemas", "schema collection"):
        assert phrase not in text, f"SKILL.md still refers to {phrase!r}"


# ── documented output fields must be producible ──────────────────────────────


def test_every_property_detail_key_documented_can_actually_appear():
    """A documented field an agent will never see teaches it to look for nothing.

    `mandatory` was documented on `property_details` and is unreachable: the
    flag bit exists in niwaki's layout but no property in the shipped catalogue
    sets it. Kept as a test rather than deleted from the projection, so that a
    future catalogue which *does* set it fails here and gets re-documented
    instead of silently appearing.
    """
    bits = catalog._flag_bits()
    total = catalog._query("SELECT COUNT(*) FROM prop")[0][0]
    with_bit = catalog._query(
        "SELECT COUNT(*) FROM prop WHERE flags & ?", (bits["mandatory"],)
    )[0][0]

    doc = _doc("get_schema")
    if with_bit == 0:
        assert '"mandatory"' not in doc, (
            f"get_schema documents a `mandatory` key, but 0 of {total} properties "
            f"in this catalogue set the bit — an agent is told to expect a field "
            f"it can never receive"
        )
    else:
        assert '"mandatory"' in doc, (
            f"{with_bit} of {total} properties now carry `mandatory` and it is "
            f"projected, but get_schema no longer documents it"
        )


# ── numbers quoted to the agent must match the code ──────────────────────────


def test_the_clamps_quoted_to_the_agent_match_the_code():
    """`query` and `search_classes` promise specific ranges; the code enforces them.

    These bounds are the only reason an agent can reason about how much it will
    get back. A change on either side without the other is the server describing
    a contract it does not keep.
    """
    src = inspect.getsource(main_mod)
    assert "max(1, min(limit, 200))" in src, "query's clamp moved"
    assert "max(1, min(limit, 50))" in src, "search_classes' clamp moved"
    assert "[1, 200]" in _doc("query")
    assert "[1, 50]" in _doc("search_classes")


def test_the_schema_list_bounds_quoted_match_the_constants():
    """`list_limit`'s default and ceiling are stated in prose and in code."""
    doc = _doc("get_schema")
    # Assert the numbers, not a phrasing — the prose may be reworded, the
    # contract may not change silently underneath it.
    assert f"default {main_mod._SCHEMA_LIST_SAMPLE}" in doc, "the default moved"
    assert f"1..{main_mod._SCHEMA_LIST_MAX}" in doc, "the ceiling moved"
    assert f"max {main_mod._SCHEMA_LIST_MAX}" in inspect.getsource(main_mod), (
        "the *Truncated note no longer tells the agent the ceiling"
    )


def test_the_envelope_keys_promised_are_the_keys_returned():
    """Every key `query` names in its docstring is one the tool actually sets.

    An agent reads this list and writes code against it. A renamed key that the
    docstring still advertises is a silent breakage in the caller, not here.
    """
    doc = _doc("query")
    returned = {
        "results", "returned", "total_available",
        "truncated", "next_page", "complete", "note", "fields",
    }
    for key in returned:
        assert f'"{key}"' in doc, f"query returns {key!r} but never documents it"

    # Look at the code with the docstring removed. The docstring spells out the
    # same `"key": value` shapes, so scanning the raw source would let the
    # documentation satisfy an assertion about the implementation — the test
    # would pass while the two disagreed, which is the one thing it exists to
    # catch. `inspect.getdoc` will not do for the strip: it returns the cleaned,
    # dedented text, which does not occur verbatim in the source.
    fn = ast.parse(textwrap.dedent(inspect.getsource(main_mod.query))).body[0]
    if isinstance(fn.body[0], ast.Expr) and isinstance(fn.body[0].value, ast.Constant):
        fn.body = fn.body[1:]
    body = ast.unparse(fn)
    for key in returned:
        assert f"'{key}':" in body or f'"{key}":' in body, (
            f"query documents {key!r} but never sets it"
        )


def test_the_group_by_fields_count_promises_are_the_fields_it_returns():
    """`count(group_by=...)` documents a breakdown envelope; the code must set it.

    An agent keys off `complete` and `groups` to decide whether it holds an
    answer, so a field the docstring names and the code never sets is an agent
    waiting on a value that cannot arrive. Same method as the `query` test: the
    docstring is stripped first, because it spells out the very `"key":` shapes
    being looked for and would satisfy the assertion by itself.
    """
    doc = _doc("count")
    keys = {"group_by", "groups", "ungrouped", "complete", "note", "warnings"}
    for key in keys:
        assert f'"{key}"' in doc, f"count returns {key!r} but never documents it"

    body = ""
    for fn_name in ("count", "_group_counts"):
        fn = ast.parse(textwrap.dedent(inspect.getsource(getattr(main_mod, fn_name))))
        fn = fn.body[0]
        if isinstance(fn.body[0], ast.Expr) and isinstance(
            fn.body[0].value, ast.Constant
        ):
            fn.body = fn.body[1:]
        body += ast.unparse(fn)
    # `warnings` is added by subscript assignment, the rest as dict literals.
    for key in keys - {"warnings"}:
        assert f"'{key}':" in body or f'"{key}":' in body, (
            f"count documents {key!r} but never sets it"
        )
    assert "response['warnings']" in body, "count documents `warnings` but never sets it"
    # The pseudo-key is part of the contract the docstring teaches.
    assert f'"{main_mod._TENANT_KEY}"' in doc


# ── the skill must describe the tools that exist ─────────────────────────────


def test_skill_names_exactly_the_tools_the_server_exposes():
    text = _SKILL.read_text()
    for tool in _TOOLS:
        assert f"`{tool}`" in text, f"SKILL.md never mentions {tool}"


def test_skill_invents_no_tool_parameter():
    """Every `name=` in a SKILL.md tool call must be a real parameter.

    A kwarg the server does not accept is an error the agent cannot diagnose:
    it looks like the server rejecting a reasonable request.
    """
    import re

    text = _SKILL.read_text()
    real = {
        tool: set(inspect.signature(getattr(main_mod, tool)).parameters)
        for tool in _TOOLS
    }
    bad: list[str] = []
    for tool in _TOOLS:
        for call in re.findall(rf"{tool}\(([^)]*)\)", text):
            for kwarg in re.findall(r"(\w+)\s*=", call):
                if kwarg not in real[tool]:
                    bad.append(f"{tool}({kwarg}=…)")
    assert not bad, f"SKILL.md passes parameters that do not exist: {sorted(set(bad))}"


# ── the vocabulary lint ───────────────────────────────────────────────────────
#
# SKILL.md teaches by example, and its examples are full of class and property
# names. A typo'd class name in a teaching example is worse than one in code:
# code fails, but an agent imitates the example verbatim, queries the mistyped
# name, and the APIC answers an empty result indistinguishable from "none".
# The docs went stale exactly this way once already ("the APIC jsonmeta schema
# file"); names rot the same way words do.


def test_every_aci_name_skill_teaches_actually_exists():
    """Every backticked camelCase token is a class, a property, or allowlisted.

    Checked against the shipped catalogue itself, so the lint follows the
    corpus: a class SKILL.md cites that disappears from a future catalogue
    fails here the day the dependency is bumped, not the day a user's query
    comes back empty.
    """
    import re

    text = _SKILL.read_text()
    candidates = set(re.findall(r"`([a-z][a-z0-9]*[A-Z][A-Za-z0-9]*)`", text))
    assert len(candidates) > 30, (
        f"only {len(candidates)} camelCase tokens found — the extraction regex "
        f"no longer matches how SKILL.md marks names, so the lint is blind"
    )

    classes = {r[0] for r in catalog._query("SELECT class_name FROM mo")}
    properties = {r[0] for r in catalog._query("SELECT DISTINCT wire_name FROM prop")}

    # Every entry must carry its reason, and — asserted below — must still be
    # in use. An allowlist that only grows is how a lint stops linting.
    allowlist = {
        # get_schema's own output keys, quoted when teaching the schema shape:
        "identifiedBy", "rnFormat", "dnFormats", "containedBy", "relationTo",
        "relationFrom", "isAbstract", "sourceClass",
        # the *Truncated marker get_schema adds beside a sampled list:
        "containedByTruncated",
        # deliberate wrong-case example teaching that lookup is case-sensitive:
        "fvBd",
        # notation for colon-flattening ("`pkg:Class` → `pkgClass`"), not a name:
        "pkgClass",
    }

    unknown = sorted(
        c for c in candidates
        if c not in classes and c not in properties and c not in allowlist
    )
    assert not unknown, (
        f"SKILL.md teaches names that are neither a class nor a property in the "
        f"shipped catalogue: {unknown}. An agent will imitate them verbatim and "
        f"read the empty result as 'there are none'."
    )

    stale = sorted(a for a in allowlist if a not in candidates)
    assert not stale, (
        f"allowlist entries no longer used by SKILL.md: {stale} — prune them, "
        f"or the allowlist rots into a bypass"
    )


def test_the_wrong_case_example_is_still_wrong():
    """`fvBd` is allowlisted as a deliberate error. Verify it stays one.

    If a future catalogue ever contained a real `fvBd`, the teaching example
    would silently become a valid class and the case-sensitivity lesson would
    teach the opposite of what it says.
    """
    from niwashi_mcp.registry.catalog import class_exists

    assert not class_exists("fvBd")
    assert class_exists("fvBD")


# ── attribute validation: what the docstrings promise must be real ───────────


def test_the_attribute_validation_surface_is_documented_and_implemented():
    """`query` and `count` tell an agent what is refused, and what a relaxed
    server adds to its answer. Each claim is pinned to the code that keeps it.

    The relaxed-mode additions matter most: `count` promises a `warnings` key
    that exists *only* in that mode, and `query` promises it lands in `note`.
    An agent that reads a zero next to a warning knows the name may be wrong;
    one whose docs never mentioned the key would read the zero as an answer.
    """
    var = "NIWASHI_MCP_STRICT_ATTRIBUTES"
    assert var in inspect.getsource(main_mod._strict_attributes)

    for tool in ("query", "count"):
        doc = _doc(tool)
        assert "UnknownAttributeError" in doc, f"{tool} no longer documents it"
        assert var in doc, f"{tool} does not name the switch that relaxes it"
    assert "UnknownAttributeError" in inspect.getsource(main_mod._attribute_problems)

    assert '"warnings"' in _doc("count")
    # Look at the code with the docstring removed, as the envelope test does:
    # the docstring spells the key out, so scanning raw source would let the
    # documentation satisfy an assertion about the implementation.
    fn = ast.parse(textwrap.dedent(inspect.getsource(main_mod.count))).body[0]
    fn.body = fn.body[1:]
    assert "'warnings'" in ast.unparse(fn), "count documents `warnings` but never sets it"

    # query reports the same condition through its existing `note`, not a new key.
    assert "unchecked" in _doc("query")
    assert "attribute_warnings" in inspect.getsource(main_mod.query)


# ── the fields / group_by / validation / fetch_all surface ───────────────────
#
# These pin what SKILL.md and the server instructions now teach about `fields`,
# `count(group_by=...)`, attribute validation and fetch_all's real limits. Each
# was a claim that went stale the moment the code changed: SKILL.md said "a
# filter on an unknown attribute returns [] silently", "`fetch_all` uses `limit`
# as the page size" and "`count` gives a scalar tally", and an agent reading
# those would plan around behaviour the server no longer has.


def _code_without_docstrings(*fns) -> str:
    """The unparsed body of each function, docstring removed.

    A docstring spells out the very `"key":` shapes the tests look for, so
    scanning raw source would let the documentation satisfy an assertion about
    the implementation (see test_the_envelope_keys_promised_are_the_keys_returned).
    """
    body = ""
    for fn in fns:
        node = ast.parse(textwrap.dedent(inspect.getsource(fn))).body[0]
        if isinstance(node.body[0], ast.Expr) and isinstance(
            node.body[0].value, ast.Constant
        ):
            node.body = node.body[1:]
        body += ast.unparse(node)
    return body


def test_skill_teaches_each_new_parameter_in_a_call_that_exists():
    """`fields` and `group_by` are shown in a real call, on tools that take them.

    `test_skill_invents_no_tool_parameter` proves every kwarg SKILL.md shows is
    real; this is its converse for the new ones — the skill must actually show
    them, or an agent never learns the parameter exists.
    """
    import re

    text = _SKILL.read_text()
    for tool, param in (
        ("query", "fields"),
        ("get_by_dn", "fields"),
        ("count", "group_by"),
    ):
        assert param in inspect.signature(getattr(main_mod, tool)).parameters
        assert re.search(rf"{tool}\([^)]*\b{param}=", text), (
            f"SKILL.md never shows {tool}(..., {param}=...)"
        )


def test_skill_envelope_table_has_a_row_for_every_key_query_returns():
    """The envelope table is how an agent learns what each key means.

    `fields` was added to every envelope — a change to a result that did not
    otherwise change — and a table that omits it leaves an agent reading an
    unexplained key. Compared against the TypedDict the server advertises as its
    `outputSchema`, so the next key added there fails here too.
    """
    import re

    text = _SKILL.read_text()
    keys = set(main_mod.QueryEnvelope.__annotations__)
    assert "fields" in keys, "QueryEnvelope no longer has the key this test is about"
    for key in sorted(keys):
        assert re.search(rf"^\| `{key}` \|", text, re.MULTILINE), (
            f"SKILL.md's envelope table has no row for {key!r}"
        )
        assert f'"{key}"' in text, f"SKILL.md's envelope example never shows {key!r}"


def test_skill_names_every_key_count_can_return_and_the_code_sets_them():
    """`count(group_by=...)` answers with keys an agent must read to be safe.

    `complete` and a null `groups` are what stop a partial breakdown being
    reported as a finding, so each has to be named in the skill *and* actually
    set by the tool. The docstring is stripped before looking at the code, as in
    the other envelope tests.
    """
    text = _SKILL.read_text()
    for key in main_mod.CountEnvelope.__annotations__:
        assert f'"{key}"' in text or f"`{key}`" in text, (
            f"SKILL.md never mentions the {key!r} key count returns"
        )

    body = _code_without_docstrings(main_mod.count, main_mod._group_counts)
    for key in ("group_by", "groups", "ungrouped", "complete", "note"):
        assert f"'{key}':" in body, f"count documents {key!r} but never sets it"
    # What the skill promises about a withheld breakdown must be what the code does.
    assert "'groups': None" in body and "'ungrouped': None" in body, (
        "SKILL.md says an incomplete breakdown has null groups and ungrouped"
    )
    assert f'"{main_mod._TENANT_KEY}"' in text, "SKILL.md never teaches group_by='tenant'"


def test_every_exception_skill_names_exists():
    """An error name in backticks is what the agent matches a failure against.

    Lints the same way the ACI vocabulary is linted: a renamed or misspelt
    exception in the skill is a tool error the agent cannot recognise. The two
    named here are the ones it is told to act on.
    """
    import re

    from niwashi_mcp import exceptions

    named = set(re.findall(r"`([A-Z][A-Za-z]+Error)`", _SKILL.read_text()))
    assert {"UnknownClassError", "UnknownAttributeError"} <= named
    for name in sorted(named):
        assert isinstance(getattr(exceptions, name, None), type), (
            f"SKILL.md names {name!r}, which niwashi_mcp.exceptions does not define"
        )


def test_the_switch_skill_names_is_the_one_the_server_reads(monkeypatch):
    """SKILL.md tells an agent the operator can set `NIWASHI_MCP_STRICT_ATTRIBUTES=false`.

    The name must be the variable the code reads, and `false` must be a value it
    treats as relaxed — otherwise the instruction sends an operator to a switch
    that does nothing, and the agent keeps reporting refusals as release gaps.
    """
    import re

    named = set(re.findall(r"NIWASHI_MCP_[A-Z_]+", _SKILL.read_text()))
    assert named == {"NIWASHI_MCP_STRICT_ATTRIBUTES"}, named
    (name,) = named
    assert f'"{name}"' in inspect.getsource(main_mod._strict_attributes)
    assert f"{name}=false" in _SKILL.read_text()
    monkeypatch.setenv(name, "false")
    assert main_mod._strict_attributes() is False
    monkeypatch.delenv(name)
    assert main_mod._strict_attributes() is True, "the default is no longer strict"


def test_the_fetch_all_numbers_quoted_match_the_client_constants():
    """The 200-per-page and 5,000-object limits are quoted in three places.

    They are the only reason an agent can reason about whether a `fetch_all`
    result is whole, and they used to be wrong in a way that mattered: the code
    paged by `limit`, so the default capped a "fetch everything" call at 500.
    """
    import re

    from niwashi_mcp.apic import client as client_mod

    cap = f"{client_mod._MAX_OBJECTS:,}"
    page = client_mod._FETCH_ALL_PAGE_SIZE
    assert main_mod._MAX_OBJECTS == client_mod._MAX_OBJECTS

    sources = {
        "SKILL.md": _SKILL.read_text(),
        "query docstring": _doc("query"),
        "server instructions": main_mod.mcp.instructions,
    }
    for where, text in sources.items():
        assert cap in text, f"{where} no longer states the {cap}-object cap"
        assert re.search(rf"\b{page}\b[^.]*\bpage|\bpage[^.]*\b{page}\b", text), (
            f"{where} no longer states the {page}-object page size"
        )
        for number in re.findall(r"(\d[\d,]*) objects per (?:call|page)", text):
            assert number in {cap, str(page)}, (
                f"{where} quotes {number!r} objects per call/page; the code has "
                f"{cap} per call and {page} per page"
            )


def test_prompt_text_no_longer_makes_claims_the_code_stopped_keeping():
    """Each phrase was true before this branch and is false now.

    A negative lint is a weak instrument — it only catches the sentence it was
    written for — so each entry is the exact stale claim found by grepping, and
    says what replaced it.
    """
    import re

    skill = _SKILL.read_text()
    everywhere = skill + "\n" + _all_prompt_text()
    stale_everywhere = {
        r"(?:uses|using) `?limit`? as (?:the )?page[- ]size": (
            "fetch_all pages at a fixed 200, whatever `limit` is"
        ),
        r"scalar tally": "count can break the tally down with group_by",
    }
    for pattern, replacement in stale_everywhere.items():
        assert not re.search(pattern, everywhere), (
            f"stale claim {pattern!r} is back; now: {replacement}"
        )
    assert not re.search(r"not in `properties` returns", skill), (
        "SKILL.md again says an unknown attribute returns [] — the server refuses it"
    )
    assert "silently returns nothing" not in main_mod.mcp.instructions, (
        "the instructions again say an unknown attribute silently returns nothing"
    )


def test_server_instructions_teach_the_new_behaviour():
    """The instructions are what an agent reads when it has no skill file.

    Every name is checked against the code that gives it meaning, so the lines
    cannot survive a rename: `group_by` and `fields` must be real parameters, and
    the keys the counting paragraph tells an agent to read must be ones `count`
    sets.
    """
    text = main_mod.mcp.instructions
    assert "group_by" in inspect.signature(main_mod.count).parameters
    assert "fields" in inspect.signature(main_mod.query).parameters
    for needle in (
        "group_by",
        '"ungrouped"',
        "complete=false",
        "UnknownAttributeError",
        "fields=[...]",
        '"fields"',  # the envelope key list
    ):
        assert needle in text, f"the server instructions no longer mention {needle!r}"
    assert '"ungrouped"' in _code_without_docstrings(main_mod._group_counts).replace(
        "'", '"'
    )
    # Every key of the query envelope is in the list FULL-FABRIC AGGREGATION gives.
    for key in main_mod.QueryEnvelope.__annotations__:
        assert f'"{key}"' in text, f"the instructions' envelope list lacks {key!r}"
