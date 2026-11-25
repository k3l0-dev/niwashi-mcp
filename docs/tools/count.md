# Tool: count

Count objects of an ACI class **without transferring them**. Answers "how many BDs / EPGs / subnets?" in a single cheap request instead of fetching everything just to measure the set.

**Verify the class name with `search_classes` first** — `count` raises the same `UnknownClassError` (with suggestions) as `query`.

---

## Signature

```python
count(
    class_name: str,
    filters: dict[str, str] | None = None,
    scope_dn: str | None = None,
    filter_expr: str | None = None,
    group_by: str | None = None,
) -> dict[str, Any]
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `class_name` | `str` | — | Exact ACI class name — **must** be verified with `search_classes()` first |
| `filters` | `dict[str, str]` | `{}` | Attribute equality filters `{attr: value}`, same semantics as `query()` |
| `scope_dn` | `str` | — | DN of a parent object to scope the count to a subtree |
| `filter_expr` | `str` | — | Raw APIC filter for predicates beyond equality; combined with `filters` via `and()` |
| `group_by` | `str` | — | Break the count down per value of this attribute, or per tenant with the special value `"tenant"`. See [Grouping](#grouping-group_by). Unset (or empty) gives the plain count |

---

## How it works

Issues the same class or subtree request as `query`, with a page size of 1, and reads the APIC-reported `totalCount` — the true size of the matching set, independent of how many objects were fetched. Exactly one object comes back instead of the whole set, so a count stays far cheaper than a full `query` on a large result set. Filtering and scoping behave exactly as in `query`.

Because the tally is the very same `total_available` that `query` reports, the two tools can never disagree about the size of the same result set.

> **Why not `rsp-subtree-include=count`?** That is the obvious idiom for a count, and it is what this tool used until v1.2.1. Its `moCount` tally was measured against reality on APIC 6.0(9c) and found to disagree — silently, and depending on the data: fabric-wide it reported 203 `fvBD` against a real 403, and for 5 of the 28 tenants holding bridge domains it reported a scoped count of **0** against subtrees really holding 1 to 192. The behaviour is deterministic rather than flaky, so a retry does not help. See [`CHANGELOG.md`](../../CHANGELOG.md) and the `count_class()` docstring for the full measurements.

---

## Return value

```json
{
  "class_name": "fvSubnet",
  "count": 12,
  "scope_dn": null,
  "filters": {"scope": "public"}
}
```

| Field | Type | Description |
|---|---|---|
| `class_name` | `str` | The class that was counted |
| `count` | `int` | Number of matching objects |
| `scope_dn` | `str \| null` | The scope DN, if one was supplied |
| `filters` | `dict` | The filters that were applied |

With `group_by` set the response gains the fields below; without it the response is exactly the four fields above, and no extra request is made.

---

## Grouping (`group_by`)

"How many X **per** Y?" — faults per severity, BDs per tenant, nodes per role — in one call:

```python
await count("faultInst", group_by="severity")
await count("fvBD", group_by="tenant")
await count("fvAEPg", scope_dn="uni/tn-OT", group_by="descr")
```

```json
{
  "class_name": "faultInst",
  "count": 7,
  "scope_dn": null,
  "filters": {},
  "group_by": "tenant",
  "groups": {"A": 3, "B": 2},
  "ungrouped": 2,
  "complete": true,
  "note": null
}
```

| Field | Type | Description |
|---|---|---|
| `group_by` | `str` | The attribute asked for |
| `groups` | `dict[str, int] \| null` | Objects per value, ordered by descending count and, within a tie, by value. `null` when `complete` is `false` |
| `ungrouped` | `int \| null` | Objects with no value to group on (see below). `null` when `complete` is `false` |
| `complete` | `bool` | `true` when the breakdown accounts for every object `count` reports |
| `note` | `str \| null` | Why there is no breakdown, and how to narrow the query. `null` when `complete` is `true` |
| `warnings` | `list[str]` | Present only when non-empty: `group_by` is not in the catalogue and attribute checking is off (below) |

**The invariant.** When `complete` is `true`, `sum(groups.values()) + ungrouped == count`. The tests assert it.

**Valid values.** Any attribute of the class in the catalogue (`get_schema` lists them), or `"tenant"`. `"tenant"` is read from the object's DN — the `tn-<name>` path segment — because a tenant is not an attribute of the objects it owns. If the class has a real attribute called `tenant` (20 classes do, e.g. `pingExecTn`), the attribute wins. An unknown name raises `UnknownAttributeError` with the closest matches, before any request is sent.

**Missing values.** An object that has no value for the attribute is counted in `ungrouped`, never as a group. For `"tenant"` that is every object whose DN has no tenant segment — fabric-level objects such as `topology/pod-1/node-101/...`, or an object under `uni/epp/...` that merely *names* a tenant's EPG inside a bracketed key. It is a separate number, not a `null` key, because a JSON object key is a string and every string (`"null"`, `"None"`, `""`) is a value some attribute can really hold. An **empty string is a value**: `descr` is `""` on most objects, and that is a `""` group, not an absence.

**What it costs.** A plain count is one request that transfers one object. A grouped count fetches **every** matching object (same class, filters and scope, 200 per page) and tallies them in the server, because the APIC cannot group. On a big class, scope or filter first.

**It never returns a partial breakdown.** If the fetch stops at its safety cap, or fetches a different number of objects than `count` — the set changed while it was being read — the groups would not cover the whole set, and a partial breakdown reads as a finding ("only two tenants have faults") when it is only where the fetch stopped. So `complete` is `false`, `groups` and `ungrouped` are `null`, `note` says why, and `count` stays the true APIC total. Narrow the query (one tenant at a time with `scope_dn="uni/tn-<name>"`, or one value at a time with `filters`) and group each narrower set; for a changed set, retry.

**Non-strict attribute checking.** With `NIWASHI_MCP_STRICT_ATTRIBUTES=false` an unknown `group_by` is not refused: the request runs, a warning is logged and added to a `warnings` list in the result, and objects that lack the attribute are reported in `ungrouped`. It exists for a fabric on a release newer than the catalogue.

---

## Raises

| Exception | Condition |
|---|---|
| `UnknownClassError` | `class_name` is not in the catalogue (15,452 classes). Includes `.suggestions` (list, drawn from the search index) and `.registry_size` (int — the 15,239-entry search index). The guard is identical to `query`'s, so the two tools can never disagree about whether a class is known. |
| `UnknownAttributeError` | `group_by` is neither `"tenant"` nor an attribute of the class in the catalogue. Includes `.suggestions` (closest names) and `.catalogue_version`. Raised before any request is sent; suppressed by `NIWASHI_MCP_STRICT_ATTRIBUTES=false`, which turns it into a `warnings` entry. |
| `FilterError` | `class_name` or a `filters` key contains characters outside the expected ACI identifier format. Filter values are always escaped, never rejected. |
| `ApicRequestError` | APIC returned a non-2xx, non-auth response — e.g. 400 for a malformed `filter_expr`, or a transient 5xx that never recovered. Carries the HTTP status and, when present, the APIC error text. |

---

## Examples

```python
# All BDs in the fabric
await count("fvBD")

# EPGs in a specific tenant
await count("fvAEPg", scope_dn="uni/tn-OT")

# Public subnets only
await count("fvSubnet", filters={"scope": "public"})

# Non-controller fabric nodes
await count("fabricNode", filter_expr='ne(fabricNode.role,"controller")')

# Faults per severity, then per tenant
await count("faultInst", group_by="severity")
await count("faultInst", group_by="tenant")

# Fabric nodes per role
await count("fabricNode", group_by="role")
```

---

## Eventual consistency

A count taken right after a large config push reflects the fabric state **at that instant**. Counts can keep moving for a few seconds while the fabric materialises the change (BDs, EPGs, and subnets appear incrementally). If a count right after a write does not match what you expect, wait for stabilisation and re-read before drawing a conclusion — do not treat the first post-write read as final.

---

## Related

- [`query`](query.md) — fetch the objects themselves once you know how many there are, or rank them (`fetch_all=True` plus local aggregation) — `group_by` tallies, it does not answer "which X has the most Y"
- [`get_by_dn`](get_by_dn.md) — fetch a single object directly by DN
