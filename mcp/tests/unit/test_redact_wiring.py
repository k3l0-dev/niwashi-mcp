# Copyright (C) 2026 Khalid El-Ouiali — MONARK AIOPS srl
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
The redaction policy is wired in, and what the agent is told about it is true.

`niwashi_mcp/redact.py` is tested on its own; these pin the two ways it can be
quietly switched off. One is an edit that drops the startup call, so the server
runs with a policy nobody checked. The other is the prompt surface drifting from
the code: an agent told that a withheld value reads `<redacted>`, that a refusal
is a `SecretAttributeError`, that `get_schema` marks `secret: true` — each a
claim the code has to keep.
"""

import inspect
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import niwashi_mcp.main as main
from niwashi_mcp import exceptions, redact
from niwashi_mcp.exceptions import RedactionPolicyError
from tests.unit.test_prompt_surface import _code_without_docstrings, _doc

_SKILL = Path(__file__).resolve().parents[2] / "client" / "SKILL.md"


def _env(monkeypatch):
    monkeypatch.setenv("APIC_HOST", "10.0.0.1")
    monkeypatch.setenv("APIC_PASSWORD", "x")


# ── startup ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lifespan_verifies_the_redaction_policy(monkeypatch):
    """The guard is worthless if the lifespan never calls it."""
    _env(monkeypatch)
    with patch("niwashi_mcp.main.load_dotenv"), \
         patch("niwashi_mcp.main.catalog.descriptions_index", return_value={}), \
         patch("niwashi_mcp.main.ApicClient", return_value=AsyncMock()), \
         patch("niwashi_mcp.main.redact.verify_policy") as verify:
        async with main.app_lifespan(MagicMock()):
            pass

    verify.assert_called_once()


@pytest.mark.asyncio
async def test_a_moved_policy_stops_startup_before_an_apic_session_opens(monkeypatch):
    """A server that cannot say which values are secret must not be one that is connected.

    Raising after `authenticate()` would leave the failure behind a session the
    process then has to close; raising before it means nothing was opened.
    """
    _env(monkeypatch)
    backend = AsyncMock()
    constructed = MagicMock(return_value=backend)

    with patch("niwashi_mcp.main.load_dotenv"), \
         patch("niwashi_mcp.main.catalog.descriptions_index", return_value={}), \
         patch("niwashi_mcp.main.ApicClient", constructed), \
         patch(
             "niwashi_mcp.main.redact.verify_policy",
             side_effect=RedactionPolicyError("niwaki 2.3 moved SECRET_VALUE_POSITIONS"),
         ), pytest.raises(RedactionPolicyError, match="SECRET_VALUE_POSITIONS"):
        async with main.app_lifespan(MagicMock()):
            pass

    constructed.assert_not_called()
    backend.authenticate.assert_not_called()


@pytest.mark.asyncio
async def test_the_policy_is_verified_after_the_catalogue_and_before_the_index(monkeypatch):
    """It resolves against the catalogue, so it cannot run first; and the index
    walk is the first thing that would read a damaged one."""
    _env(monkeypatch)
    calls: list[str] = []

    with patch("niwashi_mcp.main.load_dotenv"), \
         patch("niwashi_mcp.main.ApicClient", return_value=AsyncMock()), \
         patch(
             "niwashi_mcp.main.catalog.verify_catalogue",
             side_effect=lambda: calls.append("catalogue"),
         ), \
         patch(
             "niwashi_mcp.main.redact.verify_policy",
             side_effect=lambda: calls.append("policy"),
         ), \
         patch(
             "niwashi_mcp.main.catalog.descriptions_index",
             side_effect=lambda: calls.append("index") or {},
         ):
        async with main.app_lifespan(MagicMock()):
            pass

    assert calls == ["catalogue", "policy", "index"]


# ── the prompt surface ────────────────────────────────────────────────────────


def test_every_tool_that_refuses_documents_the_error_it_raises():
    for tool, errors in (
        ("query", ("SecretAttributeError", "SecretDnError")),
        ("count", ("SecretAttributeError", "SecretDnError")),
        ("get_by_dn", ("SecretDnError",)),
    ):
        for name in errors:
            assert name in _doc(tool), f"{tool} no longer documents {name}"
            assert isinstance(getattr(exceptions, name), type)


def test_the_marker_and_the_disclosure_keys_are_documented_and_set():
    """`<redacted>`, `redacted` and `_redacted` are what an agent reads off a result."""
    for tool in ("query", "get_by_dn"):
        assert "<redacted>" in _doc(tool)
    assert '"redacted"' in _doc("query") and '"redacted"' in _doc("count")
    assert '"_redacted"' in _doc("get_by_dn")
    assert redact.REDACTED == "<redacted>"

    assert "'redacted'" in _code_without_docstrings(main.query)
    assert "'redacted'" in _code_without_docstrings(main._group_counts)
    assert "'_redacted'" in _code_without_docstrings(main._with_disclosure)
    # Present only when something was withheld: asserted by behaviour in
    # tests/integration/test_redaction_tools.py, and here by shape.
    assert "redacted" in main.QueryEnvelope.__optional_keys__
    assert "redacted" in main.CountEnvelope.__optional_keys__
    assert "redacted" not in main.QueryEnvelope.__required_keys__


def test_get_schema_documents_the_secret_label_and_the_code_sets_it():
    assert '"secret": true' in _doc("get_schema")
    assert '"scrubbed": true' in _doc("get_schema")
    assert "'secret'" in _code_without_docstrings(redact.mark_secret_properties)
    assert "'scrubbed'" in _code_without_docstrings(redact.mark_secret_properties)
    assert "mark_secret_properties" in _code_without_docstrings(main.get_schema)


def test_the_server_instructions_teach_the_policy():
    text = main.mcp.instructions
    for needle in ("SECRETS", "<redacted>", '"redacted"', "SecretAttributeError",
                   "SecretDnError", '"secret": true', "best-effort", "changeSet", "empty secret"):
        assert needle in text, f"the instructions no longer say {needle!r}"
    for name in re.findall(r"`?(Secret\w+Error)`?", text):
        assert isinstance(getattr(exceptions, name, None), type), name


def test_skill_teaches_the_policy_in_terms_the_code_keeps():
    text = _SKILL.read_text()
    for needle in ("Secrets — withheld, never a filter", "<redacted>", "`redacted`",
                   "`_redacted`", "SecretAttributeError", "SecretDnError", '"secret"',
                   "`secret`", "`scrubbed`", "best effort", "`changeSet`", "`aaaSessionLR.sessionId`"):
        assert needle in text, f"SKILL.md no longer teaches {needle!r}"


def test_the_refusal_is_not_governed_by_the_attribute_switch():
    """The claim SKILL.md and the docstrings make, pinned to the code: the secret
    check runs before, and independently of, `_enforce_attributes`."""
    for fn in (main.query, main.count):
        body = _code_without_docstrings(fn)
        assert "_refuse_secrets(" in body
        assert body.index("_refuse_secrets(") < body.index("_enforce_attributes(")
    assert "_strict_attributes" not in inspect.getsource(main._refuse_secrets)
    assert "_strict_attributes" not in inspect.getsource(redact)
    assert "redact.check_dn" in _code_without_docstrings(main.get_by_dn)
