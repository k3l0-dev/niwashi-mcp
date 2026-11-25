# Copyright (C) 2026 Khalid El-Ouiali — MONARK AIOPS srl
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""
exceptions.py

All niwashi-mcp exceptions in a single module so callers can import from one place.

Hierarchy
---------
NiwashiMcpError
├── ConfigurationError        — missing or invalid startup configuration
├── AuthenticationError       — incoming request carries no valid API key
├── RegistryError             — base for registry load failures
│   ├── DescriptionsLoadError — niwaki catalogue missing, unreadable, or moved
│   ├── RedactionPolicyError  — niwaki's secret-redaction policy missing or moved
│   └── SchemaLoadError       — retained for compatibility; unreachable since 2.0
├── UnknownClassError         — class name not found in the descriptions registry
├── UnknownAttributeError     — attribute not among the class's properties in the catalogue
├── SecretAttributeError      — filter/order/group on an attribute that holds a secret
├── SecretDnError             — a DN that is itself named by a secret
├── FilterError               — invalid identifier or unsafe value in build_filter
└── ApicError                 — base for APIC communication errors
    ├── ApicAuthError         — authentication failed (bad credentials or server error)
    ├── ApicConnectionError   — APIC unreachable (network error or timeout)
    ├── ApicResponseError     — APIC returned an unexpected or malformed response
    └── ApicRequestError      — APIC rejected the request (400/404/500/... — non-auth)
"""


class NiwashiMcpError(Exception):
    """Base exception for all niwashi-mcp errors."""


# ── Configuration ─────────────────────────────────────────────────────────────


class ConfigurationError(NiwashiMcpError):
    """Required environment variable is missing or has an invalid value.

    Raised at server startup before any tool is served.
    """


# ── Authentication ────────────────────────────────────────────────────────────


class AuthenticationError(NiwashiMcpError):
    """Incoming MCP request is missing or carrying an invalid API key.

    Raised by _authenticate() in middleware/auth.py and caught there to produce
    the 401 HTTP response. Keeping auth logic in a pure function that raises
    this exception makes it independently testable without an HTTP layer.
    """


# ── Registry ──────────────────────────────────────────────────────────────────


class RegistryError(NiwashiMcpError):
    """Base for registry (descriptions / schemas) load failures."""


class DescriptionsLoadError(RegistryError):
    """The niwaki catalogue is missing, unreadable, or has moved.

    Raised by `registry.catalog` — at startup by `verify_catalogue()` when a
    table, column, manifest key or stored encoding is not what this server
    reads, and at any time when `catalog.db` cannot be opened. It signals a
    broken installation, never a missing ACI class: an unknown class returns
    an empty dict from `load_schema()`.

    The remedy is to reinstall the dependency that carries the catalogue:
        pip install --force-reinstall niwaki
    """


class RedactionPolicyError(RegistryError):
    """niwaki's secret-redaction policy is missing, or no longer what we read.

    Raised at startup by `redact.verify_policy()`. The policy tables
    (`niwaki._secrets`) are private to niwaki, which is pinned to one minor for
    exactly that reason. A copy of them would go stale silently, and a server
    that cannot say which values are secret must not start: serving without the
    policy would hand credentials to an agent.

    The remedy is the pin: `niwaki>=2.2,<2.3`.
    """


class SchemaLoadError(RegistryError):
    """A jsonmeta schema file exists on disk but could not be parsed.

    Indicates a corrupted or manually edited schema file.
    """

    def __init__(self, class_name: str, path: str, reason: str) -> None:
        self.class_name = class_name
        self.path = path
        super().__init__(f"Malformed schema for '{class_name}' at {path}: {reason}")


# ── Class validation ──────────────────────────────────────────────────────────


class UnknownClassError(NiwashiMcpError):
    """ACI class name not found in the descriptions registry.

    Raised by the query() tool when the caller supplies a class name that is
    not in the in-memory descriptions index.  Includes closest matches so the
    LLM can self-correct without an additional search_classes() round-trip.
    """

    def __init__(
        self, class_name: str, suggestions: list[str], registry_size: int
    ) -> None:
        self.class_name = class_name
        self.suggestions = suggestions
        self.registry_size = registry_size
        hint = (
            f"Closest matches: {', '.join(suggestions)}"
            if suggestions
            else "No close matches found."
        )
        super().__init__(
            f"Unknown ACI class '{class_name}' — not in the {registry_size}-class registry. "
            f"{hint} Call search_classes() to find the correct name."
        )


class UnknownAttributeError(NiwashiMcpError):
    """Attribute name not among a known ACI class's properties in the catalogue.

    The attribute-level counterpart of `UnknownClassError`: APIC answers a
    filter on a misspelled or invented attribute with `[]` and no error, so the
    name is checked against the catalogue before the request is sent. Carries
    closest matches so the LLM can self-correct without a `get_schema()`
    round-trip.

    The check is against the catalogue, which describes one APIC release, and
    a fabric on another release may legitimately carry the attribute. The
    message says so, because the caller may decide to send the request anyway;
    `catalogue_version` is a required argument rather than read from
    `registry.catalog`, which imports this module and would make a cycle.
    """

    def __init__(
        self,
        class_name: str,
        attribute: str,
        suggestions: list[str],
        catalogue_version: str,
    ) -> None:
        self.class_name = class_name
        self.attribute = attribute
        self.suggestions = suggestions
        self.catalogue_version = catalogue_version
        hint = (
            f"Closest matches: {', '.join(suggestions)}."
            if suggestions
            else "No close matches found."
        )
        described = (
            f"APIC {catalogue_version}"
            if catalogue_version and catalogue_version != "unknown"
            else "an APIC release this server cannot identify"
        )
        super().__init__(
            f"Unknown attribute '{attribute}' on ACI class '{class_name}'. "
            f"{hint} Call get_schema('{class_name}') to list its attributes. "
            f"The catalogue describes {described}; a fabric on another release "
            f"may legitimately have this attribute."
        )


# ── Secrets ───────────────────────────────────────────────────────────────────
#
# Both messages are written for the agent that receives them, and neither quotes
# what the agent sent: a refused guess is not echoed back into a log or a reply.


class SecretAttributeError(NiwashiMcpError):
    """A request filters, orders or groups on an attribute that holds, or may quote, a secret.

    The server never returns such a value, but a predicate on it would answer
    questions about it all the same: `filters={"value": "guess"}` returning one
    object confirms the guess, and `group_by` would print the values as keys. So
    the request is refused whatever `NIWASHI_MCP_STRICT_ATTRIBUTES` says — it
    is not a catalogue check, and relaxing it would reopen the oracle.

    `free_text` is the second kind: an audit record's `changeSet`, which is not a
    secret itself but is where one gets quoted. It is scrubbed on a best-effort
    basis, and a predicate on the raw text could confirm what the scrubbing
    removed, so it is refused the same way.
    """

    def __init__(
        self, class_name: str, attribute: str, where: str, *, free_text: bool = False
    ) -> None:
        self.class_name = class_name
        self.attribute = attribute
        self.where = where
        self.free_text = free_text
        if free_text:
            reason = (
                f"'{attribute}' on ACI class '{class_name}' is free text that can quote "
                "a secret (a credential, key, community string or session token). The "
                "server scrubs it on a best-effort basis, and a match on the raw text "
                "could confirm what was scrubbed, so "
            )
        else:
            reason = (
                f"'{attribute}' on ACI class '{class_name}' holds a secret "
                "(a credential, key or community string), and the server withholds it, "
                "so "
            )
        super().__init__(
            f"{reason}`{where}` cannot use it: a filter, order or grouping on it would "
            "let a guess be checked or the values be read back. "
            f"Use another attribute of '{class_name}'; the objects themselves still "
            "come back, with that value withheld or scrubbed."
        )


class SecretDnError(NiwashiMcpError):
    """A DN or filter text names an object by a secret.

    An SNMP community profile is named by its community string, a login session
    by its token hash, a JWT site by its API key: the secret is part of the DN.
    Fetching `.../community-public` and being told "found" or "not found" is a
    way to check a guess, so every such DN is refused on its shape alone —
    before the APIC is asked, and the same whether or not the object exists.
    """

    def __init__(self, where: str) -> None:
        self.where = where
        super().__init__(
            f"`{where}` names an object by a secret: an SNMP community string, a "
            "login-session token or an API key is part of that DN. The server "
            "refuses every DN of that shape — whether or not such an object "
            "exists — so a request cannot be used to check a guess. Query the "
            "class, or the parent's DN, instead: the objects come back with "
            "the secret withheld."
        )


# ── Filter ────────────────────────────────────────────────────────────────────


class FilterError(NiwashiMcpError):
    """Invalid input to build_filter().

    Raised when a class name or attribute key contains characters outside
    the expected ACI identifier format. Filter values are never rejected —
    they are always escaped instead, so a value can never trigger this.
    """


# ── APIC communication ────────────────────────────────────────────────────────


class ApicError(NiwashiMcpError):
    """Base for all APIC communication errors."""


class ApicAuthError(ApicError):
    """APIC authentication failed.

    Raised when the APIC returns a non-2xx response to the login request,
    or when re-authentication after a 401/403 still fails.
    """

    def __init__(self, host: str, status: int, detail: str = "") -> None:
        self.host = host
        self.status = status
        msg = f"APIC authentication failed for {host} (HTTP {status})"
        if detail:
            msg += f": {detail}"
        super().__init__(msg)


class ApicConnectionError(ApicError):
    """APIC is unreachable — network error or request timeout.

    Wraps httpx.ConnectError and httpx.TimeoutException so callers do not
    need to import httpx to handle connectivity problems.

    Retry behavior differs by call site: query_class(), get_by_dn(), and
    count_class() share a request path (_request_json()) that retries a
    bounded number of times (see ApicClient.__init__'s retry_attempts) with
    a short backoff before giving up — for those three, this is raised only
    once that budget is exhausted, never on the first connection failure
    alone. authenticate() has no retry loop of its own and raises this
    immediately on the first connection failure.
    """

    def __init__(self, host: str, reason: str) -> None:
        self.host = host
        super().__init__(f"Cannot reach APIC at {host}: {reason}")


class ApicResponseError(ApicError):
    """APIC returned an unexpected or malformed response body.

    Raised when the response is not valid JSON, or when the expected
    'imdata' key is absent from an otherwise successful response.
    """

    def __init__(self, url: str, reason: str) -> None:
        self.url = url
        super().__init__(f"Unexpected APIC response from {url}: {reason}")


class ApicRequestError(ApicError):
    """APIC rejected a request with a non-2xx, non-authentication status.

    Raised by ApicClient.query_class(), get_by_dn(), and count_class() (all
    three share the same request path) for any status code other than
    401/403 (which trigger the re-auth-and-retry flow instead) — typically
    400 for a malformed filter_expr or query-target-filter, or a transient
    404/500/502/503/504 that never recovered within the retry budget (a
    permanent status like 400 is raised on the first attempt; a transient
    one is retried a bounded number of times first — see
    ApicClient.__init__'s retry_attempts).

    Carries the raw HTTP status and, when the response body follows APIC's
    usual error shape (`imdata[0].error.attributes.text`), the human-readable
    reason APIC supplied — so an LLM caller gets an actionable message
    instead of an opaque httpx.HTTPStatusError.
    """

    def __init__(self, url: str, status: int, apic_text: str = "") -> None:
        self.url = url
        self.status = status
        self.apic_text = apic_text
        msg = f"APIC request to {url} failed with HTTP {status}"
        if apic_text:
            msg += f": {apic_text}"
        super().__init__(msg)
