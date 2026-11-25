# Copyright (C) 2026 Khalid El-Ouiali — MONARK AIOPS srl
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
Performance tests for niwashi_mcp.redact on a 5,000-object result — the most one
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
  5,000 audit records, changeSet parsed, no
    secret in them (what an audit query is)      < 300 ms   (measured ~33 ms)
  5,000 audit records of a credential, a distinct
    secret each, in `changeSet`                  < 1,500 ms (measured ~190 ms)

The audit cases are the dearer because a changeSet is parsed and the class it is
about is read from the `affected` DN: about 7 microseconds a record against the
lab's 0.9 ms a record to fetch them.
"""

import time

from niwashi_mcp import redact

_N = 5_000

# Filler attributes share one string object per name: the footprint test in
# test_startup_perf reads the whole process's high-water mark, and 5,000 objects
# of distinct filler strings would be 20 MB of it for no extra realism.
_FILLER = {f"attr{k}": f"value-{k}" for k in range(16)}


def _ordinary(i: int) -> dict:
    attributes = dict(_FILLER)
    attributes.update(
        _class="fvBD", dn=f"uni/tn-OT/BD-bd{i}", name=f"bd{i}", descr="x" * 30,
        modTs="2026-06-01T00:00:00.000+00:00", childAction="", lcOwn="local",
    )
    return attributes


def _credential(i: int) -> dict:
    attributes = dict(list(_FILLER.items())[:12])
    attributes.update(
        _class="vnsCCred", dn=f"uni/tn-OT/lDevVip-D{i}/cCred-admin", name="admin",
        value=f"secret{i}",
    )
    return attributes


def _audit(i: int) -> dict:
    community = f"c{i}secret"
    attributes = dict(_FILLER)
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
        withheld = redact.redact_results(objects).withheld
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

    done = redact.redact_results(objects)
    out, withheld = done.value, done.withheld

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


def _plain_audit(i: int) -> dict:
    attributes = dict(list(_FILLER.items())[:14])
    attributes.update(
        _class="aaaModLR", dn=f"subj-[uni/tn-OT/BD-bd{i}]/mod-{i}", affected=f"uni/tn-OT/BD-bd{i}",
        descr=f"Bridge Domain bd{i} created From IP:10.0.0.1",
        changeSet=f"name:bd{i}, arpFlood:no, unicastRoute:yes, mac:00:22:BD:F8:19:FF, userdom::all:",
    )
    return attributes


def _credential_audit(i: int) -> dict:
    attributes = dict(list(_FILLER.items())[:14])
    attributes.update(
        _class="aaaModLR", dn=f"subj-[uni/tn-a/lDevVip-D/cDev-C{i}/cCred]/mod-{i}",
        affected=f"uni/tn-a/lDevVip-D/cDev-C{i}/cCred", descr="Credential created From IP:10.0.0.1",
        changeSet=f"name:admin, value:pass{i}word, userdom::all:",
    )
    return attributes


def test_5000_ordinary_audit_records_are_parsed_and_left_alone():
    objects = [_plain_audit(i) for i in range(_N)]

    elapsed, withheld = _time(objects)

    assert withheld == 0
    assert elapsed < 300, f"scrubbing {_N} ordinary audit records took {elapsed:.0f} ms — measured at ~33 ms"


def test_5000_audit_records_of_a_credential_each_with_a_distinct_secret_in_changeset():
    objects = [_credential_audit(i) for i in range(_N)]

    elapsed, withheld = _time(objects)

    assert withheld == _N, "the changeSet of each"
    assert elapsed < 1_500, f"scrubbing {_N} credential audit records took {elapsed:.0f} ms — measured at ~190 ms"
