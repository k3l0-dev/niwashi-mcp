# Tool: count

Count objects of an ACI class **without transferring them**. Answers "how many BDs / EPGs / subnets?" in a single cheap request instead of fetching everything just to measure the set.

**Verify the class name with `search_classes` first** — `count` raises the same `UnknownClassError` (with suggestions) as `query`, and checks attribute names against the catalogue the same way (see [Attribute validation](#attribute-validation)).

---

## Signature

```python
count(
    class_name: str,
    filters: dict[str, str] | None = None,
    scope_dn: str | None = None,
    filter_expr: str | None = None,
    group_by: str | None = None,
) -> CountEnvelope
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `class_name` | `str` | — | Exact ACI class name — **must** be verified with `search_classes()` first |
| `filters` | `dict[str, str]` | `{}` | Attribute equality filters `{attr: value}`, same semantics as `query()`. An unknown key raises `UnknownAttributeError` |
| `scope_dn` | `str` | — | DN of a parent object to scope the count to a subtree |
| `filter_expr` | `str` | — | Raw APIC filter for predicates beyond equality; combined with `filters` via `and()`. Each `Class.attr` operand is checked, as in `query()` |
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
| `warnings` | `list[str]` | **Present only when [attribute checking is relaxed](#relaxing-the-check) and a name was sent unchecked** — one string per problem. Absent otherwise, so a default response is unchanged. |
| `redacted` | `int` | **Present only with `group_by`, and only when a secret was cut out of the group value of some objects** (a DN or text that carried one): how many objects. Absent otherwise. See [Secrets](#secrets). |

---

## Attribute validation

Exactly the checks `query` applies to `filters` and `filter_expr`, from the same code: the APIC answers a filter on an unknown attribute with a successful `0`, and a count of zero reads as an answer. So an attribute that the catalogue does not know for its class is refused **before the request is sent**, with the closest real names attached. A `Class.attr` operand in `filter_expr` whose `Class` is unknown raises `UnknownClassError`. Quoted values are never inspected; anything the extractor does not recognise passes through untouched. The rules and their edge cases — abstract classes, system attributes, comma-joined items — are documented once, in [`query`](query.md#attribute-validation).

### Relaxing the check

With `NIWASHI_MCP_STRICT_ATTRIBUTES=false` nothing is raised: the request is sent and each problem is reported in a `warnings` list on the result (and as a `ctx.warning`).

```json
{
  "class_name": "fvBD",
  "count": 0,
  "scope_dn": null,
  "filters": {"newAttr": "x"},
  "warnings": ["filters: Unknown attribute 'newAttr' on ACI class 'fvBD'. ... Sent anyway because strict attribute checking is off — an empty result may mean this name is wrong, not that nothing matches."]
}
```

**A `count` of zero beside a warning is not an answer of zero.** Re-check the name with `get_schema()` before reporting it.

The return is a typed dict (`CountEnvelope`), so the `outputSchema` the server advertises names these keys: the four above are required, and `scope_dn` admits `null`. Any key a later release adds is declared optional in that schema, so a client validating against it keeps working.

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
| `group_names` | `dict[str, str]` | Present only for `group_by="code"`, and only when the catalogue defines the rule behind at least one code: `{code: rule name}`, in the order of `groups`. See [Naming fault codes](#naming-fault-codes) |
| `warnings` | `list[str]` | Present only when non-empty: `group_by` is not in the catalogue and attribute checking is off (below) |

**The invariant.** When `complete` is `true`, `sum(groups.values()) + ungrouped == count`. The tests assert it.

**Valid values.** Any attribute of the class in the catalogue (`get_schema` lists them), or `"tenant"`. `"tenant"` is read from the object's DN — the `tn-<name>` path segment — because a tenant is not an attribute of the objects it owns. If the class has a real attribute called `tenant` (20 classes do, e.g. `pingExecTn`), the attribute wins. An unknown name raises `UnknownAttributeError` with the closest matches, before any request is sent.

**Missing values.** An object that has no value for the attribute is counted in `ungrouped`, never as a group. For `"tenant"` that is every object whose DN has no tenant segment — fabric-level objects such as `topology/pod-1/node-101/...`, or an object under `uni/epp/...` that merely *names* a tenant's EPG inside a bracketed key. It is a separate number, not a `null` key, because a JSON object key is a string and every string (`"null"`, `"None"`, `""`) is a value some attribute can really hold. An **empty string is a value**: `descr` is `""` on most objects, and that is a `""` group, not an absence.

**What it costs.** A plain count is one request that transfers one object. A grouped count fetches **every** matching object (same class, filters and scope, 200 per page) and tallies them in the server, because the APIC cannot group. On a big class, scope or filter first. The plain count it starts from decides two cases without fetching anything: a set of zero is answered at once (`groups` `{}`, `ungrouped` `0`, `complete` `true`), and a set larger than one `fetch_all` call can hold (5,000 objects) is withheld at once, as below — a capped fetch can only end that way, so walking 25 pages to reach it would only spend the APIC's time.

**It never returns a partial breakdown.** If the fetch stops short — at its 5,000-object safety cap, or because the APIC ended its pages early — or fetches a different number of objects than `count` — the set changed while it was being read — the groups would not cover the whole set, and a partial breakdown reads as a finding ("only two tenants have faults") when it is only where the fetch stopped. So `complete` is `false`, `groups` and `ungrouped` are `null`, `note` says why, and `count` stays the true APIC total. Narrow the query (one tenant at a time with `scope_dn="uni/tn-<name>"`, or one value at a time with `filters`) and group each narrower set; for a changed set, retry.

### Naming fault codes

`await count("faultInst", group_by="code")` answers "which faults, how many of each" — and a bare `F0546: 60` leaves the reader to say what `F0546` is. So the response also carries the rule name the schema defines for each code it knows:

```json
{
  "group_by": "code",
  "groups": {"F1690": 188, "F0546": 60, "F0467": 11, "F110176": 8},
  "group_names": {
    "F1690": "fltVnsConfIssueConfIssue",
    "F0546": "fltEthpmIfPortDownNoInfra",
    "F0467": "fltFvNwIssuesConfig-failed"
  }
}
```

`F110176` has no entry, and that is information: it is a threshold-crossing alert (a `tca-*` rule), whose code is minted at runtime from the operator's threshold policy and is not defined in the class schemas the catalogue is built from. FSM faults (`fsm-*` rules, such as `F608163`) are not in the table either; none of its codes has more than four digits. A code that is not in `group_names` is left unnamed rather than guessed; to say what it is, `query` one object of that code and read its own `rule` and `descr`. The names come from the catalogue's `fault` table, 1,491 codes, and do not need the APIC.

It is added here and nowhere else. Fault objects returned by `query`, `get_by_dn` and `rsp_subtree_include="faults"` already carry their own `rule` (`fv-nw-issues-config-failed` for `F0467`), so a second name on each would be redundant, and a key the APIC does not send would have no coherent answer under `fields`, which keeps only the attributes asked for. The key is absent for every other `group_by`, so those responses are byte-identical to what they were.

**Non-strict attribute checking.** With `NIWASHI_MCP_STRICT_ATTRIBUTES=false` an unknown `group_by` is not refused: the request runs, a warning is logged and added to a `warnings` list in the result, and objects that lack the attribute are reported in `ungrouped`. It exists for a fabric on a release newer than the catalogue.

---

## Secrets

A count of one for `filters={"value": "guess"}` confirms the guess, and `group_by` prints values as keys. So a secret cannot be counted *by value*: `filters`, `filter_expr` and `group_by` on a secret attribute raise `SecretAttributeError`, and a `scope_dn` or filter value that spells a secret DN raises `SecretDnError`. Counting the class itself is fine — `count("snmpTrapDest")` — and so is grouping it by any other attribute.

With `group_by`, objects are redacted **before** they are tallied, so a group key can never be a secret: `group_by="affected"` on audit records returns `.../community-<redacted>` as a key, and keys that become equal are counted together. `redacted` then says how many objects had a group value cut. It is not "every secret the fetch happened to hold": withholding a credential's password while grouping credentials by name changes nothing the caller reads, so it is not reported.

---

## Raises

| Exception | Condition |
|---|---|
| `UnknownClassError` | `class_name` is not in the catalogue (15,452 classes). Includes `.suggestions` (list, drawn from the search index) and `.registry_size` (int — the 15,239-entry search index). The guard is identical to `query`'s, so the two tools can never disagree about whether a class is known. Also raised when a `filter_expr` operand names an unknown class. |
| `UnknownAttributeError` | An attribute in `filters` or `filter_expr` is not a property of its class in the catalogue, or `group_by` is neither `"tenant"` nor an attribute of the class. Carries `.class_name`, `.attribute`, `.suggestions` and `.catalogue_version`. Nothing has been sent to the APIC when it is raised. With `NIWASHI_MCP_STRICT_ATTRIBUTES=false` it is not raised; the problem becomes a `warnings` entry instead. |
| `SecretAttributeError` | `filters`, `filter_expr` or `group_by` uses an attribute that holds a secret (or `dn`/`rn` of a class named by one, or an audit record's `changeSet`). Raised whatever `NIWASHI_MCP_STRICT_ATTRIBUTES` is set to. |
| `SecretDnError` | `scope_dn`, a filter value or `filter_expr` spells a DN that names an object by a secret. |
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
