# Copyright (C) 2026 Khalid El-Ouiali — MONARK AIOPS srl
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
Tests for the fabric-version check — apic/version.py, ApicClient.controller_version
and the startup wiring in main.app_lifespan.

Why this exists
---------------
The startup log used to name only the APIC release the object-model *catalogue*
was built from. It said nothing about the fabric the server was connected to, so
an operator on another release learned of the gap only when a query came back
empty — and an empty result is indistinguishable from "there are none". The
check reads the controller's running release, compares it with the catalogue's,
and warns on a difference.

What is pinned here is as much what it must NOT do as what it must:

  * never fail startup, whatever the lookup does (error, empty, malformed);
  * never claim a mismatch on a string it could not read — that would be the
    same kind of wrong answer the warning is there to prevent;
  * issue exactly one GET, with the controller filter, through the same
    retry/re-auth path as every other read.

Everything runs against FakeHTTPClient (tests/unit/test_client.py): no network.
"""

import logging
from unittest.mock import MagicMock, patch

import httpx
import pytest

import niwashi_mcp.main as main
from niwashi_mcp.apic.version import check_fabric_version, normalise_apic_version
from niwashi_mcp.exceptions import (
    ApicAuthError,
    ApicConnectionError,
    ApicRequestError,
    ApicResponseError,
)
from niwashi_mcp.registry import catalog
from tests.conftest import apic_login_response, apic_response
from tests.unit.test_client import _make_client, _MockResponse

_FILTER = 'eq(firmwareCtrlrRunning.type,"controller")'

# The phrase only the mismatch warning contains. The "could not read" and
# "unknown" warnings mention the catalogue too, so a test that means "no
# mismatch was claimed" must look for this, not for the word "catalogue".
_MISMATCH = "but the object-model catalogue describes APIC"


def _running(version: object = "apic-6.0(9c)", **extra: object) -> dict:
    """One firmwareCtrlrRunning imdata item, as an APIC serves it."""
    attributes = {"type": "controller", "version": version, **extra}
    return {"firmwareCtrlrRunning": {"attributes": attributes}}


def _version_response(*items: dict) -> _MockResponse:
    return _MockResponse(200, apic_response(list(items)))


# ── normalise_apic_version ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        "6.0(9c)",             # the catalogue's own spelling
        "apic-6.0(9c)",        # what a controller reports
        "APIC-6.0(9C)",        # case never decides a comparison
        "  apic-6.0(9c)\n",    # nor does whitespace
        "aci-apic-6.0(9c)",    # a longer product prefix
    ],
)
def test_the_spellings_of_one_release_normalise_to_the_same_string(raw):
    assert normalise_apic_version(raw) == "6.0(9c)"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("n9000-16.0(9c)", "16.0(9c)"),      # a switch's firmware string
        ("6.0(3.82a)", "6.0(3.82a)"),        # an engineering build's dotted maintenance
        ("5.2(8e)", "5.2(8e)"),
        ("4.2(7f)", "4.2(7f)"),
    ],
)
def test_other_release_strings_are_read(raw, expected):
    assert normalise_apic_version(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "unknown", "apic", "6.0", "6.0.9c", "(9c)", "6.0(9c", "apic-", "v6", None, 6.0, b"6.0(9c)"],
)
def test_a_string_that_is_not_a_release_is_none_not_a_guess(raw):
    """None means "cannot read it" and callers must treat it as unknown. Anything
    that merely resembles a release would be compared, and could be wrongly
    called a mismatch."""
    assert normalise_apic_version(raw) is None


def test_different_releases_normalise_differently():
    assert normalise_apic_version("apic-6.0(9c)") != normalise_apic_version("apic-6.0(9d)")
    assert normalise_apic_version("apic-6.0(9c)") != normalise_apic_version("apic-6.1(9c)")


# ── the catalogue still has what the lookup reads ────────────────────────────


@pytest.mark.catalog
def test_the_catalogue_has_the_class_and_attributes_the_lookup_reads():
    """The lookup filters on `type` and reads `version`. Both are attributes of
    `firmwareCtrlrRunning` in the shipped catalogue today; if a niwaki bump
    renames either, the lookup would silently return nothing, and the check
    would report "unknown" on every fabric without anyone noticing."""
    assert catalog.class_exists("firmwareCtrlrRunning")
    assert catalog.property_exists("firmwareCtrlrRunning", "type")
    assert catalog.property_exists("firmwareCtrlrRunning", "version")


@pytest.mark.catalog
def test_the_catalogue_lists_controller_as_a_firmware_type():
    """`controller` is the value that separates the APIC from switch and plugin
    firmware in this class, and it is a documented option of `type`."""
    details = catalog.load_schema(
        "firmwareCtrlrRunning", properties_filter=["type"]
    )["property_details"]["type"]
    assert "controller" in details["options"]


@pytest.mark.catalog
def test_the_catalogues_own_release_is_a_string_this_module_can_read():
    """If the catalogue's release stopped parsing, the check would degrade to
    "cannot compare" on every start — the one thing it is for."""
    assert normalise_apic_version(catalog.apic_version()) is not None


# ── ApicClient.controller_version ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_controller_version_is_one_get_with_the_controller_filter():
    client = _make_client(_version_response(_running("apic-6.0(9c)")))

    assert await client.controller_version() == "apic-6.0(9c)"

    (request,) = client._client.requests
    assert request["method"] == "GET"
    assert request["url"] == "https://10.0.0.1/api/class/firmwareCtrlrRunning.json"
    assert request["params"] == {"query-target-filter": _FILTER}


@pytest.mark.asyncio
async def test_controller_version_returns_the_string_as_written_but_stripped():
    """No interpretation here — a format this client has never seen must reach
    the caller, which decides what it means."""
    client = _make_client(_version_response(_running("  weird build 7 ")))
    assert await client.controller_version() == "weird build 7"


@pytest.mark.asyncio
async def test_controller_version_reads_the_first_object_of_a_cluster():
    """A three-node cluster answers with three objects. The first is read."""
    client = _make_client(
        _version_response(
            _running("apic-6.0(9c)", dn="topology/pod-1/node-1/sys/ctrlrfwstatuscont/ctrlrrunning"),
            _running("apic-6.0(8d)", dn="topology/pod-1/node-2/sys/ctrlrfwstatuscont/ctrlrrunning"),
        )
    )
    assert await client.controller_version() == "apic-6.0(9c)"


@pytest.mark.asyncio
async def test_controller_version_is_none_when_no_controller_object_is_returned():
    """Nothing reported is an answer, distinct from a malformed one."""
    client = _make_client(_version_response())
    assert await client.controller_version() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "item",
    [
        {"firmwareCtrlrRunning": {"attributes": {"type": "controller"}}},     # no version
        {"firmwareCtrlrRunning": {}},                                         # no attributes
        {"firmwareCtrlrRunning": {"attributes": {"version": ""}}},            # blank
        {"firmwareCtrlrRunning": {"attributes": {"version": "   "}}},         # whitespace
        {"firmwareCtrlrRunning": {"attributes": {"version": None}}},          # null
        {"firmwareCtrlrRunning": {"attributes": {"version": 6}}},             # not a string
        {"fabricNode": {"attributes": {"version": "apic-6.0(9c)"}}},          # wrong class
        "not-an-object",
    ],
    ids=["no-version", "no-attributes", "blank", "whitespace", "null", "number", "wrong-class", "string"],
)
async def test_controller_version_raises_on_a_malformed_object(item):
    """An object that arrived but cannot be read is a protocol failure, not
    "no version" — the two must not be conflated, or a broken answer looks
    exactly like an APIC that has nothing to say."""
    client = _make_client(_version_response(item))
    with pytest.raises(ApicResponseError):
        await client.controller_version()


@pytest.mark.asyncio
async def test_controller_version_reauthenticates_like_any_other_read():
    """401 on the data path → re-login → retry: proof it uses the shared
    transport and not a private one."""
    client = _make_client(
        _MockResponse(401, {}),
        _MockResponse(200, apic_login_response()),
        _version_response(_running("apic-6.0(9c)")),
    )
    assert await client.controller_version() == "apic-6.0(9c)"
    assert [r["method"] for r in client._client.requests] == ["GET", "POST", "GET"]


@pytest.mark.asyncio
async def test_controller_version_retries_a_transient_failure():
    client = _make_client(
        _MockResponse(503, {}),
        _version_response(_running("apic-6.0(9c)")),
    )
    assert await client.controller_version() == "apic-6.0(9c)"
    assert len(client._client.requests) == 2


@pytest.mark.asyncio
async def test_controller_version_issues_nothing_but_gets():
    """The server is read-only: the one non-GET it ever sends is the login."""
    client = _make_client(_version_response(_running()))
    await client.controller_version()
    assert {r["method"] for r in client._client.requests} == {"GET"}


# ── check_fabric_version — the verdict, and what it logs ─────────────────────


class _Backend:
    """A backend whose controller_version() returns or raises what it is given."""

    def __init__(self, outcome):
        self._outcome = outcome

    async def controller_version(self):
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


@pytest.fixture
def apic_log(caplog):
    caplog.set_level(logging.DEBUG, logger="niwashi-mcp.apic")
    return caplog


def _warnings(caplog) -> list[str]:
    """WARNING-and-above messages from the APIC logger — the version check's and
    the transport's — leaving the unrelated startup warnings (TLS verification)
    out of the way."""
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.name == "niwashi-mcp.apic"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("reported", ["apic-6.0(9c)", "6.0(9c)", "APIC-6.0(9C)"])
async def test_a_matching_release_logs_no_warning(apic_log, reported):
    fabric = await check_fabric_version(_Backend(reported), "6.0(9c)")

    assert fabric == "6.0(9c)"
    assert _warnings(apic_log) == []
    assert "matching" in apic_log.text


@pytest.mark.asyncio
async def test_a_different_release_warns_and_says_what_can_go_wrong(apic_log):
    fabric = await check_fabric_version(_Backend("apic-6.1(2f)"), "6.0(9c)")

    assert fabric == "6.1(2f)", "the fabric's release is returned even on a mismatch"
    (warning,) = _warnings(apic_log)
    assert "6.1(2f)" in warning and "6.0(9c)" in warning
    assert "added, removed or renamed" in warning
    assert "refused as unknown" in warning and "come back empty" in warning
    assert "SUPPORTED-APIC.md" in warning


@pytest.mark.asyncio
@pytest.mark.parametrize("reported", ["apic-6.0(9d)", "apic-5.2(9c)", "apic-6.1(9c)", "apic-6.0(10c)"])
async def test_any_difference_in_the_release_is_a_mismatch(apic_log, reported):
    """A patch letter, a maintenance number or a train differs; none of them is
    "close enough"."""
    await check_fabric_version(_Backend(reported), "6.0(9c)")
    (warning,) = _warnings(apic_log)
    assert _MISMATCH in warning


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        ApicConnectionError("10.0.0.1", "refused"),
        ApicAuthError("10.0.0.1", 403, "no read access"),
        ApicRequestError("https://10.0.0.1/api/class/firmwareCtrlrRunning.json", 500, "boom"),
        ApicResponseError("https://10.0.0.1/x", "malformed"),
        RuntimeError("anything at all"),
        ValueError("even a bug"),
    ],
    ids=lambda e: type(e).__name__,
)
async def test_a_failed_lookup_warns_returns_none_and_does_not_raise(apic_log, outcome):
    fabric = await check_fabric_version(_Backend(outcome), "6.0(9c)")

    assert fabric is None
    (warning,) = _warnings(apic_log)
    assert "Could not read the fabric's APIC version" in warning
    assert type(outcome).__name__ in warning
    assert "SUPPORTED-APIC.md" in warning
    # An unreadable fabric is not a different fabric.
    assert _MISMATCH not in warning


@pytest.mark.asyncio
async def test_cancellation_is_not_swallowed():
    """`except Exception` is the right width: a cancelled startup must stay
    cancelled, not turn into a logged warning and carry on."""
    import asyncio

    with pytest.raises(asyncio.CancelledError):
        await check_fabric_version(_Backend(asyncio.CancelledError()), "6.0(9c)")


@pytest.mark.asyncio
async def test_nothing_reported_warns_unknown_and_returns_none(apic_log):
    fabric = await check_fabric_version(_Backend(None), "6.0(9c)")

    assert fabric is None
    (warning,) = _warnings(apic_log)
    assert "no controller firmware version" in warning
    assert "unknown" in warning


@pytest.mark.asyncio
@pytest.mark.parametrize("reported", ["", "weird build 7", "6.0.9c", MagicMock()])
async def test_an_unreadable_string_is_unknown_never_a_mismatch(apic_log, reported):
    """The core safety property: failing to parse is not evidence of a
    different release, so it must not be reported as one."""
    fabric = await check_fabric_version(_Backend(reported), "6.0(9c)")

    assert fabric is None
    (warning,) = _warnings(apic_log)
    assert "not a release string this server can read" in warning
    assert "unknown" in warning
    assert _MISMATCH not in warning


@pytest.mark.asyncio
async def test_an_unreadable_catalogue_release_cannot_be_compared_and_says_so(apic_log):
    fabric = await check_fabric_version(_Backend("apic-6.0(9c)"), "unknown")

    assert fabric == "6.0(9c)", "the fabric's own release is still worth keeping"
    (warning,) = _warnings(apic_log)
    assert "cannot be compared" in warning
    assert "describes APIC" not in warning, "claimed a mismatch it could not establish"


# ── app_lifespan — wired in, and never fatal ─────────────────────────────────


async def _start(monkeypatch, client, *, catalogue_version="6.0(9c)"):
    """Run the real lifespan against `client`; return the context it yields."""
    monkeypatch.setenv("APIC_HOST", "10.0.0.1")
    monkeypatch.setenv("APIC_PASSWORD", "secret")
    with (
        patch("niwashi_mcp.main.load_dotenv"),
        patch("niwashi_mcp.main.catalog.descriptions_index", return_value={}),
        patch("niwashi_mcp.main.catalog.apic_version", return_value=catalogue_version),
        patch("niwashi_mcp.main.ApicClient", return_value=client),
    ):
        async with main.app_lifespan(MagicMock()) as ctx:
            return dict(ctx)


def _login() -> _MockResponse:
    return _MockResponse(200, apic_login_response())


@pytest.mark.asyncio
async def test_lifespan_stores_the_fabric_version_and_stays_quiet_on_a_match(
    monkeypatch, apic_log
):
    client = _make_client(_login(), _version_response(_running("apic-6.0(9c)")))

    ctx = await _start(monkeypatch, client)

    assert ctx["fabric_version"] == "6.0(9c)"
    assert ctx["backend"] is client
    assert _warnings(apic_log) == []


@pytest.mark.asyncio
async def test_lifespan_matches_against_the_real_catalogue_release(monkeypatch, apic_log):
    """The same, with nothing patched but the transport: a controller running
    the very release the shipped catalogue describes raises no warning, whatever
    release that is today."""
    real = catalog.apic_version()
    client = _make_client(_login(), _version_response(_running(f"apic-{real}")))
    monkeypatch.setenv("APIC_HOST", "10.0.0.1")
    monkeypatch.setenv("APIC_PASSWORD", "secret")
    with (
        patch("niwashi_mcp.main.load_dotenv"),
        patch("niwashi_mcp.main.catalog.descriptions_index", return_value={}),
        patch("niwashi_mcp.main.ApicClient", return_value=client),
    ):
        async with main.app_lifespan(MagicMock()) as ctx:
            assert ctx["fabric_version"] == normalise_apic_version(real)

    assert _warnings(apic_log) == []


@pytest.mark.asyncio
async def test_lifespan_asks_with_a_get_after_the_login_and_nothing_else(monkeypatch):
    client = _make_client(_login(), _version_response(_running()))

    await _start(monkeypatch, client)

    login, lookup = client._client.requests
    assert login["method"] == "POST" and login["url"].endswith("/api/aaaLogin.json")
    assert lookup["method"] == "GET"
    assert lookup["url"].endswith("/api/class/firmwareCtrlrRunning.json")
    assert lookup["params"] == {"query-target-filter": _FILTER}


@pytest.mark.asyncio
async def test_lifespan_warns_on_a_mismatch_and_still_starts(monkeypatch, apic_log):
    client = _make_client(_login(), _version_response(_running("apic-6.1(2f)")))

    ctx = await _start(monkeypatch, client)

    assert ctx["fabric_version"] == "6.1(2f)"
    (warning,) = [w for w in _warnings(apic_log) if _MISMATCH in w]
    assert "6.1(2f)" in warning and "6.0(9c)" in warning
    assert "SUPPORTED-APIC.md" in warning


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("responses", "needle"),
    [
        # The lookup itself fails — three transient answers exhaust the retries.
        ([_MockResponse(500, {})] * 3, "Could not read"),
        ([httpx.ConnectError("refused")] * 3, "Could not read"),
        # A permanent rejection, e.g. the account may not read this class.
        ([_MockResponse(400, {"imdata": []})], "Could not read"),
        # The APIC answered with nothing.
        ([_version_response()], "no controller firmware version"),
        # It answered with something unreadable.
        ([_version_response({"firmwareCtrlrRunning": {"attributes": {}}})], "Could not read"),
        ([_version_response(_running("???"))], "not a release string"),
    ],
    ids=["http-500", "connect-error", "http-400", "empty-imdata", "malformed", "unparseable"],
)
async def test_lifespan_never_fails_startup_over_the_lookup(
    monkeypatch, apic_log, responses, needle
):
    """Every way the lookup can go wrong ends the same way: a warning, a None,
    and a server that comes up."""
    client = _make_client(_login(), *responses)

    ctx = await _start(monkeypatch, client)

    assert ctx["fabric_version"] is None
    assert ctx["backend"] is client, "startup did not complete"
    assert any(needle in w for w in _warnings(apic_log)), _warnings(apic_log)
    assert not any(_MISMATCH in w for w in _warnings(apic_log)), (
        "an unreadable answer was reported as a mismatch"
    )


@pytest.mark.asyncio
async def test_lifespan_survives_a_403_on_the_lookup_after_a_good_login(monkeypatch, apic_log):
    """A read-only account that cannot see firmware classes: login works, the
    lookup is refused even after re-authentication. Not fatal."""
    client = _make_client(
        _login(),
        _MockResponse(403, {}),
        _login(),
        _MockResponse(403, {}),
    )

    ctx = await _start(monkeypatch, client)

    assert ctx["fabric_version"] is None
    assert any("ApicAuthError" in w for w in _warnings(apic_log))


@pytest.mark.asyncio
async def test_lifespan_with_an_unreadable_catalogue_release_does_not_claim_a_mismatch(
    monkeypatch, apic_log
):
    client = _make_client(_login(), _version_response(_running("apic-6.0(9c)")))

    ctx = await _start(monkeypatch, client, catalogue_version="unknown")

    assert ctx["fabric_version"] == "6.0(9c)"
    assert not any(_MISMATCH in w for w in _warnings(apic_log))


@pytest.mark.asyncio
async def test_lifespan_still_closes_the_backend_after_the_check(monkeypatch):
    client = _make_client(_login(), _version_response(_running()))
    closed = []

    async def _close():
        closed.append(True)

    client.close = _close

    await _start(monkeypatch, client)

    assert closed == [True]
