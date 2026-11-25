# Tool: get_by_dn

Fetch a single ACI object **directly by its Distinguished Name**. This is the shortcut path — it skips the `search_classes → get_schema → query` discovery sequence when you already hold an exact DN.

---

## Signature

```python
get_by_dn(
    dn: str,
    config_only: bool = False,
    include_children: list[str] | None = None,
    fields: list[str] | None = None,
) -> dict[str, Any]
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `dn` | `str` | — | Full Distinguished Name, e.g. `"uni/tn-OT/BD-servers"` |
| `config_only` | `bool` | `False` | Return only user-configurable attributes (`rsp-prop-include=config-only`) — ideal for backup and comparison |
| `include_children` | `list[str]` | — | Child class names to embed. The returned object gains a `_children` list. Each class is checked against the catalogue **before** the request is sent — see [Child classes are checked](#child-classes-are-checked) |
| `fields` | `list[str]` | — | Attribute names to keep. The object and each `_children` entry of an `include_children` class are reduced to these plus `dn`, `_class` and `_children`; children of any other class are returned whole. See [Field projection](#field-projection) |

---

## When to use it

Use `get_by_dn` whenever you **already have an exact DN** — from a previous tool result, or from a design you are verifying. The DN encodes the class, so you do not need to know the class name up front, and no `search_classes` / `get_schema` round-trip is required.

For an **unknown** object, use the discovery workflow instead: `search_classes → get_schema → query`.

Issues `GET /api/mo/{dn}.json` against the APIC.

---

## Return value

### Found

The object's attribute dict — the **same element shape as a `query()` result** (a single object, not a list):

```json
{
  "_class": "fvBD",
  "dn": "uni/tn-OT/BD-servers",
  "name": "servers",
  "arpFlood": "no",
  "unicastRoute": "yes"
}
```

With `include_children` set, the object also carries a `_children` list:

```json
{
  "_class": "fvBD",
  "dn": "uni/tn-OT/BD-servers",
  "name": "servers",
  "_children": [
    {"_class": "fvSubnet", "dn": "uni/tn-OT/BD-servers/subnet-[10.0.1.1/24]", "ip": "10.0.1.1/24"},
    {"_class": "fvRsCtx",  "dn": "uni/tn-OT/BD-servers/rsctx", "tnFvCtxName": "ot.main.vrf"}
  ]
}
```

### Child classes are checked

The APIC embeds nothing for a class it does not know, and answers with the object and no `_children` — which reads as "this object has no children". So each class in `include_children` is checked against the catalogue before the request is built, exactly as [`query`](query.md#attribute-validation) checks it (an item may be comma-joined, `["fvSubnet,fvRsCtx"]`, and counts as the classes it names):

- **Strict (the default):** an unknown class raises `UnknownClassError`, with the closest matches, and nothing is sent to the APIC. Before this check existed `get_by_dn("uni/tn-OT/BD-servers", include_children=["fvSubnett"])` returned the bridge domain with no `_children` and no warning.
- **Relaxed** (`NIWASHI_MCP_STRICT_ATTRIBUTES=false`): the request is sent, a warning is logged, and the object carries a `_note` naming the class. A missing `_children` then means the name may be wrong, not that there are none.

### Field projection

With `fields` set, the object and each `_children` entry of an `include_children` class keep only the listed attributes plus `dn`, `_class` and `_children` (always kept). A child of any other class is returned whole — `fields` was never checked against it:

```python
bd = await get_by_dn("uni/tn-OT/BD-servers", fields=["name", "arpFlood"])
# {"name": "servers", "arpFlood": "no", "dn": "uni/tn-OT/BD-servers", "_class": "fvBD"}
```

- **Validated after the fetch.** The DN encodes the class, so nothing names it up front; each name is checked against the `_class` of the object the APIC returned, plus any `include_children` classes (so a field valid only on an embedded child is accepted). That is why, unlike [`query`](query.md), a bad name is refused only after the request has been made.
- **Unknown name:** `UnknownAttributeError`, with the closest matches for the returned class.
- **Empty list:** `fields=[]` keeps only `dn`, `_class` and `_children`; omit `fields` to receive every attribute.
- **Relaxed checking** (`NIWASHI_MCP_STRICT_ATTRIBUTES=false`): an unknown name is not refused. A warning is logged and, because this tool returns the bare object rather than an envelope, the object carries a `_note` key naming the unverified fields or classes. `_note` appears only in that case (and with `fields=[]`); when several apply they are joined into the one string, none replacing another.
- **Not found** is unaffected: a missing DN returns the `found: false` dict below, whatever `fields` says — there is no class to validate against.

### Not found

The APIC returns an empty result for a missing DN. `get_by_dn` turns that into an explicit, structured message rather than a bare `[]`:

```json
{
  "found": false,
  "dn": "uni/tn-OT/BD-typo",
  "message": "No object exists at DN '...'. The DN may be mistyped, or the object may have been deleted. Verify it with search_classes() and query(), or re-derive the DN from a fresh query result."
}
```

A not-found usually means a stale or mistyped DN — re-derive it from a fresh `query` result rather than reconstructing it from memory.

---

## Examples

```python
# Direct read
bd = await get_by_dn("uni/tn-OT/BD-servers")

# Config-only, with children embedded
bd = await get_by_dn(
    "uni/tn-OT/BD-servers",
    config_only=True,
    include_children=["fvSubnet", "fvRsCtx"],
)

# Handle the not-found case
obj = await get_by_dn(dn)
if obj.get("found") is False:
    # DN is stale — re-derive it via query
    ...
```

---

## Raises

| Exception | Condition |
|---|---|
| `UnknownClassError` | A class in `include_children` is not in the catalogue. Raised before anything is sent. Not raised when attribute checking is relaxed (see [Child classes are checked](#child-classes-are-checked)). |
| `UnknownAttributeError` | A name in `fields` is not a property of the returned object's class nor of any `include_children` class. Raised after the fetch. Not raised when attribute checking is relaxed (see [Field projection](#field-projection)). |
| `ApicRequestError` | APIC returned a non-2xx, non-auth response — e.g. 400 for a malformed DN string, or a transient 5xx that never recovered. Carries the HTTP status and, when present, the APIC error text. Note: a DN that is well-formed but points to nothing does **not** raise — it returns `{"found": False, ...}` (see [Return value](#return-value)). |

---

## Related

- [`query`](query.md) — class-scoped queries when you do not have a DN
- [`get_schema`](get_schema.md) — inspect a class before building a query
