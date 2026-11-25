# Copyright (C) 2026 Khalid El-Ouiali — MONARK AIOPS srl
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
The ACI skill, served by the server it describes — EXPERIMENTAL.

Why this file exists
--------------------
`mcp/client/SKILL.md` is the operating guide that has to sit beside the five
tools: without it an agent has the tools and none of the object model, which is
the failure the file exists to prevent. Until now the user had to copy it next to
the client configuration by hand. FastMCP 4.2 can serve a skill as MCP resources,
so a client that supports it fetches the guide from the server it is already
connected to.

This is experimental on FastMCP's side — the Skills extension (SEP-2640) is a
draft, and its API and wire format may change in a release without notice. So the
surface is pinned here, narrowly: what is listed, what can be read, and that what
is read is the file people edit. A FastMCP release that moves any of it fails
these tests instead of the user's agent.

The skill is shipped as package data in `src/niwashi_mcp/skills/niwashi-mcp-query/`
because a wheel carries nothing outside the package, and because FastMCP requires
the folder to be named after the skill. `client/SKILL.md` remains the file read
and edited; the packaged copy has to be byte-identical, and the first test below
is what makes that a rule rather than a hope.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from importlib import resources
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.server.lifespan import lifespan

from niwashi_mcp import main

_CLIENT_COPY = Path(__file__).resolve().parents[2] / "client" / "SKILL.md"
_PACKAGED = resources.files("niwashi_mcp").joinpath(
    "skills", "niwashi-mcp-query", "SKILL.md"
)
_NAME = "niwashi-mcp-query"
_URI = f"skill://{_NAME}/SKILL.md"
_MANIFEST = f"skill://{_NAME}/_manifest"


class TestTheCopiesAgree:
    def test_the_packaged_skill_is_byte_identical_to_the_client_copy(self):
        """Two files, one skill. `client/SKILL.md` is the one documented and edited;
        the packaged one is what the wheel ships and the server serves. Diverging,
        the server would hand an agent a guide the repository no longer contains —
        silently, since both are valid."""
        assert _PACKAGED.read_bytes() == _CLIENT_COPY.read_bytes(), (
            "the packaged skill differs from mcp/client/SKILL.md. Edit "
            "client/SKILL.md, then run, from mcp/:\n"
            "  cp client/SKILL.md src/niwashi_mcp/skills/niwashi-mcp-query/SKILL.md"
        )

    def test_the_folder_is_named_after_the_skill(self):
        """FastMCP and the Agent Skills spec refuse a skill whose folder name is
        not its `name`; the server would not start."""
        frontmatter = _CLIENT_COPY.read_text(encoding="utf-8").split("---")[1]

        assert f"name: {_NAME}\n" in frontmatter
        assert _PACKAGED.parent.name == _NAME


@pytest.fixture
def listed(monkeypatch):
    """What a client of the real `main.mcp` is handed, in memory, per protocol era."""

    @lifespan
    async def stub(server):
        yield {}

    monkeypatch.setattr(main.mcp, "_lifespan", stub)

    def run(mode: str):
        async def go():
            async with Client(main.mcp, mode=mode) as client:
                uris = {str(r.uri): r for r in await client.list_resources()}
                skill = (await client.read_resource(_URI))[0]
                manifest = (await client.read_resource(_MANIFEST))[0]
                capabilities = client.server_capabilities.model_dump(by_alias=True)
                return uris, skill, manifest, capabilities

        return asyncio.run(go())

    return run


@pytest.mark.parametrize("mode", ["auto", "legacy"])
class TestTheResourceSurface:
    def test_the_skill_is_listed(self, listed, mode):
        uris, *_ = listed(mode)

        assert _URI in uris
        assert uris[_URI].mime_type == "text/markdown"
        assert _MANIFEST in uris

    def test_nothing_else_is_published(self, listed, mode):
        """A second skill, or a stray file, would be offered to every client."""
        uris, *_ = listed(mode)

        assert set(uris) == {_URI, _MANIFEST}

    def test_it_can_be_read_and_is_the_client_copy(self, listed, mode):
        _, skill, *_ = listed(mode)

        assert skill.text == _CLIENT_COPY.read_text(encoding="utf-8")

    def test_the_manifest_describes_the_file_served(self, listed, mode):
        """What a client verifies a download against: size and digest of the
        bytes, which are the bytes of `client/SKILL.md`."""
        _, _, manifest, _ = listed(mode)
        body = json.loads(manifest.text)
        raw = _CLIENT_COPY.read_bytes()

        assert body["skill"] == _NAME
        assert body["files"] == [
            {
                "path": "SKILL.md",
                "size": len(raw),
                "hash": "sha256:" + hashlib.sha256(raw).hexdigest(),
            }
        ]


def test_the_skills_extension_is_advertised_on_the_era_that_defines_it(listed):
    *_, capabilities = listed("auto")

    assert "io.modelcontextprotocol/skills" in capabilities["extensions"]
