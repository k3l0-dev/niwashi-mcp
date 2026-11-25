# Internals: Secret redaction

`mcp/src/niwashi_mcp/redact.py` — the one module that decides what is secret and
strips it. Everything an agent receives from `query`, `get_by_dn` and `count`
goes through it; `get_schema` uses it to label properties.

> **Interim.** The module stands in for `niwaki.secrets`, which niwaki 2.3 will
> publish with the same interface. Every decision lives in this file, the tools
> and request guards never read `niwaki._secrets` themselves, and the tests that
> do are isolated in `tests/unit/test_redact_interim_policy.py`; the switch is a
> change to one file and the tests next to it.

---

## Why

An APIC returns some secrets in clear, and writes them into its own logs. Measured
on the 6.0(9c) lab fabric, before this existed:

| Where | What an agent could read |
|---|---|
| `vnsCCred.value` | the password of an L4-L7 device credential, on 16 objects |
| `snmpTrapDest.secName` | the SNMP trap community, on 7 objects |
| `snmpCommunityP` | the community string, as the object's name *and* in its DN |
| `aaaActiveUserSession` | a session token, in the DN of 5 objects |
| `aaaModLR.changeSet` | the value of a credential or community as it was created |
| `eventRecord` | the OTP seed of a local user, session tokens, a trap community |
| `aaaSessionLR.sessionId` | the session token, on 148 records |

The server is read-only, but a read-only tool that returns these is still a
disclosure to whoever the agent is talking to. And withholding the *values* is not
enough: a tool that answers `filters={"value": "guess"}` with one object has told
the agent the guess was right.

---

## What is secret

The policy is niwaki's, so the SDK's `snapshot.take()` and this server cannot
disagree about what a secret is:

| Source | What it gives | Size on the 6.0(9c) catalogue |
|---|---|---|
| The schema's `secure` flag, read by `catalog.secure_properties()` | what the APIC itself omits from reads | 169 properties on 112 classes |
| `niwaki._secrets.SECRET_VALUE_POSITIONS` | what the flag misses and the APIC echoes in clear (`vnsCCred.value`, `snmpTrapDest.secName`, ...) | 25 pairs |
| `niwaki._secrets.SECRET_DN_POSITIONS` | classes whose *name* is the secret, so it is in their DN and every descendant's | 4 classes |
| `_INTERIM_POSITIONS`, in `redact.py` | positions measured live that niwaki 2.2 lacks and 2.3 will carry | 4 pairs |

`_INTERIM_POSITIONS` is `aaaSessionLR.sessionId`, `aaaUserProfile.otpkey` and
`pkiFabricSelfCAEp.currCertReqPassphrase` / `oldCertReqPassphrase`. A test fails
when niwaki starts carrying one, so the entry is deleted rather than becoming a
second source of truth.

### Where this goes beyond niwaki 2.2

niwaki redacts a configuration *snapshot*; this answers arbitrary queries.

* **A curated position on an abstract class applies to its concrete subclasses.**
  niwaki lists `snmpACommunityP.name`, a class no response ever carries; read
  literally the entry covers neither concrete class. `snmpCommSecP` is named by
  its community string exactly like `snmpCommunityP`, and niwaki's DN list does
  not have it, so it is derived: a class is named by a secret when a secret
  property is among its naming properties.
* **The DN is cut, not merely warned about.** `snapshot.take()` collects a warning
  — "this DN carries a secret; it cannot be redacted in place" — and publishes the
  DN. Here the segment's value is replaced wherever a DN appears.
* **Free text is scrubbed** (below). niwaki's snapshot never reads an audit log.

### Startup guard

`niwaki._secrets` is private to niwaki, which is pinned to one minor
(`niwaki>=2.2,<2.3`) for exactly that reason. A copy of its tables would go stale
silently, so `verify_policy()` runs in the lifespan — after `verify_catalogue()`,
before the APIC session opens — and stops the server, naming what moved, when:

* a name it reads (`SECRET_VALUE_POSITIONS`, `SECRET_DN_POSITIONS`,
  `secret_dn_classes`, `PROP_PATTERN`) is missing or has changed shape; or
* the policy resolves but no longer gives the known answers — `vnsCCred.value` a
  secret, `aaaUser.pwd` flagged, `snmpCommunityP` named by its secret,
  `aaaSessionLR.sessionId` a secret, `fvBD` untouched — or redacting a
  known secret-named object and a known audit record does not remove the secret; or
* a class named by a secret has an RN format that is not a literal prefix followed
  by its one naming property, so the secret could not be found in a DN.

A server that cannot say which values are secret must not start: serving without
the policy would hand credentials to an agent.

---

## The interface

The names follow what niwaki 2.3 plans to publish. Those after the rule are
niwashi's own and stay when the rest becomes a re-export.

| Function | Answers |
|---|---|
| `REDACTED` | `"<redacted>"`, the same string as `niwaki.snapshot.REDACTED` |
| `is_secret(class, prop)` | whether that value is a secret. Fail-closed: an abstract class answers for its concrete subclasses, and a property of a class the catalogue does not know is judged by its name |
| `secret_props(class)` | the properties whose value is withheld (for the `get_schema` label) |
| `free_text_props(class)` | `{"changeSet"}` on the 13 audit, event, fault and session record classes, else empty |
| `has_secret_dn(class)` | whether objects of the class are named by a secret |
| `dn_has_secret(dn)` / `redact_dn(dn)` | by RN prefix alone, with no class resolution; also inside `subj-[...]` brackets |
| `redact(class, attrs, *, on_unknown_class="heuristic")` | `attrs` with every secret withheld. Pure. `"keep"` leaves an unknown class alone, `"raise"` raises `KeyError` |
| `redact_object(item)` | the same on an APIC `imdata` item, recursively |
| `redact_text(text, *, affected_class=None, affected=None)` | free text scrubbed, both `changeSet` formats |
| — | — |
| `redact_results(objects)` / `redact_one(obj)` | the flat shape the tools hold (`_class`, attributes, `_children`); returns the objects, how many attribute values changed, and whether any was free text |
| `check_request(...)` / `check_dn(...)` | the request guards: raise `SecretAttributeError` / `SecretDnError` |
| `mark_secret_properties(schema, class)` | adds `"secret": true` / `"scrubbed": true` to `get_schema`'s `property_details` |

---

## What is done to a result

Applied to each object, children and subtree objects included, **after** any
`fields` projection, so that what is counted is what the agent receives and a
secret asked for by name is still the marker.

1. **Values.** A secret attribute is replaced by `<redacted>` — **even when it is
   empty**, so "set" and "unset" cannot be told apart. An attribute that is absent
   (projected away, or removed by `config_only`) stays absent.
2. **DNs.** The value after a secret RN prefix is replaced — `community-`,
   `actsession-`, `delsession-`, `sitejwtpubkey-` — in every string attribute:
   `dn`, `rn`, a `tDn`, an audit record's `affected`, and inside
   `subj-[uni/.../community-x]/mod-1`. The prefix is read from each secret class's
   RN format, so it is not a list kept here.
3. **Free text** (below), on the classes that carry it.
4. **Unchanged objects are the same objects.** Nothing is copied when there is
   nothing to withhold, so an ordinary `fvBD` or `fvTenant` is byte-for-byte what
   the APIC sent.

### Free text

An audit record of a credential being created writes `value:<the password>` into
`changeSet`. Withholding every `aaaModLR` would break a documented use case, so the
text is scrubbed instead.

`changeSet` is parsed in **both** formats the APIC writes:

```text
name:admin, value:<secret>, userdom::all:                          creation — 12,437 of the lab's 12,493
name (Old: N/A, New: public), descr (Old: , New: )              modification — 38
```

* The **class** whose properties are secret comes from `affected_class`, else from
  the last RN of the `affected` DN (`.../cDev-C/cCred` is `vnsCCred`; a name that
  two classes share is both of them), else from every property name that is secret
  on *any* class — which over-masks and never raises.
* In the creation format a value runs to the next `, name:` where `name` is a
  property the class declares (or `userdom`, which the APIC appends), so a secret
  containing a comma and a colon is not cut short. In the modification format the
  group is matched by balanced parentheses, and one the APIC truncated runs to the
  end.
* A secret met in a DN is then removed, as a whole word, from the rest of the
  record's `changeSet` and `descr` — never from a DN: a trap community that is also
  its policy's name (measured on the lab) stays visible *as the policy's name*.

Two guards fail closed, replacing the whole field by `<redacted>`:

* text that names a secret property somewhere other than where a pair starts, so
  it cannot be parsed; and
* text that still holds a secret longer than six characters once scrubbed, so it is
  embedded in a larger token no word-pass reaches.

**This is best effort, and the envelope says so** (`note`, when it applied). A
secret in a form nothing here recognises can remain.

---

## What is refused

None of this is governed by `NIWASHI_MCP_STRICT_ATTRIBUTES`: that switch relaxes a
question about what the catalogue knows, and this is not one. The checks run before
the attribute check, before anything is logged, and before the APIC is called.

| Request | Refused with | Why |
|---|---|---|
| `filters`, `order_by`, `filter_expr` operand or `group_by` on a secret attribute | `SecretAttributeError` | an equality filter is an oracle (one hit confirms a guess); `order_by` leaks the order; `group_by` prints the values as keys |
| the same on `changeSet` of an audit, event, fault or session record | `SecretAttributeError` | a `wcard` on it would confirm what was scrubbed out of it |
| `dn` or `rn` of a class named by a secret, in the same places | `SecretAttributeError` | `wcard(snmpCommunityP.dn, "ubli")` confirms a substring of the community |
| a DN, `scope_dn`, filter value or `filter_expr` that spells a secret RN segment | `SecretDnError` | `get_by_dn(".../community-public")` answering found or not found checks a guess |

Attribute references in `filter_expr` are found by shape — every `Class.attr`
outside a quoted value, whatever the spacing, nesting or case — and not by the
conservative extractor the typo check uses, which deliberately leaves a reference
it cannot place unchecked.

### `get_by_dn` on a secret DN: refuse, do not redact the answer

Either the DN is refused, or the answer is redacted and *existence* still shows.
The second answers a guess ("found" or "not found"), so the first is the only safe
one. The decision is made **on the DN's shape**, before the APIC is asked, so an
existing and a non-existing DN get an identical answer (a test sends four guesses
and asserts one message). The objects are still reachable by their class or their
parent's DN, redacted.

### What the agent is told

Without saying what: a count and a note.

| Tool | Key | When |
|---|---|---|
| `query` | `redacted` (int) and a `note` sentence | something was withheld; **absent otherwise**, so an ordinary result is byte-identical |
| `get_by_dn` | `_redacted` and a `_note` | the same |
| `count` with `group_by` | `redacted` | a group value was cut — how many objects. Not "every secret the fetch held": withholding a credential's password while grouping credentials by name changes nothing the agent reads |

`get_schema` marks `"secret": true` on a secret property (and on `dn`/`rn` of a
class named by one) and `"scrubbed": true` on `changeSet`. The names stay listed.

---

## What it does not cover

* **A fabric newer than the catalogue.** A class or property the catalogue does not
  know is covered only by the curated names and, for an unknown class, by the
  name heuristic (niwaki's own sweep pattern: `password`, `community`, `token`,
  ...). An unflagged secret in a new class is returned.
* **A secret reused as a name.** The policy is by position, not by value. A trap
  community that is also a policy's name is visible as that name where the policy
  is named. (An agent can infer the reuse; it cannot be prevented without hiding
  the name.)
* **Substring probes through DN and free-text attributes of other classes.**
  `wcard(aaaModLR.affected, "ommunity-pub")` does not spell the RN prefix, so it is
  not refused, and confirms that an audit record names a community starting
  `pub`. What such a record *shows* is scrubbed; what it *matches* is not.
  `descr` of an audit or event record can be probed the same way.
* **Free text the scrubber does not recognise.** See above: best effort.
* **A secret ambiguous between two classes** is scrubbed as a secret of both —
  false positives, never false negatives (see below).

### False positives, the useful values that are hidden

* An empty secret reads `<redacted>`: "this credential is unset" is no longer
  answerable.
* In a `changeSet` whose class cannot be told, every secret property name is
  masked, `name`, `key` and `value` included.
* `user-{name}` is the RN of `aaaUser` and of the SNMPv3 users: an audit record
  about either has `otpkey` masked.
* A secret property name that appears in prose (`value: ...` in a `descr`) on a
  class where it is secret makes the text unparseable, and the whole field is
  replaced.
* A trailing `changeSet` property the class does not declare, after a secret pair
  (other than `userdom`), is absorbed into that pair's value.
* A one-letter secret is removed, as a word, from the `descr` and `changeSet` of
  its own record.
* `dn` and `rn` of a class named by a secret cannot be filtered on.

---

## Duplicates

A query on an abstract class (`snmpACommunityP`) returns an object once per
concrete class that matches it — the APIC reports `totalCount: 2` for the same
DN. Redaction treats each row as an object of its own, so each is redacted and
each counted. The client's `fetch_all` already drops a repeated DN, **before**
redaction: two distinct secrets that redact to the same DN stay two objects. A
single page is not deduplicated, because `total_available` counts the duplicates
and `truncated` is computed from it; dropping rows would make a complete page look
truncated.

---

## Cost

About 1.4 microseconds an object for an ordinary object — one dictionary lookup
and a substring test — 7 for an audit record whose `changeSet` is parsed, and up
to ~40 when each record carries a distinct secret (a pattern is compiled per
secret). Against the lab APIC the same objects cost 0.2 to 24 ms each to fetch, so
redaction is 0.01 to 5 percent of a call. `tests/perf/test_redaction_perf.py`
holds the ceilings.

---

## Testing

| File | What it pins |
|---|---|
| `tests/unit/test_redact.py` | the interface: every kind of secret, both `changeSet` formats, class resolution, the oracles. Written against `redact` alone, so it survives the switch |
| `tests/unit/test_redact_interim_policy.py` | the coupling to niwaki's tables and the startup guard. **Deleted at the switch** |
| `tests/unit/test_redact_wiring.py` | the lifespan calls the guard, and the prompt surface says what the code does |
| `tests/integration/test_redaction_tools.py` | the tools end to end over a stub backend and the real MCP client, including a sweep for any secret anywhere in any output or log |
| `tests/live/test_live_redaction.py` | the lab APIC, read-only, asserting on booleans and counts — never a value |
| `tests/perf/test_redaction_perf.py` | a 5,000-object result |
