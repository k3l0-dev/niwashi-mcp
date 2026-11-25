# Copyright (C) 2026 Khalid El-Ouiali — MONARK AIOPS srl
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
`count(..., group_by=...)` — the breakdown "how many X per Y?".

A plain count is one cheap request. A breakdown fetches every matching object
and tallies it in the tool layer, so the failure that matters is a breakdown
that *looks* final and is not: the fetch stops at its safety cap, or the set
moves under it, and the groups quietly cover a fraction of the objects. An
agent reads that as a finding ("only two tenants have faults"). These tests pin
the rule the other way round: groups are returned only when they account for
every object `count` reports, and are null otherwise.

Kept apart from test_tools.py so the count tests there stay a short, flat list
of the plain tool.
"""

import json

import pytest

from niwashi_mcp.exceptions import UnknownAttributeError, UnknownClassError
from tests.conftest import MINIMAL_DESCRIPTIONS, StubBackend, make_ctx

# ── helpers ───────────────────────────────────────────────────────────────────


def _fault(dn: str, severity: str | None = "minor", **extra: str) -> dict:
    """One faultInst imdata item; `severity=None` leaves the attribute absent."""
    attrs = {"dn": dn, **extra}
    if severity is not None:
        attrs["severity"] = severity
    return {"faultInst": {"attributes": attrs}}


def _ctx(imdata: list[dict], *, cap_at: int | None = None, backend=None):
    return make_ctx(
        {
            "descriptions": dict(MINIMAL_DESCRIPTIONS),
            "backend": backend or StubBackend(imdata, cap_at=cap_at),
        }
    )


def _assert_partitions_count(result: dict) -> None:
    """The invariant the whole feature rests on."""
    assert result["complete"] is True
    assert result["groups"] is not None
    assert sum(result["groups"].values()) + result["ungrouped"] == result["count"]


@pytest.fixture(autouse=True)
def _default_strictness(monkeypatch):
    """Every test starts from the shipped default; the lenient ones opt out."""
    monkeypatch.delenv("NIWASHI_MCP_STRICT_ATTRIBUTES", raising=False)


# Fault objects spread over two tenants plus two fabric-level ones (no tenant
# segment in the DN), so one fixture exercises every branch of the tenant key.
_FAULTS = [
    _fault("uni/tn-A/fault-F1", "critical"),
    _fault("uni/tn-A/ap-x/epg-y/fault-F2", "minor"),
    _fault("uni/tn-A/BD-z/fault-F3", "minor"),
    _fault("uni/tn-B/fault-F4", "minor"),
    _fault("uni/tn-B/ap-x/fault-F5", "critical"),
    _fault("topology/pod-1/node-101/sys/fault-F6", "major"),
    _fault("topology/pod-1/node-102/sys/fault-F7", "minor"),
]


# ── group_by unset: the plain count, untouched ────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("group_by", [None, ""])
async def test_without_group_by_the_response_is_exactly_the_plain_count(
    tool_ctx, group_by
):
    from niwashi_mcp.main import count

    result = await count("fvBD", tool_ctx, group_by=group_by)

    # Same four keys, same order, nothing added: a client written against the
    # old shape must not see a new field appear.
    assert list(result) == ["class_name", "count", "scope_dn", "filters"]
    assert json.dumps(result) == (
        '{"class_name": "fvBD", "count": 3, "scope_dn": null, "filters": {}}'
    )
    # …and it stays the one cheap request: nothing was fetched to be tallied.
    assert [c["method"] for c in tool_ctx.lifespan_context["backend"].calls] == [
        "count_class"
    ]


# ── grouping by a real attribute ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_group_by_an_attribute_tallies_each_value(tool_ctx):
    from niwashi_mcp.main import count

    result = await count("fvBD", tool_ctx, group_by="arpFlood")

    # sample_imdata: servers=no, clients=yes, mgmt=no
    assert result["group_by"] == "arpFlood"
    assert result["groups"] == {"no": 2, "yes": 1}
    assert result["ungrouped"] == 0
    assert result["count"] == 3
    assert result["note"] is None
    assert "warnings" not in result
    _assert_partitions_count(result)
    # The original four fields are unchanged and still lead the dict.
    assert list(result)[:4] == ["class_name", "count", "scope_dn", "filters"]


@pytest.mark.asyncio
async def test_the_tally_is_fetched_with_the_same_class_filters_and_scope():
    from niwashi_mcp.main import count

    ctx = _ctx(_FAULTS)
    await count(
        "faultInst",
        ctx,
        filters={"severity": "minor"},
        scope_dn="uni/tn-A",
        filter_expr='ne(faultInst.code,"F0")',
        group_by="severity",
    )

    calls = {c["method"]: c for c in ctx.lifespan_context["backend"].calls}
    counted, fetched = calls["count_class"], calls["query_class"]
    for key in ("class_name", "filters", "scope_dn", "filter_expr"):
        assert fetched[key] == counted[key], key
    assert fetched["fetch_all"] is True
    # Fixed at query()'s ceiling, not the 20 a bare query() would page by.
    assert fetched["limit"] == 200


@pytest.mark.asyncio
async def test_filters_narrow_the_groups_and_the_count_together():
    from niwashi_mcp.main import count

    result = await count(
        "faultInst",
        _ctx(_FAULTS),
        filters={"severity": "minor"},
        group_by="severity",
    )

    assert result["count"] == 4
    assert result["groups"] == {"minor": 4}
    _assert_partitions_count(result)


@pytest.mark.asyncio
async def test_scope_narrows_the_groups_and_the_count_together():
    from niwashi_mcp.main import count

    result = await count(
        "faultInst", _ctx(_FAULTS), scope_dn="uni/tn-A", group_by="severity"
    )

    assert result["count"] == 3
    assert result["groups"] == {"minor": 2, "critical": 1}
    assert result["scope_dn"] == "uni/tn-A"
    _assert_partitions_count(result)


@pytest.mark.asyncio
async def test_a_breakdown_of_nothing_is_an_empty_complete_answer():
    from niwashi_mcp.main import count

    result = await count(
        "faultInst", _ctx(_FAULTS), filters={"severity": "cleared"}, group_by="code"
    )

    # Zero matches is a real answer here (the fetch succeeded), and it must not
    # be mistaken for the withheld case, whose groups are null rather than {}.
    assert result["count"] == 0
    assert result["groups"] == {}
    assert result["ungrouped"] == 0
    _assert_partitions_count(result)


# ── ordering ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_groups_are_ordered_by_descending_count_then_by_value():
    from niwashi_mcp.main import count

    # b and a tie on 2; c leads with 5; d trails with 1. Inserted in an order
    # that matches neither sort, so only the right sort produces the answer.
    severities = ["b"] * 2 + ["d"] + ["c"] * 5 + ["a"] * 2
    ctx = _ctx([_fault(f"uni/tn-A/fault-F{i}", s) for i, s in enumerate(severities)])

    result = await count("faultInst", ctx, group_by="severity")

    assert list(result["groups"].items()) == [("c", 5), ("a", 2), ("b", 2), ("d", 1)]
    # The order has to survive the trip to the client, not just the dict.
    wire = json.loads(json.dumps(result))
    assert list(wire["groups"]) == ["c", "a", "b", "d"]


# ── grouping by tenant ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_group_by_tenant_reads_the_tenant_from_the_dn():
    from niwashi_mcp.main import count

    result = await count("faultInst", _ctx(_FAULTS), group_by="tenant")

    assert result["groups"] == {"A": 3, "B": 2}
    assert result["count"] == 7
    _assert_partitions_count(result)


@pytest.mark.asyncio
async def test_objects_with_no_tenant_are_counted_as_ungrouped_not_as_a_group():
    from niwashi_mcp.main import count

    result = await count("faultInst", _ctx(_FAULTS), group_by="tenant")

    # Two fabric-level faults (topology/pod-1/node-.../sys/...) own no tenant.
    assert result["ungrouped"] == 2
    assert set(result["groups"]) == {"A", "B"}  # no None/"null"/"" entry
    assert sum(result["groups"].values()) + result["ungrouped"] == result["count"]


@pytest.mark.asyncio
async def test_group_by_tenant_ignores_a_tenant_mentioned_only_inside_brackets():
    from niwashi_mcp.main import count

    # An endpoint-policy object names tenant A's EPG in a key, but it lives
    # under uni/epp, not in A's subtree. Splitting through the brackets would
    # have credited it to A.
    ctx = _ctx(
        [
            _fault("uni/epp/fv-[uni/tn-A/ap-x/epg-y]/node-101/fault-F1"),
            _fault("uni/tn-B/ap-x/epg-y/rsdomAtt-[uni/phys-p]/fault-F2"),
        ]
    )

    result = await count("faultInst", ctx, group_by="tenant")

    assert result["groups"] == {"B": 1}
    assert result["ungrouped"] == 1


@pytest.mark.asyncio
async def test_group_by_tenant_is_not_limited_to_classes_that_own_the_dn():
    from niwashi_mcp.main import count

    # fvTenant objects sit at uni/tn-<name> itself: the segment is the tenant.
    ctx = _ctx(
        [
            {"fvTenant": {"attributes": {"dn": "uni/tn-A", "name": "A"}}},
            {"fvTenant": {"attributes": {"dn": "uni/tn-B", "name": "B"}}},
        ]
    )
    result = await count("fvTenant", ctx, group_by="tenant")
    assert result["groups"] == {"A": 1, "B": 1}


@pytest.mark.asyncio
async def test_a_real_tenant_attribute_beats_the_dn_derived_key():
    from niwashi_mcp.main import count

    # pingExecTn really has a `tenant` attribute (20 classes do). Answering
    # from the DN instead would give a different number than the one asked for.
    ctx = _ctx(
        [
            {
                "pingExecTn": {
                    "attributes": {
                        "dn": "uni/tn-FROM_DN/ping-1",
                        "tenant": "FROM_ATTRIBUTE",
                    }
                }
            }
        ]
    )

    result = await count("pingExecTn", ctx, group_by="tenant")

    assert result["groups"] == {"FROM_ATTRIBUTE": 1}


# ── missing and empty values ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_missing_value_cannot_collide_with_any_real_value():
    from niwashi_mcp.main import count

    # Every string a None could be spelled as is also a legal attribute value,
    # so the missing case must not live among the keys at all.
    ctx = _ctx(
        [
            _fault("uni/tn-A/fault-F1", "null"),
            _fault("uni/tn-A/fault-F2", "None"),
            _fault("uni/tn-A/fault-F3", ""),
            _fault("uni/tn-A/fault-F4", None),
            _fault("uni/tn-A/fault-F5", None),
        ]
    )

    result = await count("faultInst", ctx, group_by="severity")

    assert result["groups"] == {"": 1, "None": 1, "null": 1}
    assert result["ungrouped"] == 2
    _assert_partitions_count(result)


@pytest.mark.asyncio
async def test_an_empty_string_is_a_value_of_its_own():
    from niwashi_mcp.main import count

    # `descr` is "" on most objects: that is a finding ("none has a
    # description"), not an absence.
    ctx = _ctx(
        [
            {"fvAEPg": {"attributes": {"dn": "uni/tn-A/ap-p/epg-1", "descr": ""}}},
            {"fvAEPg": {"attributes": {"dn": "uni/tn-A/ap-p/epg-2", "descr": ""}}},
        ]
    )

    result = await count("fvAEPg", ctx, group_by="descr")

    assert result["groups"] == {"": 2}
    assert result["ungrouped"] == 0


# ── a breakdown that does not account for every object is withheld ───────────


@pytest.mark.asyncio
async def test_hitting_the_safety_cap_withholds_the_groups():
    from niwashi_mcp.main import count

    ctx = _ctx(_FAULTS, cap_at=3)

    result = await count("faultInst", ctx, group_by="severity")

    assert result["complete"] is False
    # Null, not {} and not the three groups that were fetched: a partial
    # breakdown is the answer-shaped wrong answer this field exists to prevent.
    assert result["groups"] is None
    assert result["ungrouped"] is None
    # `count` is still the real total (count_class), not the 3 fetched.
    assert result["count"] == 7
    assert result["group_by"] == "severity"
    note = result["note"]
    assert "3 of 7" in note
    assert "safety cap" in note
    assert "scope_dn" in note and "filters" in note  # says how to narrow it
    ctx.warning.assert_called()


@pytest.mark.asyncio
async def test_a_capped_tenant_breakdown_is_withheld_too():
    from niwashi_mcp.main import count

    result = await count("faultInst", _ctx(_FAULTS, cap_at=4), group_by="tenant")

    assert result["complete"] is False
    assert result["groups"] is None and result["ungrouped"] is None
    assert result["count"] == 7


class _DriftingBackend(StubBackend):
    """count_class and query_class disagree, as when the set moves mid-read."""

    def __init__(self, imdata: list[dict], drift: int):
        super().__init__(imdata)
        self._drift = drift

    async def count_class(self, *args, **kwargs) -> int:
        return await super().count_class(*args, **kwargs) + self._drift


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", [1, -1])
async def test_a_tally_that_disagrees_with_the_count_is_not_presented(drift):
    from niwashi_mcp.main import count

    ctx = _ctx(_FAULTS, backend=_DriftingBackend(_FAULTS, drift))

    result = await count("faultInst", ctx, group_by="severity")

    # Seven objects were fetched; the APIC said 6 (or 8). Either the set moved
    # or a page went missing — the groups cannot be trusted to add up.
    assert result["complete"] is False
    assert result["groups"] is None and result["ungrouped"] is None
    assert result["count"] == 7 + drift
    assert "7 were fetched" in result["note"]
    assert f"counted {7 + drift}" in result["note"]
    ctx.warning.assert_called()


# ── unknown attribute ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_unknown_group_by_attribute_is_refused_before_any_request():
    from niwashi_mcp.main import count

    ctx = _ctx(_FAULTS)

    with pytest.raises(UnknownAttributeError) as exc_info:
        await count("faultInst", ctx, group_by="serverity")

    err = exc_info.value
    assert err.class_name == "faultInst" and err.attribute == "serverity"
    assert "severity" in err.suggestions
    assert err.catalogue_version  # the release it was checked against
    # Nothing reached the backend: not the count, not the fetch.
    assert ctx.lifespan_context["backend"].calls == []
    ctx.warning.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["true", "1", "yes", "on", "", "anything"])
async def test_anything_but_an_explicit_off_keeps_the_refusal(monkeypatch, value):
    from niwashi_mcp.main import count

    monkeypatch.setenv("NIWASHI_MCP_STRICT_ATTRIBUTES", value)

    with pytest.raises(UnknownAttributeError):
        await count("faultInst", _ctx(_FAULTS), group_by="serverity")


@pytest.mark.asyncio
async def test_the_tenant_key_with_the_wrong_case_suggests_the_real_one():
    from niwashi_mcp.main import count

    with pytest.raises(UnknownAttributeError) as exc_info:
        await count("faultInst", _ctx(_FAULTS), group_by="Tenant")

    assert exc_info.value.suggestions[0] == "tenant"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["false", "0", "no", "off", "FALSE", " Off "])
async def test_when_not_strict_an_unknown_attribute_proceeds_with_a_warning(
    monkeypatch, value
):
    from niwashi_mcp.main import count

    monkeypatch.setenv("NIWASHI_MCP_STRICT_ATTRIBUTES", value)
    ctx = _ctx(_FAULTS)

    result = await count("faultInst", ctx, group_by="serverity")

    # The fabric may be on a release the catalogue does not describe, so the
    # request runs. Nothing carries the attribute here, so everything is
    # ungrouped — and the caller is told why, in the result and in the log.
    assert result["complete"] is True
    assert result["groups"] == {}
    assert result["ungrouped"] == result["count"] == 7
    assert len(result["warnings"]) == 1
    assert "serverity" in result["warnings"][0]
    assert "severity" in result["warnings"][0]  # the suggestion travels along
    ctx.warning.assert_called_once()


@pytest.mark.asyncio
async def test_when_not_strict_an_attribute_the_fabric_does_have_still_groups(
    monkeypatch,
):
    from niwashi_mcp.main import count

    monkeypatch.setenv("NIWASHI_MCP_STRICT_ATTRIBUTES", "false")
    ctx = _ctx(
        [
            _fault("uni/tn-A/fault-F1", "minor", futureAttr="x"),
            _fault("uni/tn-A/fault-F2", "minor", futureAttr="x"),
            _fault("uni/tn-A/fault-F3", "minor", futureAttr="y"),
        ]
    )

    result = await count("faultInst", ctx, group_by="futureAttr")

    assert result["groups"] == {"x": 2, "y": 1}
    assert len(result["warnings"]) == 1
    _assert_partitions_count(result)


@pytest.mark.asyncio
async def test_a_valid_group_by_adds_no_warnings_key():
    from niwashi_mcp.main import count

    result = await count("faultInst", _ctx(_FAULTS), group_by="severity")

    assert "warnings" not in result


@pytest.mark.asyncio
async def test_an_unknown_class_is_reported_before_the_group_by_is_looked_at():
    from niwashi_mcp.main import count

    with pytest.raises(UnknownClassError):
        await count("xyzFakeClass", _ctx([]), group_by="alsoNotReal")


# ── an object can lack the attribute altogether ──────────────────────────────


@pytest.mark.asyncio
async def test_objects_lacking_the_attribute_are_ungrouped_not_dropped():
    from niwashi_mcp.main import count

    ctx = _ctx(
        [
            _fault("uni/tn-A/fault-F1", "minor"),
            _fault("uni/tn-A/fault-F2", None),
        ]
    )

    result = await count("faultInst", ctx, group_by="severity")

    assert result["groups"] == {"minor": 1}
    assert result["ungrouped"] == 1
    _assert_partitions_count(result)
