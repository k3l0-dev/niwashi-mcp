# Copyright (C) 2026 Khalid El-Ouiali — MONARK AIOPS srl
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
`_tenant_of` — the tenant a DN belongs to, for `count(group_by="tenant")`.

A tenant is not an attribute of the objects it owns, only a `tn-<name>` segment
of their DN, so this one parser decides every per-tenant number the tool gives.
The cases worth pinning are the ones where a naive `split("/")` or a substring
search credits an object to the wrong tenant.
"""

import pytest

from niwashi_mcp.main import _tenant_of


@pytest.mark.parametrize(
    ("dn", "tenant"),
    [
        ("uni/tn-OT", "OT"),
        ("uni/tn-OT/BD-servers", "OT"),
        ("uni/tn-OT/ap-prod/epg-web/rsdomAtt-[uni/phys-p]", "OT"),
        ("uni/tn-common/brc-x", "common"),
        ("uni/tn-a.b_c-d/BD-x", "a.b_c-d"),
        # An operational tenant object, which lives outside uni/.
        ("topology/pod-1/node-101/sys/acllog/tn-OT", "OT"),
    ],
)
def test_the_tenant_is_the_tn_segment(dn, tenant):
    assert _tenant_of(dn) == tenant


@pytest.mark.parametrize(
    "dn",
    [
        "",
        "uni",
        "uni/infra/attentp-x",
        "topology/pod-1/node-101/sys/fault-F1",
        # A tenant named only inside a bracketed key: the object is not in that
        # tenant's subtree, it merely refers to something that is.
        "uni/epp/fv-[uni/tn-A/ap-x/epg-y]/node-101",
        "uni/infra/attentp-x/rsdomP-[uni/tn-A]",
        # `tn-` inside a segment is not a tenant segment.
        "topology/pod-1/node-1/sys/AnyToEp-tn-A-r-r1-l-1",
        # No name after the prefix.
        "uni/tn-",
        # Unbalanced brackets must not crash or invent a tenant.
        "uni/epp/fv-[uni/tn-A",
        "uni/x]/y",
    ],
)
def test_a_dn_with_no_tenant_segment_has_none(dn):
    assert _tenant_of(dn) is None


def test_the_first_tenant_segment_wins():
    assert _tenant_of("uni/tn-A/x/tn-B") == "A"
