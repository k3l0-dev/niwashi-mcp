# Supported APIC releases

`niwashi-mcp` ships **one** ACI object model at a time. It travels inside the
[`niwaki`](https://pypi.org/project/niwaki/) dependency as a single SQLite
catalogue, which is what makes `uvx niwashi-mcp` work with no download and no
data directory.

| Currently shipped | Classes | Source |
|---|---:|---|
| **APIC 6.0(9c)** | 15,452 | `niwaki>=2.2,<2.3` |

The server logs the catalogue's release at startup, then asks the fabric what it
is running and compares the two:

```text
Registry loaded — 15239 class descriptions (niwaki catalogue, APIC 6.0(9c))
Connected to APIC — your-apic.example.com
Fabric runs APIC 6.0(9c), matching the object-model catalogue
```

The fabric's release comes from the controller itself
(`firmwareCtrlrRunning.version`, read once after the login). `apic-6.0(9c)` and
`6.0(9c)` are the same release; case and whitespace do not matter.

---

## Running a different release

Most of it still works. The object model is stable across trains and the
overwhelming majority of classes are identical between them.

What does not work is narrow but worth knowing: **classes added, removed or
renamed in your release are missing or wrong.** What happens depends on which
way the gap runs:

- **The fabric has it, the catalogue does not** (a class added in your release):
  the server refuses the name as unknown before the APIC is asked.
- **The catalogue has it, the fabric does not** (a class removed or renamed in
  your release): the request goes through, and the APIC does not return an error
  for a class it does not know — it returns an empty result, which is
  indistinguishable from "there are none". This is the quiet one.

When the fabric's release differs from the catalogue's, startup says so rather
than leaving you to find out from a query:

```text
WARNING  niwashi-mcp.apic  Fabric runs APIC 6.1(2f) but the object-model catalogue describes APIC 6.0(9c). Classes and attributes added, removed or renamed between the two releases may be refused as unknown (the fabric has them, the catalogue does not) or come back empty (the catalogue has them, the fabric does not) — and an empty result reads as "there are none". See https://github.com/k3l0-dev/niwashi-mcp/blob/main/SUPPORTED-APIC.md
```

The check is advisory and never stops the server. If the lookup fails, returns
nothing, or returns a release string the server cannot read (for instance the
account may not read firmware classes), you get a warning that the fabric's
release is **unknown** — not a mismatch. An unreadable answer is never reported
as a different release.

An empty result on a class you are confident exists is still the symptom to
watch for when the two releases differ. It is the one case where the version gap
can mislead you.

---

## Requesting a release

Add a row below in a pull request. That is the whole contribution — no data, no
export from your controller, nothing that leaves your fabric.

| APIC release | Requested | By |
|---|---|---|
| _e.g. 6.1(2f)_ | _2026-08-09_ | _@your-handle_ |

Demand decides order. When a release has askers, a catalogue for it is built and
published in `niwaki`, and `niwashi-mcp` picks it up in its next release. The
pull request is where you will get a realistic timing rather than an open-ended
promise.

If you hit a case where the version gap produced a **wrong** answer rather than
an empty one, please open an issue instead. That is a worse failure than the one
described above and has not been observed.
