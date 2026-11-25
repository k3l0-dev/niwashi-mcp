# Copyright (C) 2026 Khalid El-Ouiali — MONARK AIOPS srl
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
The cost of the envelope guard on a 5,000-object result — the most one
`fetch_all` call ever holds.

`query` and `count` check the envelope they built against its TypedDict before
returning it (`main._guarded`). The check is only worth having if it is
invisible next to what the same call spends fetching from the APIC and what
FastMCP spends serialising the answer. Measured on this machine (Apple M3 Max,
Python 3.13) on 5,000 objects of 40 attributes each:

    guard alone                             4.6 ms   (0.9 microseconds an object)
    `query(fetch_all)`, tool function       12.0 ms  without, 16.8 ms with
    `query(fetch_all)`, in-memory client    151 ms   without, 155 ms with
    `count` guard                           0.5 microseconds

The APIC takes seconds to produce the same objects. The ceiling is a sanity bound
sized to catch an order-of-magnitude change — validation turning into a
per-object Python loop, say — not to police milliseconds, and it allows for a CI
runner 2-4x slower than that machine.
"""

import time

from niwashi_mcp import main

_N = 5_000
_CEILING_MS = 150

# Filler attributes share one string object per name, as in test_redaction_perf:
# there is no reason to build 200,000 distinct strings to make the point.
_FILLER = {f"attr{k}": f"value-{k}" for k in range(36)}


def _envelope() -> dict:
    results = [
        {**_FILLER, "_class": "fvBD", "dn": f"uni/tn-OT/BD-bd{i}", "name": f"bd{i}",
         "descr": "x" * 30}
        for i in range(_N)
    ]
    return {
        "results": results, "returned": _N, "total_available": _N, "truncated": False,
        "next_page": None, "complete": True, "note": None, "fields": None,
    }


def test_guarding_5000_objects_is_invisible_next_to_fetching_them():
    envelope = _envelope()

    t0 = time.perf_counter()
    for _ in range(5):
        main._guarded("query", main._QUERY_GUARD, envelope)
    per_call_ms = (time.perf_counter() - t0) / 5 * 1000

    assert per_call_ms < _CEILING_MS, (
        f"guarding {_N} objects took {per_call_ms:.1f} ms. Measured at ~5 ms; "
        f"this allows ~30x. Past it the check is a visible share of every "
        f"fetch_all call, which is what it must never become."
    )
