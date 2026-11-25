# Changelog

All notable changes to this project will be documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) — versioning follows [SemVer](https://semver.org/).

---

## [Unreleased]

### Added

- **`get_schema` reports each relation's cardinality and how it names its
  target.** `relationTo[*].cardinality` was `""` on all 2,992 entries and the
  docs told agents not to branch on it: the relation dicts in the catalogue's
  `residual` blob carry none. The `relation` table does, so `get_schema("fvBD")`
  now returns `"fv:RsCtx": {"targetClass": "fv:Ctx", "cardinality": "n-to-1",
  "kind": "named"}` — many bridge domains, one VRF each, and a name
  (`tnFvCtxName`) rather than a DN. `kind` is `named` or `explicit` (`tDn` only,
  no name attribute), which is what tells an agent whether a `tn…Name` attribute
  is worth reading on the relation object. `relationFrom` entries gain the same
  two keys, read from `sourceClass` to the class.

  Additive: every existing key and `targetClass` / `sourceClass` value is as it
  was. 6,247 of 6,249 entries are filled; the two the table has no row for, both
  on `fvCtx` and both naming cloud relation classes absent from the catalogue,
  keep `cardinality: ""` and get no `kind`. It costs one indexed query per call
  (about 0.02 ms) and, for the 1,597 classes that have relations, a median 52
  bytes — at most 2.4 KB, on the classes with the most.

  Done in a new `catalog.annotate_relations()`, applied by the tool, and not in
  `load_schema()`: that function is the projection the recorded baseline and the
  jsonmeta oracle pin, and both record `cardinality` as empty. The baseline is
  untouched and passes unmodified. `verify_catalogue()` now requires the
  `relation` table and probes it, because its key is the relation class in wire
  notation and a move to colon notation would make every lookup miss silently —
  `cardinality` would just go back to empty. SKILL.md, the tool docstring and
  the docs teach the two keys.

- **`count(group_by="code")` names the fault codes.** `{"F0546": 60}` leaves the
  reader to say what `F0546` is, and an agent that does so from memory is how a
  fault table gets fabricated (the July inventory, in the entry further down, is
  one). The catalogue's `fault` table maps each code to the rule name the schema
  gives it, so the response now carries `group_names`, `{"F0546":
  "fltEthpmIfPortDownNoInfra", "F0467": "fltFvNwIssuesConfig-failed"}`, in the
  order of `groups`. Run against a lab fabric (616 faults, 39 distinct codes): 30
  resolve and 9 do not — 8 threshold-crossing alerts (`tca-*`, whose codes are
  minted at runtime) and one FSM fault (`F608163`); the table's codes run F0020
  to F4680, and `sum(groups) + ungrouped == count` held.
  Those are left out rather than guessed, and the docs say what a missing code
  means and to read the object's own `rule` and `descr` instead.

  Additive and narrow: the key exists only for `group_by="code"` and only when
  at least one code resolves; every other response is byte-identical. It is the
  one place a code appears without an object to explain it. `query` and
  `get_by_dn` results are left alone on purpose: a fault object already carries
  its own `rule` (`fv-nw-issues-config-failed` for `F0467`, present on all 616
  faults read from the lab), so a second name would be redundant on every row of
  a fetch of thousands, and a key the APIC does not send has no coherent answer
  under `fields`, which keeps only the attributes asked for. `verify_catalogue()`
  requires the `fault` table and checks that `F0467` resolves.

- **`fields` projects `query` and `get_by_dn` results onto the attributes
  asked for.** A class carries ~40 attributes per object and `fetch_all` can
  hold 5,000 objects; an agent that needed a name and a state still received
  everything, and the answer is what fills its context. `fields=["name",
  "arpFlood"]` reduces each result — and each `_children` entry of a class named
  in `include_children` — to those, plus `dn`, `_class` and `_children`, which
  are always kept: an object that cannot be addressed or identified is no use to
  the next call. Names are validated against the queried class or any
  `include_children` class, because a misspelled field would return objects that
  merely lack the key, which reads as "the attribute is empty". `fields=[]`
  keeps only the three fixed keys and the `note` says so; it is not the same
  request as omitting `fields`.

  Children that arrive another way — `rsp_subtree_include="faults"` brings
  `faultInst` children — are returned whole: `fields` was never checked against
  them, and reducing them anyway turned each fault into its `dn` and `_class`
  with no note, deleting the severity the caller had asked the APIC for. A name
  that exists only on such a child is therefore refused, since there is nothing
  for it to select. An `include_children` item may be comma-joined
  (`["fvSubnet,fvRsCtx"]`, two classes on the wire); every check reads it that
  way.

  Projection happens after the APIC answers, so it saves the response, not the
  APIC's work, and changes neither `total_available`, `truncated` nor paging.
  `get_by_dn` can only validate after the fetch — the class is not known before
  it. In relaxed mode (see Changed) an unknown name is kept and the doubt is
  said in the `note`, or in a `_note` key on `get_by_dn`'s object, which has no
  envelope to carry one.

- **`count(group_by=...)` answers "how many X per Y?" in one call.** `group_by`
  takes any property of the class, or `"tenant"`, read from the DN's
  `tn-<name>` segment because a tenant is not an attribute of the objects it
  owns (20 classes, `pingExecTn` among them, carry a real `tenant` attribute,
  and that one wins). The APIC cannot group, so the matching objects are fetched
  and tallied in the server: it costs a full `fetch_all` where a plain count
  costs one request, and the docs say so. The response gains `group_by`,
  `groups` (largest first, ties by value), `ungrouped`, `complete` and `note`;
  without `group_by` it is exactly what it was.

  Objects with no value to group on are counted in `ungrouped`, never under a
  `null` key: a JSON key is a string, and every string — `"null"`, `"None"`,
  `""` — is a value some attribute can hold. An empty string *is* a value and
  keeps its own group. Tenant derivation splits the DN only outside square
  brackets, so an object under `uni/epp/fv-[uni/tn-A/ap-B/epg-C]/...`, which
  merely *names* tenant A's EPG in a key and is not in A's subtree, stays
  ungrouped instead of being credited to A.

  **It never returns a partial breakdown.** If the fetch hits its cap, or
  fetches a different number of objects from the total it was checked against,
  `complete` is `false`, `groups` and `ungrouped` are `null`, `count` stays the
  true total, and `note` says how to narrow. A partial list of groups reads as a
  finding ("only two tenants have faults") when it is only where the fetch
  stopped. The note names both ways a fetch stops short — the cap, or the APIC
  ending its pages early — as `query`'s does. A set the plain count already
  shows to be larger than one `fetch_all` can hold is withheld without fetching
  it, rather than after walking 5,000 objects to reach the same answer, and a
  set of zero is answered (`groups: {}`) without a fetch.

- **The fabric's APIC release is compared with the catalogue's at startup.**
  The log named only the release the catalogue was built from, which says
  nothing about the fabric the server is connected to; an operator on another
  train learned of the gap when a query came back empty, and an empty result
  reads as "there are none". After the login the server now reads the
  controller's running release (`firmwareCtrlrRunning`, filtered to
  `type == "controller"`), normalises both strings — `apic-6.0(9c)`,
  `APIC-6.0(9C)` and `6.0(9c)` are one release — and logs a `WARNING` naming
  both and pointing to `SUPPORTED-APIC.md` when they differ.

  It is advisory and never stops startup. A failed lookup, an empty answer, or a
  string it cannot read is logged as *unknown* and leaves `fabric_version` `None`
  in the lifespan context; a string the server merely failed to parse is never
  reported as a different release, since claiming a mismatch it cannot establish
  is the same kind of wrong answer the warning exists to prevent. Only the first
  controller's release is read, so a rolling upgrade can briefly run mixed
  releases the check does not see. The lookup is `ApicClient.controller_version()`.

- **SKILL.md and the server instructions are pinned to the new surface.** Eight
  tests tie what the agent reads to the code that gives it meaning: that
  `fields` and `group_by` are shown in a call on a tool that takes them; that the
  envelope table has a row for every key `QueryEnvelope` declares, and the
  instructions' key list names them all; that every key
  `count` can return is named and set; that every exception named in backticks
  exists, and the switch an operator is told to set is the variable the code
  reads, with `false` a value it honours; that the 200-per-page and 5,000-object
  figures quoted in three places are the client's constants; and that the stale
  sentences this change removed stay removed.

  Mutation-tested: dropping the `fields` row, a 5,000 that disagrees with
  `_MAX_OBJECTS` (in SKILL.md, and in the instructions), a mistyped switch name,
  a renamed exception, `groups` withheld as `{}` instead of `null`, and each
  stale sentence put back each fail the test written for it.

- **SKILL.md's teaching vocabulary is linted against the catalogue.** Every
  backticked camelCase name in the skill file must be a real class, a real
  property, or a documented exception with a reason. A typo in a teaching
  example is worse than one in code: code fails, but an agent imitates the
  example verbatim, queries the mistyped name, and reads the empty result as
  "there are none". Mutation-tested — a typo'd class or property name each fail
  it — and it also asserts the deliberate wrong-case example (`fvBd`) stays
  wrong, so the case-sensitivity lesson cannot silently invert.

- **What a tool does when the backend fails.** `StubBackend` never raised — not
  a 400, not a 500, not a timeout, not malformed JSON — so no integration test
  saw a tool meet a failing backend. Fourteen tests now do, through a backend
  that raises. The one that matters most is `count`: returning `{"count": 0}`
  because the APIC refused the request is a wrong answer delivered with total
  confidence, and the tool's own docstring tells an agent that a tool error "is
  a failure to answer, not an answer of zero". Nothing enforced it. Same for
  `get_by_dn`, which has a legitimate `{"found": false}` shape a failure must
  not borrow — "no object exists there" and "I could not ask" lead an agent to
  opposite conclusions. Mutation-tested: making either swallow its exception
  fails five and four tests respectively.

- **Startup and footprint budgets.** Both sit on the critical path of
  `uvx niwashi-mcp` and neither was measured. Cold process, three runs:
  **419 / 420 / 436 ms** for import plus a full index rebuild — against the
  `descriptions_load_cold = 33.96` ms the recorded baseline still carries from
  the pre-2.0 JSON path, an 11.5x regression written into a committed file
  nobody read, because that file records timings without asserting them.
  Resident footprint ~159 MB once the tokenised index is warm. Ceilings are
  sanity bounds with 3-9x headroom for a slower runner, and the build-once
  property is asserted as a ratio rather than a constant, since a constant only
  passes on the machine it was calibrated on.

- **The MCP contract is under test.** An entire class of defect had no coverage:
  what the server *advertises*. Every other test drives a tool function directly
  in Python, so none saw the tool list a client actually receives — names,
  titles, annotations, parameter schemas, required fields. That surface is the
  whole integration contract, and a defect in it does not raise: the tool simply
  stops being offered, or is offered with a shape the caller cannot satisfy.
  Twelve tests now assert it through FastMCP's own registry, no network
  involved. Mutation-tested: a sixth tool appearing, a tool losing
  `readOnlyHint`, the server changing its announced name, a title emptied, and
  the `GROUNDING` section vanishing from the instructions each fail their test.

- **The projection filters and search priors are pinned.** Six decisions that
  were correct and invisible: an audit removed each in turn and all 539 tests
  stayed green. Empty `default` values (86 properties, 57 classes), the
  `defaultValue` enum marker, the `"null"` comment sentinel, and the three
  structural priors — stats/telemetry, abstract, Rs/Rt. The stats prior carries
  **4,769 of 15,239 index entries (31.3 %)** and the entire evaluation stack was
  blind to it: not one of the 74 golden queries surfaces a telemetry class, so
  removing the penalty changed no metric.

  The abstract prior took measuring to test honestly. Built on "bridge domain"
  the test asserted nothing — `fvBD` leads on an exact jargon match whether the
  penalty exists or not. Removing it flips exactly two queries: "interface"
  poeIf → ipIf and "domain" bgpDom → l2Dom, both abstract. The test uses those.

- **The July fault inventory is frozen as a fixture, and scored.** It existed
  only as a line in a private note: 22 faults, `F0104x1, F0103x1, F609026x7,
  F2247x11, F3951x2`, verified twice live on 2026-07-20. A model summarising
  that list into a table named two codes present in none of it — with confident
  descriptions attached — while under-counting three of the five real ones.
  Every tool call in that transcript was correct; the fabrication happened after
  the last one returned.

  `agent-eval/lib/fabrication.py` scores an answer as precision at full recall
  over the set of codes: one fabricated code scores **0, not 0.8**. Grading it
  as "four of five, 80 %" gets the incentive backwards — a table that is 80 %
  right and 20 % missing is usable, because the reader knows it is partial; one
  that is 80 % right and 20 % invented is worse than useless, because nothing
  marks which fifth is fiction and it reads more authoritative, not less.

- **`SKILL.md` gains the fourth grounding counter-example**, on summarising a
  list into clusters or a table — the case above. It ships with its automated
  form rather than as advice alone, which was the condition for adding it.

### Changed

- **Dev tooling moved to pytest 9.2.0, pytest-asyncio 1.5.0 and ruff 0.17.6.**
  Ruff reports no findings under the existing rule selection. pytest-asyncio
  1.5 warns when `asyncio_default_fixture_loop_scope` is unset, so it is set to
  `fixture-scope`, which keeps the current loop behaviour. Lock only; no runtime
  change.

- **`get_schema` samples very long enumerations.** A property's `options` is
  cut at max(`list_limit`, 100) and the entry gains `optionsTruncated:
  {returned, total, note}`. `faultInst.code` enumerates 80,512 values, so
  `get_schema("faultInst", properties_filter=["code"])` returned about 880 KB —
  the largest tool result in agent-eval's tier F, on every server version — and
  now returns about 5 KB. Only 13 of the catalogue's 3,947 enums exceed 100
  values (median 3, 99th percentile 33), so ordinary enums stay whole at the
  default `list_limit`. The note tells the agent that a value missing from the
  sample may still be valid. `catalog.load_schema()` is unchanged; the bound is
  applied at the tool surface like the `dnFormats` one.
- **niwaki 1.8 → 2.2: the object model is the same, the catalogue is 10 MB
  lighter.** `niwaki>=2.2,<2.3`, still a single minor. The one thing that moved
  under this server is the private table it reads properties from: 2.1 replaced
  `prop` (one row per class and property) with `prop_sig` (one row per distinct
  property signature, 25,901 of them) and `class_prop` (which classes carry
  which signature, the same 332,297 memberships). `catalog.py` now reads the
  join, `verify_catalogue()` requires both tables, and the startup guard's
  messages name the new pin. Everything else this server reads — the other
  tables, the blob encodings, the manifest keys, 15,452 classes, APIC 6.0(9c) —
  is unchanged, and the catalogue is 25,993,216 bytes instead of 36,229,120
  (the wheel 16.8 MB instead of 18.8 MB).

  Checked rather than assumed: the joined rows equal the old `prop` rows
  exactly (332,297 of 332,297, every column), the rebuilt search index is
  byte-identical to the recording, and the baseline passes unmodified. One
  difference had to be handled: `prop` was keyed by `(class_id, wire_name)`, so
  rows came back sorted by wire name without asking; `class_prop` returns them
  in signature order. Search-index labels are built in that order, so
  `prop_labels` now says `ORDER BY wire_name`, and a test checks the order
  against the raw rows. Without it the digest test fails — the index changes —
  but a bare "the index changed" says nothing about why.

- **An attribute the catalogue does not know is now refused instead of sent —
  a behaviour change for every `query` and `count` caller.** Before,
  `query("fvBD", filters={"arpFlod": "yes"})` went to the APIC, which answered
  HTTP 200 and `[]`, and `count` answered `0`: an invented name was
  indistinguishable from "there are none", the failure this server exists to
  prevent. Now every name the caller supplies is checked against the catalogue
  before the request is built — the keys of `filters`, the attribute of each
  `order_by` term, every class in `include_children`, and the `Class.attr`
  operands of `filter_expr`. The first unknown attribute raises the new
  `UnknownAttributeError`, an unknown class `UnknownClassError`, nothing reaches
  the backend, and the error carries the closest real names. For an abstract
  class the accepted names are the union of its own and its concrete subclasses',
  so `hostBasedRouting`, declared by `fvBD`, is valid on `fvABD`. In
  `filter_expr`, quoted values are masked before looking, so a dot inside a
  string literal is never read as `Class.attr`, and anything not recognisably
  `Class.attr` passes through untouched.

  The catalogue describes one APIC release, and a fabric on another may
  legitimately carry a name it lacks, so there is an escape hatch:
  `NIWASHI_MCP_STRICT_ATTRIBUTES=false` sends the request anyway and says so —
  appended to `query`'s `note`, never replacing the truncation note, or as a
  `warnings` list on `count` that exists only then — and logs each problem. An
  empty result beside such a note is not a zero. The switch is read on every
  call, and any value other than `false`/`0`/`no`/`off` keeps it strict, so a
  typo cannot quietly disable the check. The class named by `class_name` is
  outside it: that check is older, and a typo there has no legitimate reading.

  Not done: a guard for the relation-class trap in SKILL.md section 8, where a
  `filter_expr` on `state`, `stateQual` or `tDn` returns zero results. Those are
  real properties, so this validation passes them, and the premise a dedicated
  guard would rest on was not measured for this change.

- **`get_by_dn` now refuses an unknown class in `include_children` — a
  behaviour change.** It used to send the request and return the object with no
  `_children` and no warning, which reads as "this object has none": the APIC
  embeds nothing for a class it does not know. The classes are checked, as
  `query` checks them, before the request is built — `UnknownClassError`,
  nothing sent — and with `NIWASHI_MCP_STRICT_ATTRIBUTES=false` the request goes
  out and the object carries a `_note` saying which class was not recognised
  (added to, never replacing, the note a `fields` warning writes). A
  comma-joined item is read as the classes it is.

- **`verify_catalogue()` refuses startup on two more kinds of damage.** It now
  also fails when the catalogue's `inherits` table is missing or its columns
  have moved, and when the abstract-class probe — `fvABD`, whose union with its
  concrete subclasses must be wider than its own properties — comes back no
  wider. Both are quiet failures: every query still runs, but an abstract
  class's accepted attributes collapse to its own and valid names start being
  refused as unknown.

- **Every `query` envelope has a new key, `fields`, and `query` and `count`
  advertise a typed `outputSchema`.** `fields` echoes the projection and is
  `null` when none was asked for, so it is present on every result — the one
  change a caller who never passes `fields` can see. Separately, both tools were
  annotated `dict[str, Any]`, so FastMCP advertised
  `{"type": "object", "additionalProperties": true}`: a client was told "an
  object" and nothing about the keys an agent must read — `truncated`,
  `complete`, `next_page`. They now return `QueryEnvelope` and `CountEnvelope`
  typed dicts and the schema names the keys. `count`'s `group_by`, `groups`,
  `ungrouped`, `complete`, `note` and `warnings` are declared optional, so a key
  added later is an addition to the schema rather than a break of a client that
  validates against it. `results` items stay free-form: which attributes an
  object carries depends on its class.

- **The rest of the audit's bogus-test list — deleted or rebuilt, none left as
  they were.** Two had no reason to exist; the other five were necessary and
  badly built.

  Deleted: `tests/eval/` in full. It asserted Recall@1 >= 0.60 and
  Recall@5 >= 0.85 against an implementation delivering 0.784 / 0.946 — floors
  so far below reality that its own docstring's claim, that it would catch
  removing the `isConfigurable` prior, is measurably false: removing it drops
  R@1 to 71.6 % and the module still passed. Of twelve scoring mutants it killed
  two; `tests/baseline/` kills twelve, because it asserts the metrics and every
  query's exact top-5 as equalities. Its one non-redundant check, that the
  golden set has not shrunk, is folded into `test_search_source.py`. Also
  deleted: two constants in `baseline/capture.py` pointing at `data/` paths
  removed in 2.0, and five contentless `ctx.warning.assert_called_once()`
  assertions replaced rather than dropped — those warnings reach the MCP client
  as log notifications, so they are agent-visible text and their content is the
  contract.

  Rebuilt: `test_query_and_count_both_validate` counted a substring in
  `main.py`'s source, which passes on a comment, passes on a call in dead code,
  and fails on a benign refactor — factoring the shared guard into one helper
  would have broken it while improving the code. It now patches
  `catalog.class_exists` and asserts neither tool reaches the backend for a
  rejected class. `test_metadata_with_none_values_does_not_crash` required an
  `AttributeError`, asserting the opposite of its own name and guaranteeing that
  whoever fixed the gap would see a red suite; the gap is closed in
  `_build_entry` instead and the test asserts what it is called.
  `test_empty_filter_is_fastest` measured two paths and then asserted two
  unrelated constants — the comparison in its name was never made; renamed to
  what it checks. Two `all(...)` assertions were vacuous on an empty list, which
  is exactly what a broken call returns; they now assert non-empty first. And
  `test_awkward_shapes_match_exactly` skipped whichever of its twelve difficult
  classes were missing from the frozen fixtures; it now asserts that set is
  exactly the four known to be absent, so a fixture dropping out fails loudly
  rather than shrinking the test in silence.

- **Six tests that passed for the wrong reason now assert what they claim.**
  Found by the audit, each verified by breaking the thing it guards:

  - Three clamp tests asserted `len(results) <= 200` against a three-object
    fixture — true whether the ceiling was 200, 9,999, or absent. They now
    assert the clamped value that reaches the backend, and still check the
    envelope describes what came back. Removing `query`'s clamp fails seven.
  - Four search-limit tests used `<= 50` where any lower ceiling also passed; a
    regression to 10 was invisible. Now equalities.
  - The truncation-note test asserted `note is not None`. Replacing the entire
    note with `"ok"` passed — and that note is the only text telling an agent
    not to read a partial page as a total. It now asserts what the note says.
  - `test_no_readable_name_leaks_across_configurable_classes` said "swept, not
    spot-checked" and covered 150 of 3,010 configurable classes (5 %), picked by
    an unordered `SELECT ... LIMIT 4000`. The full sweep costs ~0.12 s, so the
    sampling bought nothing and cost the guarantee. It sweeps, and asserts its
    own coverage.
  - `test_get_schema_known_class_returns_required_fields` carried a `skipif` on
    a `data/schemas` directory that 2.0 deleted and `.gitignore` excludes, so it
    skipped in CI and in every clean checkout. Measured on `git archive HEAD`:
    one skip, this test. The body never needed the directory.

### Fixed

- **`fetch_all` capped a "fetch everything" call at 500 objects, not the 5,000
  it advertised.** It paged by the caller's `limit` and stopped after 25 pages,
  so the default `limit=20` ended a call at 25 x 20 = 500 objects and reported
  `complete: false` on a 600-object set the cap was never meant to touch; a set
  of exactly 25 x `limit` objects was reported incomplete too, because the loop
  fell out of its `for` without a `break` on a page that had left nothing
  behind. The page size is now fixed at 200 — the largest page `query` already
  sends, so a full 5,000-object walk is 25 requests instead of 250 — and the cap
  is 5,000 objects, which lands on a page boundary: nothing is fetched past it
  and nothing trimmed. After each page the walk checks whether everything the
  APIC's `totalCount` reported is in hand (`complete: true`, with no extra empty
  request for an exact multiple of the page size) before asking for another.

  Two behaviours differ for any caller of `fetch_all`. **`limit` no longer means
  anything under it** — it is not the page size any more, and `page` was already
  ignored. And **an early end is now an incomplete result**: a short or empty
  page while `totalCount` says more is due — the APIC ran dry — used to be taken
  as the natural end and reported `complete: true`; it is now `complete: false`
  with a logged warning, and the `note` covers both ways a walk can stop short,
  the cap and the APIC's pages ending first. With no `totalCount` at all a short
  page is still trusted as the end, as before.

  **An object that arrives twice is kept once and counted once.** The set is not
  frozen while it is walked: an object created or deleted early in it shifts
  every later page by one, so the object on a page boundary can arrive twice and
  another be skipped. Counting the repeat toward `totalCount` made the walk reach
  the count one object short and report `complete: true` with a duplicate in the
  answer (and `count(group_by=...)`, which tallies the same objects, would have
  counted it twice). The walk now keeps the first copy of each `dn` — objects
  with no `dn` are all kept — compares the distinct count with `totalCount`, and
  logs how many it dropped. A short page is still judged on what the APIC sent,
  not on what was kept.

### Security

- **`query`, `get_by_dn` and `count` no longer hand an agent the secrets an APIC
  returns in clear.** Measured on a 6.0(9c) lab fabric before the fix: the
  password of an L4-L7 device credential (`vnsCCred.value`, 16 objects), the SNMP
  trap community (`snmpTrapDest.secName`, 7), a community string that is both the
  name and the DN of its `snmpCommunityP`, and the session tokens that name
  `aaaActiveUserSession` objects were all returned as the APIC sent them, and
  `count(group_by=...)` would have printed any attribute as a group key. Nothing
  was redacted; the only "secure" in the source was a flag name in `catalog.py`.

  **What is withheld.** A secret attribute reads `<redacted>` in every object,
  children and subtree objects included, whatever `fields` asked for — and **empty
  ones too**, so "set" and "unset" cannot be told apart. A DN that passes through a
  secret (`community-`, `actsession-`, `delsession-`, `sitejwtpubkey-`) has that
  segment cut wherever a DN appears: `dn`, `rn`, `tDn`, an audit record's
  `affected`, inside `subj-[...]`. The policy is niwaki's own — the schema's
  `secure` flag (169 properties), the curated `SECRET_VALUE_POSITIONS` (25) and
  the secret-DN classes — so the SDK's `snapshot.take()` and this server agree.
  `verify_policy()` runs at startup, after the catalogue check and before the APIC
  session opens, and stops the server naming what moved: `niwaki._secrets` is
  private and a copy of it would go stale silently.

  **What goes beyond niwaki 2.2.** A curated position on an abstract class now
  reaches its concrete subclasses (`snmpACommunityP.name` names a class no
  response carries; `snmpCommSecP` is named by its community string too, and
  niwaki's DN list lacked it). The DN is cut, where niwaki's snapshot only warns.
  And four positions measured live and absent from niwaki 2.2 —
  `aaaSessionLR.sessionId`, `aaaUserProfile.otpkey`, `pkiFabricSelfCAEp`'s two certificate-request
  passphrases — are carried in a short supplement that a test drops when niwaki
  2.3 absorbs it.

  **Audit, event and session records.** The APIC writes secrets into its own logs:
  `aaaModLR.changeSet` carried credential and community values, `eventRecord` the
  OTP seed, session tokens and a trap community, `aaaSessionLR.sessionId` the
  session token. These are scrubbed, not withheld, so audit questions still work:
  `changeSet` is parsed in both formats (`prop:value, prop:value`, 12,437 of the
  lab's 12,493, and `prop (Old: x, New: y)`), the value of each secret pair is
  replaced using the class read from the `affected` DN (every secret property
  name when none fits — over-masking, never raising), a secret met in a DN is cut
  and then removed from the record's other text. Text that names a secret property
  but cannot be parsed, or that still holds a secret once scrubbed, is replaced
  whole. **This is best effort and the response says so.**

  **Requests that would reveal one are refused**, in both
  `NIWASHI_MCP_STRICT_ATTRIBUTES` modes and before the APIC is called:
  `filters`, `order_by`, `filter_expr` operands and `group_by` on a secret
  attribute, on `dn`/`rn` of a class named by a secret, or on `changeSet`
  (`SecretAttributeError`); and any DN, `scope_dn`, filter value or `filter_expr`
  that spells a secret RN segment (`SecretDnError`). `get_by_dn` refuses such a DN
  on its shape alone, so "found" and "not found" cannot check a guess — an
  existing and a missing DN get the identical answer. `filter_expr` operands are
  found by shape, not by the typo check's conservative extractor, so a predicate
  cannot hide one by how it is written.

  **The agent is told, without being told what.** `query`'s envelope gains an
  optional `redacted` count and a `note`; `get_by_dn` an optional `_redacted` and
  `_note`; `count(group_by=...)` an optional `redacted`. All are absent when
  nothing was withheld, so an ordinary object is byte-identical to what it was.
  `get_schema` marks `"secret": true` on a secret property (names stay listed) and
  `"scrubbed": true` on `changeSet`.

  **Cost.** 1.4 microseconds an ordinary object, about 7 for an audit record whose
  `changeSet` is parsed: 0.01 to 5 percent of what the same objects cost to fetch
  from the lab APIC (0.2 to 24 ms each).

  **Known limits**, documented in `docs/internals/redaction.md`: a fabric newer than
  the catalogue (an unflagged secret in a class it lacks, covered only by a name
  heuristic); a secret reused as a name elsewhere, which is visible as that name;
  substring probes through the DN and free-text attributes of other classes
  (`wcard` on an audit record's `affected` or `descr`); and free text in a form the
  scrubber does not recognise. There is no opt-out. Every decision sits in one
  module, `niwashi_mcp/redact.py`, whose interface follows what niwaki 2.3 plans
  to publish as `niwaki.secrets`, so the switch is a change to that file and its
  niwaki-coupled tests.

- `fastmcp` floor raised from 3.4.5 to 3.4.10 for the 2026-10-23 advisories, and
  the lock now resolves 3.4.11 (`certifi` 2026.6.17 → 2026.11.20 came along in
  the same `uv lock`). The floor moves, not only the lock, because an
  installation from the published package resolves from `pyproject.toml` and
  ignores `uv.lock` — a lock bump alone would leave `uvx niwashi-mcp` free to
  resolve a release the advisories cover.
- Lock moved for the transitive packages under fastmcp's transport and auth
  stack. Reached on `/mcp`: `mcp` 1.28.1 → 1.30.0 fixes GHSA-84m7-p3x7-pcfv
  (unbounded session accumulation in the StreamableHTTP transport), and
  `opentelemetry-api` 1.44.0 → 1.45.2 fixes GHSA-mf5v-w3w3-f6cg (quadratic
  `traceparent` parsing) and GHSA-xcq7-p3pf-p88h (unbounded `tracestate`).
  fastmcp extracts `traceparent` and `tracestate` from each tool call's
  request metadata, so a caller who reaches `/mcp` reaches that parser. With
  `mcp` 1.30 an idle stateful session closes after 30 minutes and a server
  holds at most 10,000 sessions; a client whose session has expired gets a 404
  and re-initialises.
  Pulled in but not reached by niwashi, which configures no fastmcp auth
  provider and uses its own API-key middleware: `dnspython` 2.8.0 → 2.9.2
  (GHSA-45c3-73f7-pv4m, GHSA-vvf4-9x63-8xvv; only `email-validator` loads it,
  and no wire or zone parsing runs), `authlib` 1.7.2 → 1.9.10 (empty HMAC key
  acceptance, GHSA-8m4g-j4wp-wgcg; the JWT and OAuth paths are not used),
  `joserfc` 1.7.4 → 1.8.0 (GHSA-9pg5-8c7x-5hfx, GHSA-jpxq-7h8p-2q76) and
  `pyjwt` 2.13.0 → 2.15.1 (GHSA-42vr-xj54-vc7v). `python-dotenv` 1.2.2 → 1.2.4
  is a direct dependency bumped in the same lock.

---

## [2.1.1] - 2026-08-09

**A silent wrong answer removed, and the documentation the agent reads made
true again.** Concurrent catalogue reads could return another class's schema
with no exception raised; that is now serialised. Separately, three pieces of
agent-facing text still described the pre-2.0 data layer — a directory of
jsonmeta files that has not existed since the object model moved into the
`niwaki` catalogue.

The five tools keep their signatures. Nothing in the request or response shape
changes.

### Added

- **The prompt surface is under test.** About 7,800 words reach an agent on a
  normal session — `mcp.instructions`, the five tool docstrings FastMCP forwards
  as tool descriptions, and `client/SKILL.md` — and nothing imported any of it.
  Line coverage could not help: `instructions` is one string literal, so
  `main.py` reported 98 % while its largest agent-facing artifact went
  unexamined. Eight tests now pin what can be mechanically compared — that no
  prompt text describes files on disk, that every documented output key is one
  the tool sets, that the clamps and list bounds quoted to the agent match the
  code, and that `SKILL.md` passes no parameter the tools do not accept. All
  three defects above were found by writing them.

- **Concurrency tests for the catalogue**, asserting content equality against a
  single-threaded reference rather than merely "did not raise" — the measured
  failure raised nothing 3.5 % of the time. Plus a structural test, over the AST,
  that fails if any statement is executed outside `_query()`: `_connect().execute(…)`
  is the natural thing to write and a timing test might not catch it.

  Both nets were mutation-tested. Removing the lock fails three tests; one
  bypassing statement fails two; five separate sabotages of the prompt surface
  each fail the test that targets them. One of those five initially survived —
  the envelope-key check scanned the raw source, docstring included, so the
  documentation satisfied an assertion about the implementation. It now strips
  the docstring via the AST.

- **Seven guards that a green suite could not tell were broken.** A targeted
  mutation pass over the client and the `query` tool found seven places where a
  plausible edit passed all 539 tests. None was a defect — the code is correct
  at every one of them — but nothing would have noticed them becoming one, and
  five sit on the path where the caller receives fewer objects than match and
  the envelope does not say so.

  The blocker was `StubBackend`: it hardcoded `complete=True` on both return
  paths, so the branch of `query` that tells an agent *"I fetched 200 of 99,999
  objects before hitting the safety cap"* was unreachable from any integration
  test. Deleting that note and its warning outright left the suite green. The
  stub now takes `cap_at` and can express the state.

  Also pinned: `truncated` computed on the clamped limit rather than the
  requested one (raw `limit=500, page=2` over 1,000 matches reports
  `truncated=false` and `next_page=null`, stopping a documented paging loop 400
  objects short); a 403 on the *data* path re-authenticating and retrying, which
  only `/aaaLogin` had ever exercised; `_MAX_PAGES` and `_MAX_OBJECTS` holding
  their values, since every other cap test imports the constants and so measures
  only that the loop obeys them — 25 → 250 and 5000 → 50000 both passed; and a
  non-numeric `totalCount` falling back rather than reporting zero, which is
  half of what that function's docstring already promised.

  Each of the seven was broken deliberately and fails the test written for it.
  No production code changed.

- **The five tools declare themselves read-only.** `search_classes` and
  `get_schema` never leave the process; `query`, `get_by_dn` and `count` issue
  GETs against the APIC and nothing else. None of that was stated anywhere a
  client could read it, so every client had to assume the worst and prompt the
  user on each call — and an agent answering one question makes a dozen. Each
  tool now carries `readOnlyHint`, an `openWorldHint` that separates the two
  local tools from the three that reach the fabric, and a human-readable title.
  `destructiveHint` and `idempotentHint` are deliberately absent: the MCP
  specification defines them as meaningful only when `readOnlyHint` is false.

- **A contribution path for APIC releases.** `SUPPORTED-APIC.md` states which
  release ships, what happens when yours differs, and takes a one-line request.
  A pull request here cannot add support on its own — the catalogue lives in the
  `niwaki` dependency — so what it does is register demand, and the file says so
  rather than implying otherwise.

- **Issue and pull request templates.** The bug report asks for the
  `Registry loaded` startup line and the reporter's APIC release, because a
  version gap and a genuine defect present identically: an empty result. It also
  opens by saying an empty result is usually not a bug, which is the truth about
  how the APIC answers an unknown class.

### Fixed

- **Concurrent catalogue reads could return another class's schema, silently.**
  `_connect()` hands out one `sqlite3.Connection` with `check_same_thread=False`,
  and its docstring claimed that was "safe under SQLite's default serialised
  threading mode". SQLite serialises its own internals; `sqlite3.Connection`
  keeps a per-connection prepared-statement cache that does not. Measured on this
  catalogue, `load_schema` under a thread pool, three repetitions per cell:

  | threads | calls | exceptions | silently wrong |
  |---:|---:|---:|---:|
  | 1 | 600 | 0 | 0 |
  | 4 | 2,400 | 21 (0.9 %) | 29 |
  | 16 | 9,600 | 192 (2.0 %) | 333 (3.5 %) |

  "Silently wrong" means a schema whose content differs from the single-threaded
  reference **with no exception raised** — the caller receives another class's
  schema and cannot tell. Every read now goes through one `_query()` behind a
  lock. Isolated to the statement cache, not to SQLite: cache disabled 0/0,
  thread-local connection 0/0, lock 0/0. The lock is also the fastest of the
  three (0.040 s against 0.089 s and 0.150 s at 2,400 calls) and the only one
  that keeps a single copy of the string pools, which is what the cached
  connection exists for. Latent today — nothing in `src/` spawns threads — and
  one `asyncio.to_thread` or one multi-worker deployment away from not being.

- **The server described a data layer it stopped having in 2.0.** `get_schema`
  told every agent it reads "the APIC jsonmeta schema file" and returns `{}` when
  "the class file is not found in the local schema collection"; `query` and
  `count` documented a two-tier validation with a fallback to schema files that
  2.0 replaced with a single source of truth; `SKILL.md` said `get_schema`
  "returns the APIC jsonmeta schema". None of it has been true since the object
  model became a SQLite catalogue inside the `niwaki` dependency. Documentation
  that lies to a human wastes an afternoon; documentation that lies to an agent
  becomes its model of the system.

- **`property_details` documented a `mandatory` key that cannot appear.**
  Measured: **0 of 332,297 properties** in the shipped catalogue set the flag bit.
  Removed from the `get_schema` docstring and from `SKILL.md`. The projection
  stays in `catalog.py` — a future catalogue that does set the bit will fail the
  new test rather than emitting an undocumented field.

- **The documented client setup did not work, for either client.** Claude
  Desktop rejects a `"type": "http"` entry — it reports *"not valid MCP server
  configurations and were skipped"* once at startup and then runs with the
  server silently absent. It needs the `mcp-remote` stdio bridge, which is now
  what the README shows, with the reason and the trap that the header is one
  argument. The OpenCode block was wrong in four ways at once: the file name,
  an extra `servers` nesting level, `"type": "http"` instead of `"remote"`, and
  a missing `enabled`. Both are now the configurations that were verified
  connected.

- **`README.md` never disclosed the version limitation.** It named APIC 6.0(9c)
  and stopped there. It now says what happens on a different release, and that
  the failure is quiet.

- **`CONTRIBUTING.md` gave two instructions that were wrong, one of them
  harmful.** It told every contributor to run `ruff format`, which would
  reformat 30 of the 59 files in `mcp/` and bury their change in unrelated diff —
  this codebase is not formatter-managed and CI only ever runs `ruff check`. And
  it pointed at `pytest tests/unit/`, which is 434 of the 539 tests, so nobody
  following the guide ran the integration tests, the search-quality gate or the
  recorded baseline before opening a pull request. Both corrected, and the local
  convention that a new guard is broken on purpose before it is trusted is now
  written down rather than folklore.

---

## [2.1.0] - 2026-08-08

**Two authentication defects fixed, and the Docker deployment path removed.**
Both defects are reachable in 2.0.0 as published: a `SIGHUP` could strip
authentication from a routable bind, and the rate limiter's table grew without
bound under unauthenticated traffic. The Docker path is removed rather than
repaired — it was shipped broken, no image was ever published, and running the
server directly is unaffected.

The five tools keep their signatures. The Python interface does not change.

### Removed

- **The Docker deployment path is gone** — `mcp/deploy/` (Dockerfile,
  `docker-compose.yml`, `Caddyfile`), `docs/getting-started/docker.md`,
  `docs/getting-started/https.md`, the Docker build jobs in both CI pipelines,
  the `MCP_DOMAIN` variable, and every reference in the README and `docs/`.

  It was shipped broken. Since 2.0 the server binds `127.0.0.1` by default and
  nothing in the image, the compose file or `.env.example` overrode it, so the
  documented `docker run -p 8000:8000` published a port nothing listened on.
  The compose stack was worse: its healthcheck probed loopback from *inside*
  the container, so it passed, the container reported healthy, and Caddy then
  failed with connection refused. No image was ever published, so nothing
  downstream depends on this.

  Running the server directly — `uvx niwashi-mcp`, or the console script from a
  virtualenv — is unaffected and is now the only documented path.

- **Dead code the 2.0 migration left behind.** None of it was reachable; all of
  it pointed at a world that no longer exists.

  - `registry.descriptions.load_descriptions()` read `class-descriptions.json`,
    the file 2.0 deleted. Nothing in the server called it — a test asserts that
    — and its error message told the reader to run `aci-collect`, a tool that
    is not in this repository. Gone, with its four tests and its reference page.
  - `scripts/list-configurable-classes.sh` read `data/schemas/`, removed in 2.0
    and `.gitignore`d besides, so on a fresh clone it could only ever print
    `run schema-collector first`. It was the only file in `scripts/`; the
    directory goes with it.
  - `tests/perf/conftest.py` still generated synthetic jsonmeta files into a
    versioned directory, for a `resolve_schemas_dir()` and a
    `tests/perf/test_schema_perf.py` that 2.0 deleted. No test consumed the
    fixture.
  - `tests/integration/test_tools.py` carried an unused `_EXTRA_ONLY_SCHEMA`
    fixture and a comment describing the two-tier validation fallback that 2.0
    replaced with a single source of truth. The surviving test is rewritten to
    assert what actually happens now, including that the backend is never
    reached — the APIC does not error on an unknown class, it returns an empty
    result, which is indistinguishable from "no such objects".

### Added

- **`MCP_HOST` and `MCP_ALLOW_NO_AUTH` are documented.** Both were read by the
  server since 2.0 and appeared nowhere but a docstring — while
  `docs/architecture/overview.md` positively asserted a list of read variables
  that omitted them. They are now in `.env.example`, the settings reference
  (with the startup guard and its exact message), the README configuration
  table, and the variable map. The absence of `MCP_HOST` from `.env.example` is
  what made the container defect above invisible.

- **The catalogue's latency decisions are measured again.** Until 2.0 the schema
  path was `registry/schema.py` and `tests/perf/` measured it; the migration
  deleted that reader, replaced it with SQLite — changing the cost profile
  completely — and nothing replaced the measurement. `tests/perf/test_catalog_perf.py`
  closes that gap, and pins three decisions that are each one deleted decorator
  away from silently reverting: the single cached SQLite connection (a second
  one loads a second copy of 26,654 labels and 25,411 comments), the pooled
  string caches, and the fact that `property_details` and `properties_filter`
  are genuinely the cheaper paths — which is what makes `get_schema`'s advice
  true rather than merely well-intentioned. Asserted structurally or by ratio,
  never by a wall-clock constant calibrated on one machine. Mutation-tested:
  five sabotages, each failing the test that targets it.

### Fixed

- **Authentication could be removed by a SIGHUP, on a bind that refuses to start
  without it.** An empty key set makes `ApiKeyMiddleware` a no-op — intended on
  loopback, and refused outright at startup on a routable bind. But that refusal
  runs once. `SIGHUP` re-reads the file afterwards, and anything that yields an
  empty set there applied it: a truncated `.env`, a file caught mid-rotation, an
  unmounted secret volume, a mistyped key in `kubectl create secret`. The server
  kept serving, kept reporting healthy, and every tool — with the APIC
  credentials behind them — became reachable without a header. The code already
  logged `auth disabled`; logging is not refusing.

  `KeyStore` now carries `auth_required`, set from the same
  loopback/`MCP_ALLOW_NO_AUTH` fact the startup guard uses, and `reload()`
  refuses an empty set rather than applying it — keeping the previous keys and
  returning `False` so the handler reports it at `error`. The refusal lives in
  `KeyStore` rather than in the signal handler so it holds for every call site,
  and is testable without an HTTP layer. Rotation to a non-empty set is
  unaffected, and loopback dev mode is unchanged.

- **The rate limiter's tracking table had no bound.** `RateLimiter._counts` was
  a `defaultdict(list)` keyed by peer address that pruned timestamps only for
  the address being looked at — an address seen once kept its key forever. The
  table is written by requests carrying no valid credential, so its size was
  chosen by an unauthenticated caller: measured at 206 bytes per address,
  200,000 distinct sources retain 39.3 MiB, bounded only by the address space.
  Under a container memory limit that ends as a SIGKILL, and since CPython does
  not read cgroup limits it arrives as exit 137 with no traceback and no log
  line.

  Two mechanisms now bound it: a sweep of the whole table once per window, so
  the retained set is "addresses that failed within the last window" rather than
  "addresses that have ever failed", and a hard ceiling of 4,096 entries
  (~4.5 MiB worst case) for a burst inside a single window. A full table refuses
  a new address rather than evicting the oldest — eviction would hand an
  attacker who sprays addresses a fresh budget on every one of them, which is
  the property the limiter exists to deny.

  Also documented, not changed: "per-IP" means per *peer* address, which is only
  per-client when the server is reached directly. Behind a reverse proxy the
  peer is the proxy for every request, so the window becomes global. That is
  stricter than advertised, not laxer, and reading `X-Forwarded-For` unvalidated
  would be worse than the imprecision.

  Thirteen tests cover both defects, and were mutation-tested before being
  committed: five sabotages — the wiring in `_serve`, the refusal in `reload()`,
  the handler's check of its return value, the sweep, and the ceiling — each
  fail the tests that target them.

- **`DescriptionsLoadError` described the wrong failure.** Its docstring still
  said "class-descriptions.json is missing or contains invalid JSON. Regenerate
  it with: `aci-collect run --from descriptions`", while in 2.0 it is raised by
  `registry.catalog` for a broken niwaki catalogue — and `get_schema`'s own
  docstring told the caller to reinstall niwaki. Two contradictory instructions
  for one error; the exception now describes what it actually means.

### Changed

- The Cisco DevNet sandbox password no longer appears in `README.md` or
  `.env.example`. Both now point at the sandbox page, which is where Cisco
  publishes the credentials and rotates them; a copy here goes stale and teaches
  the wrong habit. The value remains in the git history, where it is what it
  always was — a credential Cisco publishes for a shared always-on lab.

---

## [2.0.0] - 2026-08-08

**The data layer is now niwaki's embedded catalogue.** The server reads the ACI
object model from one SQLite database shipping inside the `niwaki` dependency
instead of 15,452 raw jsonmeta files, which is what makes it installable with
`uvx niwashi-mcp` rather than requiring a git checkout and a 98.8 MB schema
bundle. The five tools keep their signatures; parity with the 1.x projection was
proven class by class against a frozen jsonmeta oracle before the old path was
deleted.

The package is renamed **`niwashi-mcp`** — 庭師, the gardener who tends the
niwaki. The 1.x name carried a protected mark it had no licence to use.

### Changed — the retired name

- **Every public identifier drops the retired name.** `AciMcpError` becomes
  `NiwashiMcpError` (and with it the base of the whole exception hierarchy),
  `ACI_MCP_ENV_FILE` becomes `NIWASHI_MCP_ENV_FILE`, the FastMCP server
  announces itself as `niwashi-mcp` in the MCP handshake, and the logger tree
  moves from `aci-mcp` / `aci-mcp.apic` / `aci-mcp.auth` to `niwashi-mcp.*`.

  The reason is narrower and harder than trademark caution: **`aci-mcp` is
  already taken on PyPI** by an unrelated project (Aipolabs, `1.0.0b13`). The
  name was never available. A partial rename would have left this server
  announcing a name that resolves to somebody else's package.

  `NIWASHI_MCP_ENV_FILE` costs nobody anything — it was introduced inside the
  2.0 cycle and has never appeared in a tagged release. The other three shipped
  in 1.2.2, which was installable only from a git checkout; renaming them now
  rather than later is the difference between one breaking change and two.

  Retired names are **not** accepted as aliases. `ACI_MCP_ENV_FILE` and
  `ACI_MCP_DATA_DIR` are both ignored, and a test asserts it — an alias that
  silently steered path resolution is the one failure mode nobody notices. The
  2.0 removal of `ACI_MCP_DATA_DIR` had until now been recorded only in a
  source comment.

### Added

- **The niwaki catalogue adapter (`registry/catalog.py`), not yet wired in.**
  It reads the object model from the SQLite catalogue shipped inside the
  `niwaki` dependency and is the only module in the codebase that knows niwaki
  exists — which is what keeps the migration reviewable and the rollback cheap.
  The server still runs entirely on the jsonmeta path, so this can be proven or
  disproven at no cost.

  Measured against the live corpus: **1,500 classes compared, zero unexpected
  divergence**; the only difference is the `options` list on `mo:*` register
  properties, which niwaki drops deliberately (one `mo:MoClassId` carries
  17,653 entries into an agent's context). The rebuilt search index is
  **byte-identical** to `class-descriptions.json` — 15,239 entries, including
  the 213-class gap, which turns out to be a property of the collector's filter
  rather than an accident.

  Twenty-five tests cover it, and were mutation-tested before being committed:
  six sabotages — a leaked readable name, an unfiltered `defaultValue`, a
  truncated `dnFormats`, `prop_labels` as a string, a case-insensitive lookup,
  an omitted empty key — each fail the test that targets them.
- **A behavioural baseline, recorded before any 2.0 change was made.** The five
  tools keep their signatures across this migration, so a defect in the swap
  would be silent — a changed field shape, a drifted ranking, a truncated list —
  and every pre-existing test would still pass. `mcp/tests/baseline/` now
  records what the implementation does and asserts *equality* against it: the
  whole descriptions index by digest, `get_schema()` for 38 stratified classes,
  and the exact top-5 of all 74 golden queries. Two gaps made this necessary:
  the search floors sat at 60 % / 85 % while the implementation delivers
  78.4 % / 94.6 %, so a regression to 61 % was a green build; and nothing
  anywhere pinned the output of `get_schema()`. The net was mutation-tested
  before being committed — five separate sabotages each fail exactly one test.
- **Explicit, overridable path resolution** via `NIWASHI_MCP_ENV_FILE`, with
  tests pinning both the override and the refusal to honour the retired
  `ACI_MCP_*` spellings. (`ACI_MCP_DATA_DIR` was introduced and removed inside
  this same cycle — see Removed — and never reached a release.)

### Changed

- **BREAKING — `get_schema` and class validation now read the catalogue.** The
  jsonmeta directory is no longer resolved, opened or consulted at runtime, and
  `schemas_dir` is gone from the server's lifespan context.

  Measured against the recorded pre-2.0 baseline, on 38 stratified classes:
  **zero drift on the plain schema**, and a single divergence on
  `property_details` — `actionAeSubj`, whose `mo:MoClassId` register loses its
  17,653-entry `options` list. That class is in the sample deliberately, and the
  exception is asserted to be exactly itself: the test fails both if the drift
  disappears (a stale allowlist entry) and if it spreads to any other field.

- **BREAKING — class validation has one source of truth, and the class universe
  opens from 15,239 to 15,452.** `query()` and `count()` used to validate in two
  tiers — the descriptions index, then a fallback to the schema files — because
  the two collections disagreed by 213 classes and a class absent from the first
  could still be perfectly queryable. Both now come from the same catalogue, so
  the fallback is gone along with the warning it emitted on 213 valid classes.
  Those 213 remain validatable but not searchable, which is a property of the
  index filter rather than an accident.

  Case sensitivity is now structural: SQLite's BINARY collation makes `fvBd`
  resolving to `fvBD` impossible, where the previous reader needed an explicit
  guard against case-insensitive filesystems doing exactly that.

- **`search_classes` now runs on the catalogue-rebuilt index.** The server no
  longer reads `data/class-descriptions.json` at startup; it rebuilds the index
  from niwaki's catalogue instead. Because that index is byte-identical, search
  quality is unchanged **exactly** — Recall@1 78.4 %, Recall@5 94.6 %,
  MRR 0.846, and every one of the 74 golden queries returns the same top-5 in
  the same order. Those are asserted as equalities, not floors: any movement is
  a rebuild bug, not a scoring trade-off. The scorer itself — `_score`, the
  curated synonym table, the structural priors — is untouched.

  Startup now logs the APIC release the catalogue was built from. From 2.0 that
  version is pinned by a dependency rather than chosen by the operator, so a
  silent niwaki upgrade would otherwise change the object model an agent
  reasons about with no trace.

  Index construction costs ~440 ms once at startup, against ~34 ms to parse the
  JSON file it replaces, inside a lifespan that already performs an APIC
  authentication round trip. Steady-state search latency is unchanged (~14 ms).

- **BREAKING — the distribution is now `niwashi-mcp` and the import package is
  `niwashi_mcp`.** The code moved from a flat `mcp/` layout to
  `mcp/src/niwashi_mcp/`, which is what makes the server installable — and so
  what makes `uvx` possible, the whole point of 2.0. Publishing the flat layout
  would have claimed `exceptions`, `main`, `registry`, `middleware` and `apic`
  as top-level PyPI modules; a test now proves none of them is importable from
  an installed environment. `python main.py` still works through a deprecation
  shim scheduled for removal in 3.0.

  On the name: `cisco-aci-mcp` was the earlier plan and is dropped — Cisco's
  published trademark policy forbids using its marks "as or as part of a
  product name". `aci-mcp-server` is no better: Cisco does not own "ACI" alone,
  but ACI Worldwide does, live in classes 9 and 42, and enforces it. `niwashi`
  (庭師, the gardener) carries no third-party mark in any register searched and
  matches the existing `niwaki` family. Compatibility with Cisco ACI is stated
  in the summary, keywords and README — the construction trademark owners
  themselves publish as acceptable.

### Fixed

- **A `mailto:` in `[project.urls]` made the package unpublishable.**
  `"Commercial License" = "mailto:monark.aiops@pm.me"` is rejected by the index
  with `400 ... is not a valid url` — `Project-URL` values must be real URLs.
  Neither `twine check` nor `uv build` validates this, so it passed every local
  gate and only surfaced on a real upload. It now points at
  `LICENSE-COMMERCIAL.md` in the repository, which carries the same contact.
  Caught by the TestPyPI rehearsal, which is the whole reason for having one:
  on PyPI it would have burned the 2.0.0 version number permanently.

- **The latency tests had never run on CI hardware, and one measured the wrong
  thing.** `ci.yml` excludes `tests/perf` and only the release pipeline runs the
  full suite, so thresholds calibrated "on a modern laptop" met a shared 2-core
  runner for the first time at the release candidate: a 200 ms budget measured
  0.426 s. The budget was not the real defect. `test_single_search_15k_classes`
  timed the *first* call — the one that builds the tokenised index — and called
  it search latency; production builds that index once in the lifespan and never
  pays it per query. The tests now measure the warm path, and the regression
  actually worth catching — losing the single-slot index cache, which costs ~25x
  — is pinned by a cold/warm ratio rather than a wall-clock constant, so it holds
  on any machine. Verified by sabotage: disabling the cache fails both the ratio
  assertion and the throughput ceiling, and takes the file from 3.0 s to 29.6 s.

- **`query()` could never report the end of a result set.** `truncated`
  compared the total to the size of the page in hand rather than to the offset
  consumed, so it was permanently true: page 2 of 45 objects returned 5 and
  still claimed more remained, and so did page 99 returning nothing. Since
  `SKILL.md` and `docs/tools/query.md` both instruct an agent to *page until
  `truncated` is false*, an agent following the documented procedure looped
  until it exhausted its turn budget — one APIC call per iteration — and
  returned nothing. No test had ever called `query()` past page 0; six now do,
  including one that walks the documented loop to exhaustion.

- **`get_schema()` could return 7.8 MB in a single call.** `dnFormats` and
  `containedBy` are unbounded in the object model — a class that attaches to
  almost any managed object enumerates one entry per possible parent — and
  seven classes are extreme: `faultDelegate` carries 64,313 DN templates,
  `faultCounts` 31,271, `faultInst` 24,151. Serialised that is 2.6 MB to 7.8 MB
  of JSON, roughly 800 k to 2 M tokens, for one tool result. They are not
  obscure classes and the tool's own documented workflow walks into them:
  `search_classes("fault")` ranks `faultCounts` first, and the next prescribed
  step is `get_schema` on it. Both lists are now sampled to `list_limit`
  entries (25 by default, clamped to `1..500`) with a `dnFormatsTruncated` /
  `containedByTruncated` marker carrying `{returned, total, note}`, so the cut
  is disclosed rather than silent. `faultDelegate` drops from 7.8 MB to 3.5 KB
  and the other 15,445 classes come back byte-identical, with no marker. No
  information is lost that an agent can act on: the 64,313 templates differ
  only in their parent prefix and all end in the same relative name, which
  `rnFormat` already carries in full. The bound sits at the tool surface, not
  in `registry.catalog`, so the data layer stays the faithful projection the
  baseline parity tests verify against the 1.x jsonmeta oracle. Forty-one tests
  cover it — including one that walks all 15,452 classes to prove none escape —
  and five sabotages were each caught before it was committed.

- **`rsp_subtree_include` returned no children.** Asking for children set the
  APIC `rsp-subtree-include` parameter but never `rsp-subtree`, which the APIC
  requires alongside it, so the fabric answered without the child objects; the
  extraction step then gated on a flag the caller had not set and dropped
  whatever did come back. The request now defaults `rsp-subtree=children` when
  an include is asked for, and extraction keys off the response actually
  containing children rather than off the request that asked for them.

- **The server bound every interface, without authentication, while the README
  said localhost.** `0.0.0.0` was hardcoded with no way to change it. The
  documented quickstart therefore put an unauthenticated server holding APIC
  credentials — usually admin-capable — on every interface of the machine, with
  a log line as the only guard. It now binds `127.0.0.1` by default, `MCP_HOST`
  selects the interface, and a routable bind with `MCP_API_KEYS` unset is
  **refused** rather than warned about. `MCP_ALLOW_NO_AUTH=true` accepts the
  risk explicitly and logs that it was deliberate. The production path
  (`docker-compose`, which uses `expose:`) was never affected — only the
  documented one.

- **`APIC_VERIFY_SSL=false` passed in silence.** The first thing the server
  does is POST the APIC username and password to `/api/aaaLogin.json`; without
  verification it does so to whatever answers, so an ARP or DNS spoof on the
  management network collects the credential in clear. The default stays false
  — an APIC ships self-signed, and demanding verification would make the server
  unusable on most fabrics — but startup now names the risk instead of leaving
  it to be inferred from four documentation lines that called it a lab
  convenience.

- **The niwaki dependency was pinned as if the coupling were its public API.**
  `registry/catalog.py` reads the catalogue's SQLite schema directly — `mo`,
  `prop`, `comment_pool`, `label_pool`, `type_pool`, `enum`, `manifest` — none
  of which appear in niwaki's public surface (`Niwaki`, `AsyncNiwaki`,
  `models`). A 1.9 could therefore restructure any of it and remain perfectly
  within SemVer, while `niwaki>=1.8,<2.0` would happily install it. Some of
  those changes fail loudly; the ones worth guarding are the quiet ones — a
  repurposed column, a changed blob encoding — where every query still runs and
  the server answers questions about a production fabric from silently empty
  fields, reporting that a bridge domain has no parent. The pin is now
  `>=1.8,<1.9`, and because a pin is only advice (a resolver override or a
  `--force-reinstall` gets past it), `catalog.verify_catalogue()` runs before
  anything reads the catalogue and refuses startup on a mismatch, naming what
  moved and which niwaki produced it. It checks structure — every table and
  column the queries name, the manifest keys, the `prop.flags` bit layout — and
  then decodes one known class end to end, which is what catches an encoding
  change that leaves the structure intact. Separately, a corrupt blob now
  reports as a broken catalogue naming the column rather than letting a bare
  `zlib.error` escape. Twenty-three tests cover it; five sabotages were run,
  two survived the first pass and the tests were strengthened until none did.

- **A DN went into the APIC request URL unchecked.** `get_by_dn(dn)` and the
  `scope_dn` argument of `query()`/`count()` were interpolated straight into
  `/api/mo/{dn}.json`. A DN carrying `..` segments, a `?`, a `#`, a backslash,
  a newline or a NUL could therefore walk out of `/api/mo/` and reach another
  APIC endpoint, or split the request — with the server's own APIC session,
  which is the point: the caller borrows an authenticated session they do not
  hold. DNs are now validated before interpolation and a malformed one is
  rejected with `FilterError` naming the offending field, rather than being
  sent to the fabric.

- **Eleven tests were silently skipping — the search guarantees were not being
  checked at all.** Deleting `data/class-descriptions.json` in the same release
  that removed the data plane left every test that compared against it in a
  `pytest.skip` branch: index equality, the recall and MRR assertions, the
  per-query top-5, and the quality floors. They reported green by not running.
  All of them now compare against the pre-2.0 recording in
  `tests/baseline/baseline.json` instead of a deleted file. Verified: 0 skipped,
  and a deliberate index regression fails 9 of them.
- **The source distribution was malformed and could not build a wheel.**
  `readme = "../README.md"` produced an archive entry named
  `niwashi_mcp-2.0.0/../README.md`, a path escaping the archive root: `tar`
  refuses to extract it and several tools reject such archives outright. The
  package now carries its own `mcp/README.md`, and `uv build` rebuilds the
  wheel *from the sdist*, which is what proves the sdist is complete.
- **Path resolution no longer breaks when the package is installed.** The data
  and `.env` locations were derived from `__file__`, which only worked from a
  git checkout; installed, that arithmetic walked out of `site-packages` onto a
  meaningless directory. Because a missing `.env` is not an error and a missing
  schema directory merely yields empty results, the failure was silent. A
  checkout is now *verified* by its layout rather than assumed, and an
  invariant test asserts no resolved path may ever point inside
  `site-packages`.
- `get_schema`'s documented exception is retargeted. `SchemaLoadError` meant
  "a jsonmeta file exists but is malformed", a condition that can no longer
  arise. The failure that replaces it — the niwaki catalogue missing or
  unreadable — is a real one and needed naming, so it is reported as
  `DescriptionsLoadError` with a reinstall hint rather than leaving a
  documented exception that nothing can raise.
- The container looked for the data bundle in `/app/data/` while the image
  copies it to `/data/`, which would have broken startup. The image now also
  installs the package rather than copying modules one by one, so it runs
  exactly what a wheel produces.

### Removed

- **BREAKING — the data plane is gone.** `data/schemas/` (1.7 GB),
  `data/class-descriptions.json` (11 MB, previously tracked in git),
  `scripts/download-schemas.sh` and the jsonmeta reader
  (`registry/schema.py`, 361 lines) are all deleted. Installing no longer means
  cloning a repository and downloading a 98.8 MB bundle: **`uvx niwashi-mcp`
  starts on a machine with no checkout**, which is the entire point of 2.0.

  The container drops from **3.97 GB to 457 MB** — 8.7x smaller — because it no
  longer carries the schema corpus. Deployments that mounted `/data` lose that
  mount point; there is nothing left to mount.

  Parity remains verifiable, deliberately and by two independent means.
  `tests/fixtures/jsonmeta/` freezes 31 raw APIC 6.0(9c) class files (2.4 MB)
  and `tests/fixtures/jsonmeta_oracle.py` keeps the 1.x projection as a **test
  oracle**, so the expected output is *derived* from the vendor's own files
  rather than read back from a snapshot this project recorded of itself — a
  snapshot cannot catch an error made identically in both the recording and the
  implementation. For the full corpus, the `reference/pre-2.0-jsonmeta` branch
  and the v1.0.0 release asset together reconstruct the complete comparison;
  the procedure is recorded and was tested end to end.

- `ACI_MCP_DATA_DIR` no longer exists — there is no data directory to point at.
  The `.env` override survives under its new name, `NIWASHI_MCP_ENV_FILE`.
  Neither retired spelling is accepted as an alias.
- 43 tests of the deleted jsonmeta reader, and its performance suite, are
  removed rather than adapted: a module that no longer exists cannot be tested.
  The 31-class oracle covers what they covered, and covers it by independent
  derivation rather than by reading the implementation back to itself.

### Fixed (1.x)

- The README described the project as "the first open-source MCP server for
  Cisco ACI". It is neither: at least eight other ACI/APIC MCP servers predate
  it, the earliest by a year and one of them published by CiscoDevNet, and
  PolyForm Noncommercial is source-available rather than OSI-approved. The
  tagline now states what actually distinguishes the project — it reads the
  fabric's own object model rather than a fixed list of endpoints — which is a
  description of the architecture rather than a claim of primacy, and so cannot
  be falsified by a project neither of us has seen. The GitHub repository
  description was also still advertising three tools; there are five.

## [1.2.2] - 2026-08-06

Documentation-only release. No code path changes, no API change — but the
guidance an LLM client actually reads was wrong in ways that produce
confident, incorrect answers about relations, so the effect on behaviour is
not cosmetic.

### Fixed

- **`SKILL.md` §8 taught a traversal convention that covers a fifth of the
  model.** It presented `tn{TargetClass}Name` as *the* way to read a
  relation's target. Measured against the schema collection: that attribute
  exists on **310 of the 1499 concrete relation-source classes (20%)**. The
  other 1189 are `explicit` and carry no `tn*Name` at all, so an agent following
  the documented pattern reads a nonexistent attribute, gets nothing, and
  improvises. The canonical field is `tDn`; §8 now leads with it and treats
  `tn*Name` as the narrower case it is, including the classes that carry two
  of them.

- **Nothing in the guidance mentioned `state`.** An Rs object records the
  target that was *configured*, and that record survives the target being
  deleted — so a populated name or DN is not evidence the target exists.
  Only `state` carries the APIC's verdict. §8 now opens with that rule and
  documents both enumerations (`state`, `stateQual`), grounded in a case
  observed on a live fabric: a `spanRsSrcGrpToFilterGrp` in
  `state=missing-target` whose `tDn` nonetheless resolves to a live
  `spanFilterGrp`. An agent "verifying" by fetching that DN concludes the
  relation is healthy while the APIC says it never formed.

- **§12's worked example was itself the bug.** It read `tnFvCtxName` and
  concluded "BD `servers` uses VRF `ot.main.vrf`" without ever looking at
  `state`. Rewritten to read `state` before concluding, and paired with the
  same example in its broken form to show that only `state` distinguishes
  the two responses.

- **`stateQual: default-target` was undocumented.** Sweeping all 48 tenants
  of the test fabric, **2220 of 4753 relations (47%) resolve to an inherited
  default policy** rather than to anything configured on the object.
  Reporting those as design decisions is wrong for nearly half of all
  relations.

- **`unformed` is now documented as ambiguous rather than broken.** Across
  the same 4753 relations only 24 were not `formed`, and 22 of those were
  `unformed` — most with targets that resolve perfectly well
  (`vzRsRFltPOwner`, `mgmtRsInBStNode`). It is the property's default and
  the resting state of many internal relations. Only `missing-target`,
  `invalid-target` and `cardinality-violation` are definite failures. An
  earlier draft of this guidance would have flagged all 22 as faults.

- **Two silent-failure traps are now documented**, both measured:
  `filter_expr` predicates against relation properties (`state`,
  `stateQual`, `tDn`) return **HTTP 200 with zero results in both
  directions**, so neither `eq` nor `ne` can be believed; and a fabric-wide
  subtree sweep of relation classes returns a small fraction of the real
  population with no error and no `truncated` signal.

### Added

- `SKILL.md` §4 now documents the **Show Usage** equivalent: the APIC
  materialises an Rt object under the *target*, one per referring source,
  each carrying that source's DN in `tDn` — the way to answer "what would
  break if I deleted this?". It also records that Rt objects carry **no**
  `state`, so relation health exists only on the outgoing side and the two
  directions are not symmetric.

- A `RELATION INTEGRITY` section in the FastMCP server `instructions`, so
  the rule reaches clients that never load the skill file, plus the two
  enumerations in §10's common-values table and four new rows in the
  error-handling table.

## [1.2.1] - 2026-08-06

### Added

- `.github/dependabot.yml` schedules weekly version-update checks for the
  `mcp/` uv-managed dependencies and for GitHub Actions dependencies, on top
  of the security-only Dependabot alerts already active — so a vulnerable
  or outdated dependency surfaces on a schedule instead of only at the next
  manual audit.

### Fixed

- `count()` reported a tally that did not match the number of objects it was
  counting. The tool used the APIC `rsp-subtree-include=count` mechanism and
  returned the `moCount` it carries; measured against reality on APIC
  6.0(9c), that value disagrees with the true size of the result set — and
  does so silently, since the request itself succeeds:

  | call | reported | actual |
  | --- | --- | --- |
  | `count("fvBD")` | 203 | 403 |
  | `count("fvTenant")` | 36 | 48 |
  | `count("fvBD", filters={"arpFlood": "no"})` | 99 | 203 |
  | `count("fvBD", scope_dn=<tenant A>)` | **0** | 192 |
  | `count("fvBD", scope_dn=<tenant B>)` | 128 | 128 |

  The failure is data-dependent rather than systematic: sweeping every tenant
  on the test fabric, 5 of the 28 holding bridge domains reported a scoped
  count of `0` while the subtree really held between 1 and 192 of them; the
  other 23 were exact. It is deterministic — repeated calls return the same
  wrong value for the same scope — so it cannot be mistaken for transient
  noise or absorbed by the client's retry budget.

  A `0` is the most damaging shape this can take. It reads as a legitimate
  finding ("this tenant has no bridge domains") rather than as a failed
  lookup, which is precisely the error-as-answer failure mode the tool layer
  guards against elsewhere.

  `count()` now issues the same class or subtree request as `query()` with a
  page size of 1 and reads the APIC-reported `totalCount`, which was exact in
  every case measured. That is the same field `query()` already reports as
  `total_available`, so the two tools can no longer disagree about the size
  of the same result set. One object is transferred instead of none; every
  other match stays on the APIC, so counting remains far cheaper than
  fetching a result set to measure it.

  Measured on an APIC 6.0(9c) simulator; the `moCount` behaviour has not been
  re-confirmed against hardware. The fix does not depend on that: `totalCount`
  is exact on both, and is the mechanism the client already relies on
  everywhere else.

- The live test suite (`tests/live/`, excluded from the default run) had gone
  stale against the `QueryResult` envelope introduced in 1.2.0 — it still
  indexed `query_class()`'s return value as a list, so every test touching it
  would have errored. Repaired, and now passing against a real fabric.

- The live `count_class()` test asserted only that the tally was a
  non-negative `int`, a condition the broken count satisfied trivially by
  returning `0`. Replaced with three tests that pin the real invariant —
  `count()` must agree with `query()`'s `total_available`, fabric-wide,
  scoped to the busiest tenant, and filtered.

- Documentation across `README.md` and `docs/` had drifted from the
  current implementation (mostly predating the `get_by_dn`/`count` tools,
  the `query()` envelope return, and the v2 search algorithm). Corrected,
  across architecture, internals, and tool-reference docs:
  - `query()`'s documented return shape — an envelope dict
    (`results`/`returned`/`total_available`/`truncated`/`next_page`/
    `complete`/`note`), not a bare list — including every stale code
    example built against the old shape.
  - `search_classes`'s scoring algorithm description, which still
    described the retired v1 substring scheme instead of the current
    tokenized/structural-priors v2 algorithm — including the live tool
    docstring in `main.py` itself, which is what an MCP client actually
    sees as the tool's capability description.
  - `get_schema()`'s schema-directory resolution: documented as a
    per-call glob fallback across versioned subdirectories, when
    resolution actually happens once at server startup.
  - `get_by_dn`/`count` missing from several tool-count diagrams and
    tables that still showed only 3 of the 5 tools.
  - Two invented ACI class names (`fvRsConsumedBrCP`/`fvRsProvidedBrCP`)
    in the object-model doc, replaced with the real ones (`fvRsCons`/`fvRsProv`).
  - The `ApicRequestError` exception and the retry/backoff behavior
    (exponential backoff, transient-status retries), missing from the
    documented exception hierarchy and client internals.
  - A security-relevant docstring inaccuracy in the API-key comparison
    (`middleware/auth.py`'s `_is_valid`): claimed comparison time is
    always independent of key position, which is false — `any()`
    short-circuits on the first match. Corrected the docstring and the
    two docs copying it, with the accurate (and lower-severity) framing:
    the leak only manifests once a token is already valid.
  - Smaller inaccuracies: wrong version badges (both READMEs), a wrong
    Docker volume-mount target (`/app/data` vs. the real `/data`), `.env`
    precedence documented backwards, a stale expected-log-output order in
    the quickstart, and quantitative errors in the object-model doc
    ("hundreds" vs. the real low-thousands counts for abstract/relation/
    configurable classes).
  - `registry/descriptions.py`'s own module docstring mislabeled the
    intermediate "Rs/Rt-penalty-only" measurements (Recall@1 28.2%) as
    "v1" — the real final v1 state (after the prop_labels axis was added)
    scored 30.8%, matching `search-algorithm.md`'s own numbers. The v2
    comparison in the docstring now uses the correct baseline.
  - The scoring tables in `search-algorithm.md` and `registry.md` folded
    the comment field's direct-substring-match branch (+2) into the same
    row as its squared-coverage fallback (+1) — split into two rows,
    matching how the property-label branch was already documented.
  - `mcp/client/SKILL.md` (never previously audited): a wrong enum list for
    `topSystem.state` (claimed a nonexistent `unknown` value, missing 4 real
    ones), a false claim that querying an `isAbstract` class always returns
    `[]` (it returns the polymorphic union of concrete subclasses), a
    `relationFrom` example with an invented `sourceClass`, and
    `relationTo`/`relationFrom` examples missing the colon notation real
    schema keys actually use (`fv:RsCtx`, not `fvRsCtx`). Also reconciled a
    `time_range`-eligible-classes list that disagreed with `main.py`'s own
    docstring (`healthRecord` was missing from the latter).
  - CHANGELOG's own `[1.0.0]` entry claimed the license shipped was AGPL-3.0;
    git history shows PolyForm Noncommercial was already in place by the
    time that tag was cut (same-day license swap). Corrected. Also fixed
    `[0.2.0]`/`[0.3.0]` compare links pointing at a `v0.2.0` tag that was
    never created (replaced with the real commit SHA).
  - `overview.md` and two `main.py` comments quoted a "~300 classes" gap
    between the `schemas/` collection and `class-descriptions.json`;
    measured directly, it's ~200 (213).
  - `auth.md`/`middleware.md` described the `X-API-Key` fallback as
    triggered "only when `Authorization` is absent" — it's actually
    whenever `Authorization` doesn't start with `Bearer` (absent, empty, or
    a different scheme).
  - `ApicConnectionError`'s docstring claimed retry-then-raise universally;
    that's true for `query_class()`/`get_by_dn()`/`count_class()`'s shared
    request path, but `authenticate()` has no retry loop and raises
    immediately — docstring scoped correctly now.
  - `get_schema()` had no `Raises` section despite being able to raise
    `SchemaLoadError`; `query()`'s docstring was missing three real
    parameters (`filter_expr`, `rsp_subtree_include`, `time_range`) from its
    `Args`; `FilterError` was described (in `exceptions.py`, `main.py`, and
    the tool docs) as triggerable by a filter *value* — only an identifier
    (class/attribute name) can ever raise it, values are always escaped;
    `count()` shares the same `FilterError` exposure as `query()` but never
    documented it; `query.md`'s URL-construction diagram was missing
    `rsp-prop-include=config-only`.
  - `search-algorithm.md`'s tokenization example showed
    `"l3extRtVrfValidationPol"` splitting into `["l3", "ext", ...]`; the
    real `_TOKEN_RE` only splits at a lowercase→uppercase transition or an
    acronym-run boundary, neither of which occurs inside `"l3ext"` — it
    tokenizes as one token, `["l3ext", "rt", "vrf", "validation", "pol"]`.
    Confirmed by running the actual regex, not just re-reading it.

## [1.2.0] - 2026-07-20

### Added

- `mcp/client/SKILL.md` documents `dnFormats` — the full DN template chained
  from every ancestor's `rnFormat` — alongside the existing `rnFormat`
  coverage, with an explicit note that a repeated placeholder name (e.g.
  `{name}` appearing twice) reflects two ancestors sharing an identifying
  attribute and must be quoted verbatim, never renamed for readability.
- `ApicClient` retries a bounded number of times (default 3 attempts, small
  exponential backoff capped at 2s) on transient failures — connection
  errors/timeouts and HTTP 404/500/502/503/504 — before raising. A genuine
  application error (e.g. 400 for a malformed filter) is still raised
  immediately, never retried. Configurable via new `retry_attempts` /
  `retry_backoff_base` constructor arguments.
- `query()` gains `fetch_all=True` — walks every page (bounded by a safety
  cap of 25 pages / 5000 objects) and returns the complete matching set in
  one call. Closes a real gap where a default page's max/min/argmax over a
  class (e.g. "which bridge domain has the most subnets") could be silently
  wrong once the fabric grew past a single page. `count()`'s docstring and
  the server instructions now distinguish a pure tally (`count()`) from
  ranking/argmax (`fetch_all=True` plus local aggregation over `results`).

### Changed

- `apic/client.py` — `query_class()` now shares its transport/retry logic
  with `get_by_dn()`/`count_class()` via `_request_json()`, instead of
  duplicating the request-and-error-handling block. The 401/403
  re-authenticate-and-retry flow is unchanged, now isolated in a new
  `_send()` helper that the retry loop wraps.
- **Breaking:** `query()` now returns an envelope — `{results, returned,
  total_available, truncated, next_page, complete, note}` — instead of a
  bare list, so a partial page is never silently indistinguishable from a
  complete one. `total_available` is the APIC-reported true match count
  (previously parsed and discarded by `query_class()`); `truncated`/
  `complete` and an explicit `note` tell the caller when a max/min/total/
  all-of conclusion from the current response would be wrong.
  `mcp/client/SKILL.md`'s canonical response shape, pagination, and
  counting sections updated accordingly. Version bumped 1.1.0 → 1.2.0.

### Fixed

- `mcp/client/SKILL.md` and `main.py`'s server instructions now state
  explicitly that a tool error (unknown class, unreachable object, malformed
  filter) is a failed lookup, not an answer of zero or an empty result — and
  that every specific fact in a final answer (a property name, a configured
  value, a DN template, a count) must trace back to an actual tool result
  from the conversation rather than general ACI knowledge. Also fixes a
  stale row in SKILL.md's error-handling table that described a
  `{"error": ..., "closest_matches": [...]}` return shape `query` has never
  actually used — `UnknownClassError` is raised, not returned, matching the
  file's own `count` section a few paragraphs earlier.
- `mcp/client/SKILL.md` and `main.py` sharpen the grounding rule above with
  the two cases it's most often stretched past its evidence: `contains`
  lists child class names only, never what each one targets or means (that
  is `relationTo`'s job, and only for the Rs classes it lists); `properties`
  lists names only, never a property's type, default, or allowed values
  (that requires `property_details`). Naming a relation's target or a
  property's type/value without having actually requested that detail is
  the unsupported completion the rule forbids.

### Security

- Bumped locked dependencies to clear 36 known vulnerabilities flagged by
  the CI dependency audit across 10 packages (`fastmcp` 3.1.1→3.4.4, `mcp`
  1.26.0→1.28.1, `starlette` 1.0.0→1.3.1, `cryptography` 46.0.6→49.0.0,
  `authlib` 1.6.9→1.7.2, `click`, `idna`, `pydantic-settings`, `pyjwt`,
  `python-multipart`) — no direct dependency constraint changes, resolved
  within the existing `pyproject.toml` ranges via `uv lock --upgrade`.
- `pytest` bumped 8.4.2 → 9.1.1 (`pytest-asyncio` 0.26.0 → 1.4.0 for
  compatibility) to clear GHSA-6w46-j5rx-g56g (vulnerable tmpdir handling),
  a dev-only dependency vulnerability GitHub's Dependabot alerts caught but
  the CI dependency audit missed — that job ran `pip-audit` with `--no-dev`,
  excluding dev dependencies entirely. CI now audits the full dependency set
  including dev dependencies.

## [1.1.0] - 2026-07-20

Expands the tool surface with three new capabilities (direct DN lookup,
cheap counting, config-only reads), rewrites `search_classes` for a large
ranking-quality improvement, and closes a set of correctness and
reliability gaps in schema loading, error handling, and class validation.

### Added

- `get_by_dn(dn, config_only, include_children)` — fetch a single object
  directly by its Distinguished Name (`GET /api/mo/{dn}.json`), the
  shortcut path when the exact DN is already known. Returns a structured
  `{"found": false, ...}` message for a missing DN instead of a bare `[]`.
- `count(class_name, filters, scope_dn, filter_expr)` — count objects of a
  class via APIC `rsp-subtree-include=count` without transferring them.
  Validates the class name against the registry like `query` (raises
  `UnknownClassError` with suggestions).
- `get_schema` now returns `contains` — a sorted list of the child class
  names an object may hold, ready to feed to `get_schema`/`query`/
  `include_children` (previously dropped from the response).
- `get_schema` gains `include_property_details` and `properties_filter`,
  exposing a compact per-property constraint dict (`type`, `access`,
  `naming`, `mandatory`, `default`, `options`, `comment`). Opt-in, so a
  100+-property class doesn't bloat every response by default.
- `query` and `get_by_dn` gain `config_only` — adds
  `rsp-prop-include=config-only` so only user-configurable attributes are
  returned, dropping operational noise for comparison, drift detection,
  and backup.
- `search_classes` scoring rewritten from raw substring matching to
  tokenized, camelCase-aware ranking: exact label/class-name matches
  dominate, token coverage rewards a query that names most of a concept,
  and property-label phrase matches surface functional queries (e.g. "ARP
  flooding" → `fvBD`). Structural priors — a boost for `isConfigurable`
  classes, penalties for `isAbstract`, stats/telemetry, and Rs/Rt relation
  classes — resolve ties that pure text matching never could, since dozens
  of ACI classes share an identical Cisco-assigned label. A small curated
  ACI jargon/synonym table covers the handful of terms with no textual
  anchor anywhere in the schema. Measured on a 74-query golden set:

  | Metric   | Before | After |
  | -------- | ------ | ----- |
  | Recall@1 | 30.8%  | 78.4% |
  | Recall@5 | 53.8%  | 94.6% |

  See `docs/internals/search-algorithm.md` for the full mechanics.
- `data/class-descriptions.json` regenerated with `isConfigurable`/
  `isAbstract` flags per class, feeding the search priors above.
- `ApicRequestError`: wraps non-2xx, non-authentication APIC responses
  (400 for a malformed `filter_expr`, 404, 500, ...), carrying the HTTP
  status and, when present, the APIC-supplied error text. Previously these
  escaped as a raw, unhelpful `httpx.HTTPStatusError`.
- A search-quality gate (`mcp/tests/eval/`) runs the golden-set evaluation
  as a pytest test with a floor on Recall@1/5, so a scoring regression
  fails the suite instead of only showing up in an offline report. The
  golden set itself grew from 39 to 74 queries.
- Tool-layer wiring tests prove that `query`/`get_by_dn`/`count`'s
  parameters (`page`, `rsp_subtree_include`, `time_range`, `config_only`)
  reach the real APIC request the client builds, and that an invalid
  filter attribute raises `FilterError` all the way out through the tool.
- An end-to-end test suite (`mcp/tests/live/`) exercises the real APIC
  client against a live fabric with no stubs — object queries, `get_by_dn`
  (found and not-found), `count`, `config_only`, and real APIC error
  responses. Marked `@pytest.mark.live` and excluded from the default test
  run, since a CI runner has no network path to a real fabric; run
  explicitly with `uv run pytest tests/live/ -m live`.
- CI now generates a coverage report and runs the full test suite (minus
  `tests/perf/`), so the search-quality gate above is actually exercised
  on every push.

### Changed

- The server's discovery workflow (its startup instructions and the
  bundled skill guide) is now scoped to genuine discovery: when the exact
  class and DN are already known, `get_by_dn` is the documented shortcut
  instead of always requiring `search_classes → get_schema → query`. The
  skill guide also gained an eventual-consistency note — a read taken
  right after a large config push reflects the fabric state at that
  instant, and a count can move for a few seconds while the change
  materializes.
- Schema loading no longer scans the schema directory tree on every call;
  the real jsonmeta directory is resolved once at server startup.
- `query()` no longer rejects a class outright just because it's absent
  from `class-descriptions.json`: it falls back to a direct schema-file
  check before raising `UnknownClassError`, closing a ~300-class gap
  between the registry and the full schema collection.
- `search_classes` and `query` clamp `limit` to a floor of 1 instead of
  passing a non-positive value straight through.

### Fixed

- `get_schema()` cold-load latency: the per-call directory scan described
  above measured ~8.3 ms against the full schema collection, against a
  5 ms budget; the fix brings it under 1 ms.
- A negative or zero `limit` on `search_classes`/`query` could reach the
  APIC as an invalid `page-size`, or silently mis-slice a result list.
- The `query()` registry/schema fallback could be tricked by a typo (e.g.
  `fvBd` for `fvBD`) into treating a bogus class as valid on a
  case-insensitive filesystem (the macOS/Windows default). Fixed by
  comparing the schema file's own class-identity fields instead of
  trusting the filesystem lookup alone.
- Installing the project's declared development dependencies and running
  its test suite (`uv sync && pytest`) previously failed outright — an
  async-test dependency lived in an optional extra the plain install never
  pulled in.
- The public CI workflow's lint and dependency-audit jobs referenced a
  directory removed from the repository in an earlier cleanup, so both had
  been failing on every push since. Removed the dead steps.

---

## [1.0.0] - 2026-06-24

First public open-source release.

### Added

- `LICENSE` — PolyForm Noncommercial License 1.0.0. (Briefly AGPL-3.0-or-later
  earlier the same day; replaced before this tag was cut — every file's SPDX
  header and the shipped `LICENSE` text at `v1.0.0` are PolyForm Noncommercial,
  never AGPL.)
- `LICENSE-COMMERCIAL.md` — commercial license terms for proprietary integrations.
- `SKILL.md` — LLM skill guide at repo root for client discovery.
- `scripts/list-configurable-classes.sh` — query configurable ACI classes from jsonmeta
  schemas. Options: `--package`, `--exclude-rsrt`, `--count`.
- SPDX license headers (`Copyright (C) 2026 Khalid El-Ouiali — MONARK AIOPS srl /
  SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0`) on all Python source files.
- `[project.authors]`, `[project.urls]`, `classifiers`, `keywords` in both
  `mcp/pyproject.toml` and `schema-collector/pyproject.toml`.

### Changed

- Repository scope narrowed: lab orchestration tooling (`scripts/`, `Makefile`,
  `.env.example`) extracted to the separate `ai-netlab` repository. This repo now
  contains only the MCP server (`mcp/`) and the schema collector (`schema-collector/`).
- Build system removed: Nuitka/UPX standalone binary pipeline (`schema-collector/deploy/`)
  deleted. The schema collector is distributed as Python source only.
- `.gitignore` extended: `.claude/`, `.opencode/`, `.vscode/`, `CLAUDE.md`,
  `.markdownlint*`, `.lab*`.

---

## [0.3.0] - 2026-06-13

### Added

- `mcp/middleware/oauth.py` — `OAuthDiscoveryMiddleware`: intercepts
  `/.well-known/oauth-protected-resource` and `/.well-known/oauth-protected-resource/mcp`,
  returning RFC 9728 Protected Resource Metadata JSON. Prevents spec-compliant MCP clients
  (OpenCode, Claude Desktop) from crashing on a plain-text "Not Found" response.
- `mcp/middleware/auth.py` — `KeyStore`: thread-safe, hot-reloadable key container.
  `reload()` swaps the key set atomically; in-flight requests are unaffected.
- `mcp/middleware/auth.py` — `RateLimiter`: fixed-window per-IP limiter (default 30 attempts /
  60 s). Returns 429 with `Retry-After: 60` after threshold. Successful requests do not
  consume budget.
- 21 new unit tests for `middleware.auth` (KeyStore, RateLimiter, WWW-Authenticate, hot-reload).
  Total: 199 tests.

### Changed

- `mcp/middleware/auth.py` — `ApiKeyMiddleware` now takes a `KeyStore` instead of a raw
  `frozenset`; accepts an optional `RateLimiter`.
- `mcp/middleware/auth.py` — 401 responses include
  `WWW-Authenticate: Bearer resource_metadata="<url>"` per RFC 9728, so clients locate
  the discovery endpoint without probing multiple `/.well-known/` candidates.
- `mcp/main.py` — `ApiKeyMiddleware` is always added to the middleware stack (no-op when
  `KeyStore` is empty, eliminating the conditional branch). `OAuthDiscoveryMiddleware` added
  as outermost middleware in all modes.
- `mcp/main.py` — SIGHUP handler installed at startup: sends `SIGHUP` to reload
  `MCP_API_KEYS` from `.env` without restarting the server.

### Security

- Per-IP rate limiting on failed auth attempts prevents brute-force token enumeration.
- `WWW-Authenticate` header now hints at the discovery URL, allowing clients to complete
  the MCP 2025-03-26 OAuth discovery flow without exposing internal server details.

---

## [0.2.0] - 2026-06-13

### Added

- `mcp/middleware/auth.py` — `ApiKeyMiddleware`: Starlette `BaseHTTPMiddleware` validating
  `Authorization: Bearer <token>` or `X-API-Key: <token>` on every incoming request.
  Token comparison uses `hmac.compare_digest` to prevent timing-oracle attacks.
  No-op (with startup warning) when `MCP_API_KEYS` is unset — preserves local dev UX.
- `mcp/deploy/Caddyfile` — TLS-terminating reverse proxy; supports Let's Encrypt (public domain)
  and Caddy's internal CA (LAN/self-signed). Security headers included (`HSTS`, `X-Frame-Options`, …).
- `mcp/deploy/docker-compose.yml` — two-service production stack: `mcp` (internal only) + `caddy`
  (ports 80/443). MCP container is not exposed on the host — all traffic enters via Caddy.
- `mcp/exceptions.py` — `AuthenticationError` added to the exception hierarchy.
- `docs/` — project wiki: architecture diagrams (Mermaid), deployment guides, tools reference,
  internals documentation, and full settings reference.
- 26 new unit tests for `middleware.auth` (load, extract, validate, HTTP integration).

### Changed

- `mcp/pyproject.toml` — version bumped to `0.2.0`; all dependency version constraints
  now have explicit upper bounds.
- `mcp/main.py` — `_serve()` now conditionally applies `ApiKeyMiddleware` via
  `run_http_async(middleware=[...])` when `MCP_API_KEYS` is configured.

### Security

- All MCP endpoints are now protected behind API key authentication when `MCP_API_KEYS` is set.
- TLS is provided end-to-end by Caddy when deployed via `docker-compose.yml`.
- The MCP server port (`8000`) is never exposed directly to the host in the production stack.

---

## [0.1.0] - 2026-06-12

### Added

- `mcp/` — FastMCP server exposing three tools: `search_classes`, `get_schema`, `query`
- `mcp/apic/client.py` — APIC REST client with cookie auth and auto-reauth on 401/403
- `mcp/registry/` — lazy schema loading, keyword search, query-target-filter builder
- `mcp/deploy/Dockerfile` — container image (build context: repo root)
- `mcp/client/` — ready-made MCP client config and LLM skill doc
- `schema-collector/collect.py` — unified CLI (`aci-collect`) replacing four standalone scripts
  - `run` — full pipeline with `--from`, `--concurrency`, `--force`
  - `status` — rich table showing artifact state per APIC version
  - `clean` — remove generated artifacts, with optional `--version` targeting
- Versioned artifact layout: `cobra-sdk/{apic_version}/` and `mo-schemas/{apic_version}/`
- APIC version auto-detected via `firmwareCtrlrRunning` after authentication
- Shared `data/` at monorepo root — written by `schema-collector`, read by `mcp`

### Changed

- Monorepo structure: `mcp/` and `schema-collector/` as independent Python projects
- `class-descriptions.json` centralised in `data/` (was duplicated in both subprojects)

---

[Unreleased]: https://github.com/k3l0-dev/niwashi-mcp/compare/v2.0.0...HEAD
[2.0.0]: https://github.com/k3l0-dev/niwashi-mcp/compare/v1.2.2...v2.0.0
[1.2.2]: https://github.com/k3l0-dev/niwashi-mcp/compare/v1.2.1...v1.2.2
[1.2.1]: https://github.com/k3l0-dev/niwashi-mcp/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/k3l0-dev/niwashi-mcp/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/k3l0-dev/niwashi-mcp/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/k3l0-dev/niwashi-mcp/compare/v0.3.0...v1.0.0
[0.3.0]: https://github.com/k3l0-dev/niwashi-mcp/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/k3l0-dev/niwashi-mcp/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/k3l0-dev/niwashi-mcp/releases/tag/v0.1.0
