# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| 2.x | ✅ |
| 1.x | ✗ — superseded by 2.0 |
| < 1.0 | ✗ |

## Reporting a vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Report vulnerabilities by email to **monark.aiops@pm.me** with the subject
line `[niwashi-mcp] Security Vulnerability`.

Include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (optional)

You will receive an acknowledgement within **48 hours** and a resolution
timeline within **7 days**. We will notify you when the fix is released.

## Scope

In scope:
- Authentication bypass
- Token leakage or exposure
- APIC credential exposure
- Secrets returned to an MCP client by a tool result — a credential, key,
  community string or session token that `query`, `get_by_dn` or `count` hands
  over, or that a request can recover (see below)
- Prompt injection via MCP tool responses
- Dependency vulnerabilities with a known CVE

Out of scope:
- Vulnerabilities in the APIC itself
- Issues requiring physical access to the deployment host
- Denial of service against the Cisco sandbox (public shared resource)

## What the server withholds from an MCP client

This server is read-only, but its answers are handed to an LLM agent and, through
it, to whoever the agent is talking to. An APIC returns some secrets in clear
(device credentials, SNMP communities, the community string that *names* an
object, session tokens) and writes them into its own audit and event records, so
the tools redact them. In summary:

- A secret attribute reads `<redacted>` in every object returned, children
  included, even when empty. A DN that carries a secret has that segment cut.
- Free text of audit, event, fault and session records (`changeSet`, `descr`) is
  scrubbed on a **best-effort** basis; the response says so.
- A filter, `order_by`, `group_by` or DN that would let a client check a guess is
  refused, whatever `NIWASHI_MCP_STRICT_ATTRIBUTES` says. There is no setting that
  turns redaction off.
- The policy is niwaki's own, verified at startup; the server refuses to start if
  it has moved.

The full account — sources, decisions, and the cases it does **not** cover
(a fabric newer than the catalogue, a secret reused as a name, substring probes
through other classes' text) — is in
[docs/internals/redaction.md](docs/internals/redaction.md). A secret reaching a
client by any route not described there as a known limit is a vulnerability;
report it as above, without including the value.
