# Copyright (C) 2026 Khalid El-Ouiali — MONARK AIOPS srl
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
Performance tests for registry.redaction on a 5,000-object result — the most one
`fetch_all` call ever holds.

Every object a tool returns goes through `redact_object`, so the question is not
whether it is fast but whether it is invisible next to what the same call spends
waiting on the APIC. Measured on this machine (Apple M3 Max, Python 3.13) against
the lab APIC, the same objects cost:

    class      objects   APIC fetch_all   redaction      share
    fvBD           405        9,824 ms       1.0 ms      0.01 %
    fvRsCtx        405        4,072 ms       1.0 ms      0.02 %
    faultInst      611        1,051 ms       0.9 ms      0.09 %
    aaaModLR     4,355        3,897 ms       7.8 ms      0.20 %
    fvAEPg         148           35 ms       0.3 ms      0.88 %

— about 2 microseconds an object (46 attributes, on the lab's real objects)
against 0.2 to 24 milliseconds an object to fetch them. On the synthetic objects
below, 5,000 ordinary 23-attribute ones take about 7 ms.

The ceilings are sanity bounds sized to catch an order-of-magnitude change, not to
police milliseconds, and they allow for a CI runner 2-4x slower than that machine:

  5,000 ordinary objects                         < 150 ms   (measured ~7 ms)
  5,000 credentials, a secret each               < 150 ms   (measured ~6 ms)
  5,000 audit records through a secret DN, each
    with a *distinct* secret (the worst case,
    which also compiles a pattern per object)    < 1,500 ms (measured ~180 ms)
"""

import time

from niwashi_mcp.registry import redaction

_N = 5_000


def _ordinary(i: int) -> dict:
    attributes = {f"attr{k}": f"value-{i}-{k}" for k in range(16)}
    attributes.update(
        _class="fvBD", dn=f"uni/tn-OT/BD-bd{i}", name=f"bd{i}", descr="x" * 30,
        modTs="2026-06-01T00:00:00.000+00:00", childAction="", lcOwn="local",
    )
    return attributes


def _credential(i: int) -> dict:
    attributes = {f"attr{k}": f"value-{i}-{k}" for k in range(12)}
    attributes.update(
        _class="vnsCCred", dn=f"uni/tn-OT/lDevVip-D{i}/cCred-admin", name="admin",
        value=f"secret{i}",
    )
    return attributes


def _audit(i: int) -> dict:
    community = f"c{i}secret"
    attributes = {f"attr{k}": f"value-{i}-{k}" for k in range(16)}
    attributes.update(
        _class="aaaModLR",
        dn=f"subj-[uni/fabric/snmpp-default/community-{community}]/mod-{i}",
        affected=f"uni/fabric/snmpp-default/community-{community}",
        descr=f"Community {community} created",
        changeSet=f"name (Old: N/A, New: {community}), descr (Old: , New: )",
    )
    return attributes


def _time(objects: list[dict]) -> tuple[float, int]:
    """Best of three, in milliseconds, and the count withheld."""
    best = float("inf")
    withheld = 0
    for _ in range(3):
        t0 = time.perf_counter()
        _, withheld = redaction.redact_objects(objects)
        best = min(best, (time.perf_counter() - t0) * 1000)
    return best, withheld


def test_5000_ordinary_objects_cost_nothing_worth_measuring():
    objects = [_ordinary(i) for i in range(_N)]

    elapsed, withheld = _time(objects)

    assert withheld == 0
    assert elapsed < 150, (
        f"redacting {_N} ordinary objects took {elapsed:.0f} ms — measured at ~7 ms; "
        f"the common case must stay a dictionary lookup and a substring test"
    )


def test_5000_ordinary_objects_come_back_as_the_same_objects():
    """Nothing is copied when there is nothing to withhold — the cost above is the whole cost."""
    objects = [_ordinary(i) for i in range(_N)]

    out, withheld = redaction.redact_objects(objects)

    assert withheld == 0
    assert all(a is b for a, b in zip(out, objects, strict=True))


def test_5000_credentials_each_with_a_secret():
    objects = [_credential(i) for i in range(_N)]

    elapsed, withheld = _time(objects)

    assert withheld == _N
    assert elapsed < 150, f"redacting {_N} credentials took {elapsed:.0f} ms — measured at ~6 ms"


def test_5000_audit_records_each_through_a_distinct_secret_dn():
    objects = [_audit(i) for i in range(_N)]

    elapsed, withheld = _time(objects)

    assert withheld == 4 * _N, "dn, affected, descr and changeSet, per record"
    assert elapsed < 1_500, (
        f"redacting {_N} audit records took {elapsed:.0f} ms — measured at ~180 ms, and "
        f"this is the worst case: every object carries a different secret"
    )
