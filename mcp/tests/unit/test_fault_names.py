# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""`count(group_by="code")` names the fault codes the catalogue defines.

`{"F0546": 60}` leaves the reader to say what `F0546` is, and an agent that does
so from memory is how a fault table gets fabricated. The catalogue's `fault`
table maps each code to the rule name the schema gives it, and
`catalog.fault_names` reads it. Codes the table does not hold — threshold
crossing alerts, FSM faults — must come back unnamed, never guessed.

Names are checked against niwaki's own public `fault_name`, which is a separate
code path onto the same file, so a bug shared by both would have to be a bug in
two places.
"""

from __future__ import annotations

import pytest

from niwashi_mcp.registry import catalog
from tests.conftest import MINIMAL_DESCRIPTIONS, StubBackend, make_ctx

pytestmark = pytest.mark.catalog

# Codes read off a real fabric. F0546 and F0467 are in the catalogue; F110176
# is a threshold-crossing alert (rule tca-l2-ingr-bytes5min-drop-rate) and
# F608163 an FSM fault (fsm-handshake-with-apic-fsm-fail), neither of which is.
KNOWN = {"F0467": "fltFvNwIssuesConfig-failed", "F0546": "fltEthpmIfPortDownNoInfra"}
UNKNOWN = ["F110176", "F608163"]


class TestFaultNames:
    def test_a_known_code_resolves_to_its_rule_name(self):
        assert catalog.fault_names(["F0467"]) == {"F0467": "fltFvNwIssuesConfig-failed"}

    def test_every_code_agrees_with_niwakis_own_accessor(self):
        """All 1,491, against `niwaki.catalog.fault_name` rather than our SQL."""
        from niwaki import catalog as niwaki_catalog

        codes = [r[0] for r in catalog._query("SELECT code FROM fault")]
        assert len(codes) > 1_400, "the fault table shrank — re-scope this test"
        names = catalog.fault_names(codes)
        assert len(names) == len(codes)
        for code in codes:
            assert names[code] == niwaki_catalog.fault_name(code), code

    def test_a_code_the_catalogue_does_not_define_is_absent_not_guessed(self):
        assert catalog.fault_names(UNKNOWN) == {}
        assert catalog.fault_names(["F0467", *UNKNOWN]) == {"F0467": KNOWN["F0467"]}

    def test_the_result_follows_the_order_given_and_names_each_code_once(self):
        names = catalog.fault_names(["F0546", "F0467", "F0546"])
        assert list(names) == ["F0546", "F0467"]

    def test_nothing_in_nothing_out(self):
        assert catalog.fault_names([]) == {}

    def test_an_arbitrary_string_is_just_an_unknown_code(self):
        """Values are bound parameters, never SQL; and none of these is a code."""
        assert catalog.fault_names(["", "f0467", "F0467 ", "'; DROP TABLE fault;--"]) == {}


# ── through the tool ──────────────────────────────────────────────────────────


def _fault(code: str, n: int, severity: str = "minor") -> list[dict]:
    return [
        {
            "faultInst": {
                "attributes": {
                    "dn": f"topology/pod-1/node-{100 + i}/sys/fault-{code}",
                    "code": code,
                    "severity": severity,
                }
            }
        }
        for i in range(n)
    ]


def _ctx(imdata: list[dict], *, cap_at: int | None = None):
    return make_ctx(
        {
            "descriptions": dict(MINIMAL_DESCRIPTIONS),
            "backend": StubBackend(imdata, cap_at=cap_at),
        }
    )


@pytest.fixture(autouse=True)
def _default_strictness(monkeypatch):
    monkeypatch.delenv("NIWASHI_MCP_STRICT_ATTRIBUTES", raising=False)


@pytest.mark.asyncio
class TestThroughTheTool:
    async def test_grouping_by_code_names_the_codes_the_catalogue_knows(self):
        from niwashi_mcp.main import count

        ctx = _ctx(_fault("F0546", 3) + _fault("F0467", 2) + _fault("F110176", 1))
        result = await count("faultInst", ctx, group_by="code")

        assert result["groups"] == {"F0546": 3, "F0467": 2, "F110176": 1}
        # in the order of `groups`, with the alert it does not define left out
        assert list(result["group_names"].items()) == [
            ("F0546", KNOWN["F0546"]),
            ("F0467", KNOWN["F0467"]),
        ]
        assert result["complete"] is True

    async def test_the_breakdown_itself_is_what_it_was(self):
        """`group_names` is beside the existing keys, never instead of one."""
        from niwashi_mcp.main import count

        result = await count("faultInst", _ctx(_fault("F0467", 2)), group_by="code")
        assert [k for k in result if k != "group_names"] == [
            "class_name", "count", "scope_dn", "filters",
            "group_by", "groups", "ungrouped", "complete", "note",
        ]
        assert result["groups"] == {"F0467": 2}
        assert result["ungrouped"] == 0
        assert result["count"] == 2

    @pytest.mark.parametrize("group_by", ["severity", "tenant", "dn"])
    async def test_any_other_grouping_is_untouched(self, group_by):
        from niwashi_mcp.main import count

        result = await count("faultInst", _ctx(_fault("F0467", 2)), group_by=group_by)
        assert "group_names" not in result

    async def test_a_value_equal_to_a_code_under_another_attribute_is_not_named(self):
        """It is the attribute that makes a value a fault code, not the value."""
        from niwashi_mcp.main import count

        objs = [
            {"faultInst": {"attributes": {"dn": "uni/fault-a", "code": "F0467", "descr": "F0467"}}}
        ]
        result = await count("faultInst", _ctx(objs), group_by="descr")
        assert result["groups"] == {"F0467": 1}
        assert "group_names" not in result

    async def test_no_resolvable_code_means_no_key(self):
        """Not an empty dict: an agent would read that as "named, none found"."""
        from niwashi_mcp.main import count

        result = await count("faultInst", _ctx(_fault("F110176", 2)), group_by="code")
        assert result["groups"] == {"F110176": 2}
        assert "group_names" not in result

    async def test_a_plain_count_gains_nothing(self):
        from niwashi_mcp.main import count

        result = await count("faultInst", _ctx(_fault("F0467", 2)))
        assert list(result) == ["class_name", "count", "scope_dn", "filters"]

    async def test_a_withheld_breakdown_names_nothing(self):
        """No groups, so nothing to name — and no partial answer by the back door."""
        from niwashi_mcp.main import count

        result = await count(
            "faultInst", _ctx(_fault("F0467", 4), cap_at=2), group_by="code"
        )
        assert result["complete"] is False
        assert result["groups"] is None
        assert "group_names" not in result

    async def test_objects_with_no_code_stay_ungrouped_and_unnamed(self):
        from niwashi_mcp.main import count

        no_code = [{"faultInst": {"attributes": {"dn": "uni/fault-x", "severity": "minor"}}}]
        result = await count(
            "faultInst", _ctx(_fault("F0467", 1) + no_code), group_by="code"
        )
        assert result["groups"] == {"F0467": 1}
        assert result["ungrouped"] == 1
        assert result["group_names"] == {"F0467": KNOWN["F0467"]}
