# Copyright (C) 2026 Khalid El-Ouiali — MONARK AIOPS srl
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
apic/client.py

Async HTTP client for the Cisco APIC REST API.

Handles authentication (cookie-based token), class queries, and subtree queries.
A single ApicClient instance is created at server startup via the lifespan and
shared across all tool invocations through the FastMCP context.

query_class() returns a QueryResult (objects + the APIC-reported totalCount +
a completeness flag) rather than a bare list, so callers can tell a partial
page from the whole matching set — see QueryResult and the fetch_all param.

APIC endpoint reference:
  POST /api/aaaLogin.json                          — authenticate
  GET  /api/class/{cls}.json                       — fabric-wide class query
  GET  /api/mo/{dn}.json?query-target=subtree&...  — subtree query under a DN
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from niwashi_mcp.exceptions import (
    ApicAuthError,
    ApicConnectionError,
    ApicRequestError,
    ApicResponseError,
    FilterError,
)
from niwashi_mcp.registry.filter import build_filter

logger = logging.getLogger("niwashi-mcp.apic")

# fetch_all — how much one call may pull, and in what size of pages.
#
# A fabric-wide class scan with no scope_dn can be unbounded (a bad filter, or a
# class with tens of thousands of instances), so the page loop needs a hard stop
# of its own: _MAX_OBJECTS is the most objects one fetch_all ever holds.
#
# The page size is fixed here and is NOT the caller's `limit`. It used to be:
# the loop paged by `limit` and stopped after 25 pages, so the default
# limit=20 capped a "fetch everything" call at 25 x 20 = 500 objects — a tenth
# of the advertised 5,000 — and reported complete=False on a result set of 600
# that the cap was never meant to touch. 200 is the largest page the `query`
# tool already sends (its `limit` clamp is [1, 200]), so it is a page-size this
# server issues to the APIC anyway rather than a new one, and a full-size walk
# is 25 requests instead of 250.
#
# _MAX_OBJECTS must be a multiple of the page size: the cap then lands on a page
# boundary, the loop never holds an object past it, and nothing is fetched only
# to be thrown away. tests/unit/test_client.py pins that relationship.
_FETCH_ALL_PAGE_SIZE = 200
_MAX_OBJECTS = 5000

# The hard bound on the loop, derived so the two numbers above cannot drift
# apart: a walk that never meets a short page or the reported total still ends
# after this many requests. (25 at the current values.)
_MAX_PAGES = _MAX_OBJECTS // _FETCH_ALL_PAGE_SIZE

# HTTP statuses treated as transient and worth a bounded retry, rather than an
# immediate permanent failure. 404 is included deliberately: nowhere in this
# client does a real "doesn't exist" condition surface as an HTTP 404 —
# query_class()/count_class() only ever reach the backend with a class name
# already validated against the local registry, and get_by_dn()'s "no object
# at this DN" case is APIC returning 200 with an empty imdata list, not a
# 404. An observed 404 here is therefore presumptively infrastructure noise
# (a proxy/load-balancer hiccup in front of the APIC), not an application
# error — confirmed empirically by repeating one otherwise-successful
# request several times against a live fabric and seeing it intermittently
# fail with a bare 404 (no APIC error body) between successes. A genuine
# application-level rejection (a malformed filter_expr, for instance) comes
# back as 400, which is deliberately NOT in this set — retrying a real 400
# would only add latency to a failure that will never succeed.
_TRANSIENT_STATUSES = frozenset({404, 500, 502, 503, 504})


# How much of an APIC error code or text is kept. The text is the APIC's own
# sentence about the request ("Incorrect filter format for fvBD.unicastRoute,
# value 'False' is not valid"), usually one line of under a hundred characters;
# this is a ceiling for the day one is not.
_MAX_ERROR_TEXT = 500


def _clean_apic_text(value: object) -> str:
    """An APIC error code or text as one bounded line, or "" when it is not text.

    Both come off the wire and end up in a tool error and in logs, so control
    characters are dropped, every run of whitespace (newlines included) becomes
    a single space, and the length is capped. Anything that is not a string or
    an integer is treated as absent rather than formatted: an error whose shape
    is not the APIC's is still an error, it just has nothing to quote.
    """
    if not isinstance(value, (str, int)):
        return ""
    text = " ".join(
        "".join(ch for ch in str(value) if ch.isprintable() or ch.isspace()).split()
    )
    return text if len(text) <= _MAX_ERROR_TEXT else text[: _MAX_ERROR_TEXT - 1] + "…"


def _apic_errors(body: object) -> list[tuple[str, str]]:
    """The (code, text) of every `error` object among a body's `imdata` items.

    The APIC answers a request it refused with an `imdata` whose item is an
    error object — `{"error": {"attributes": {"code": "301", "text": "..."}}}` —
    and a `totalCount` of "1". It is not a managed object, and must never be
    parsed as one. Opportunistic about shape: a body that is not JSON-shaped
    like that, or an error object without attributes, yields "" for what is
    missing instead of raising, so the caller can still raise on the error.

    Returns:
        One pair per error item, in order; empty when there is none.
    """
    imdata = body.get("imdata") if isinstance(body, dict) else None
    if not isinstance(imdata, list):
        return []
    errors: list[tuple[str, str]] = []
    for item in imdata:
        if not isinstance(item, dict) or "error" not in item:
            continue
        error = item["error"]
        attributes = error.get("attributes") if isinstance(error, dict) else None
        if not isinstance(attributes, dict):
            attributes = {}
        errors.append(
            (_clean_apic_text(attributes.get("code")), _clean_apic_text(attributes.get("text")))
        )
    return errors


def _request_error(url: str, status: int, body: object) -> ApicRequestError:
    """The ApicRequestError for a refused request, quoting the APIC's reason if it gave one.

    Only the first error object is quoted: the APIC sends one, and the others
    would only be a longer message.
    """
    errors = _apic_errors(body)
    code, text = errors[0] if errors else ("", "")
    return ApicRequestError(url, status, text, code)


def _error_body(resp: httpx.Response) -> object:
    """The decoded body of an error response, or None when it is not JSON."""
    try:
        return resp.json()
    except ValueError:
        return None


# Characters and shapes that must never reach the path of an APIC URL. A DN is
# interpolated straight into `/api/mo/{dn}.json`, so anything that can leave
# that path segment changes *which endpoint* the server's authenticated session
# reaches — not merely which object it asks for.
#
# Measured before the guard existed: `uni/tn-OT/../../api/aaaListDomains`
# resolved to `https://apic/api/mo/api/aaaListDomains.json`. A DN arrives from
# an LLM, and an LLM's input can be attacker-influenced, so this is a
# confused-deputy hazard rather than a theoretical one.
_DN_FORBIDDEN = ("?", "#", "\\", "\n", "\r", "\t", "\x00")


def validate_dn(dn: str, *, field: str = "dn") -> str:
    """Return `dn` unchanged, or raise if it could leave its URL path segment.

    Deliberately a rejection rather than a sanitisation: silently rewriting a
    DN would answer a question the caller did not ask, which is the failure
    mode this whole server is built to avoid. A malformed DN is a mistake worth
    surfacing.

    Args:
        dn: The distinguished name to check.
        field: Parameter name to quote in the error, so the caller knows which
            argument to fix.

    Raises:
        FilterError: The DN is empty, absolute, contains a traversal segment,
            or carries a character that would escape the path.
    """
    if not dn or not dn.strip():
        raise FilterError(f"{field} must not be empty")

    if dn.startswith("/"):
        raise FilterError(
            f"{field} must be relative, got {dn!r} — an ACI DN never starts with '/'"
        )

    for char in _DN_FORBIDDEN:
        if char in dn:
            raise FilterError(
                f"{field} contains {char!r}, which would escape the URL path: {dn!r}"
            )

    # `..` is only a traversal as a whole segment; it is legitimate inside a
    # name (a tenant may be called `a..b`), so match segments, not substrings.
    if any(segment == ".." for segment in dn.split("/")):
        raise FilterError(
            f"{field} contains a '..' path segment, which would target a "
            f"different APIC endpoint: {dn!r}"
        )

    return dn


@dataclass
class QueryResult:
    """Result of a query_class() call — objects plus the true match size.

    Attributes:
        objects:          Parsed attribute dicts, in the same shape query_class()
                          has always returned (each with a "_class" key, and
                          "_children" when include_children was set).
        total_available:  The APIC-reported totalCount for the query — the
                          true number of matching objects fabric-wide (or
                          subtree-wide with scope_dn), independent of how many
                          were actually fetched into `objects`.
        complete:         True when `objects` holds every object the query
                          matches. Without fetch_all that is always True — it
                          describes the loop, not the result set, and
                          `total_available` is what says how much exists.
                          With fetch_all=True it is False when the walk stopped
                          before reaching the APIC's totalCount: at
                          _MAX_OBJECTS, or because the APIC ran out of pages
                          early. Never True on the strength of a guess.
    """

    objects: list[dict[str, Any]] = field(default_factory=list)
    total_available: int = 0
    complete: bool = True


class ApicClient:
    """Async APIC REST client with session cookie management.

    Create one instance at startup, call authenticate(), then reuse across
    requests.  Call close() during shutdown to release the underlying
    httpx.AsyncClient.
    """

    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        verify_ssl: bool = False,
        timeout: float = 30.0,
        retry_attempts: int = 3,
        retry_backoff_base: float = 0.2,
    ) -> None:
        """Initialise the client without opening a connection.

        Args:
            host:               APIC hostname or IP (no scheme), e.g. "10.41.71.11".
            user:               APIC username, typically "admin".
            password:           APIC password. Never logged.
            verify_ssl:         Set True to enforce TLS certificate verification.
            timeout:            Per-request timeout in seconds (default 30 s).
            retry_attempts:     Total attempts (including the first) for a
                                transient failure — see _TRANSIENT_STATUSES —
                                before it is raised as a permanent error.
            retry_backoff_base: Base delay in seconds for the exponential
                                backoff between retries (doubles each attempt,
                                capped at 2s). Set to 0 in tests to avoid
                                real sleeps.
        """
        self._host = host
        self._user = user
        self._password = password
        self._base = f"https://{host}"
        # Redirects are never followed. httpx's default is already False; it is
        # set so the rule does not rest on a default. The APIC's 3xx is not a
        # redirect (see ApicRequestError), and a real one would send this
        # session's cookie somewhere this client was not pointed at.
        self._client = httpx.AsyncClient(
            verify=verify_ssl, timeout=timeout, follow_redirects=False
        )
        self._retry_attempts = retry_attempts
        self._retry_backoff_base = retry_backoff_base

    def _backoff_delay(self, attempt: int) -> float:
        """Delay before retry attempt `attempt` (1-based), exponential and capped.

        attempt=1 → base, attempt=2 → 2xbase, ... capped at 2.0s so a run of
        transient failures can't turn a single tool call into a multi-second
        stall for the LLM agent waiting on it.
        """
        return min(self._retry_backoff_base * (2 ** (attempt - 1)), 2.0)

    async def authenticate(self) -> None:
        """Obtain an APIC session token and store it as a cookie.

        Sends a POST to /api/aaaLogin.json and sets the returned token as the
        APIC-cookie on the underlying httpx client so all subsequent requests
        are authenticated automatically.

        Raises:
            ApicAuthError:       APIC returned a non-2xx response to the login.
            ApicConnectionError: Host unreachable or request timed out.
            ApicResponseError:   Response body is not valid JSON or missing token.
        """
        url = f"{self._base}/api/aaaLogin.json"
        payload = {
            "aaaUser": {"attributes": {"name": self._user, "pwd": self._password}}
        }
        try:
            resp = await self._client.post(url, json=payload)
        except httpx.TimeoutException as exc:
            raise ApicConnectionError(self._host, f"request timed out: {exc}") from exc
        except httpx.ConnectError as exc:
            raise ApicConnectionError(self._host, str(exc)) from exc

        if not resp.is_success:
            raise ApicAuthError(self._host, resp.status_code)

        try:
            data = resp.json()
            token: str = data["imdata"][0]["aaaLogin"]["attributes"]["token"]
        except (KeyError, IndexError, ValueError) as exc:
            raise ApicResponseError(
                url, f"unexpected login response body: {exc}"
            ) from exc

        self._client.cookies.set("APIC-cookie", token)
        logger.info("Authenticated to APIC as %s @ %s", self._user, self._host)

    def _build_query_params(
        self,
        class_name: str,
        filters: dict[str, str],
        scope_dn: str,
        limit: int,
        order_by: str,
        include_children: list[str] | None,
        filter_expr: str | None,
        rsp_subtree_include: str | None,
        time_range: str | None,
        config_only: bool,
    ) -> tuple[str, dict[str, str]]:
        """Build the (url, params) pair shared by every query_class() page request.

        `page` is deliberately excluded — the caller sets it per request, since
        a fetch_all loop needs the same base params with only `page` varying.
        """
        params: dict[str, str] = {"page-size": str(limit)}

        if scope_dn:
            url = f"{self._base}/api/mo/{validate_dn(scope_dn, field='scope_dn')}.json"
            params["query-target"] = "subtree"
            params["target-subtree-class"] = class_name
        else:
            url = f"{self._base}/api/class/{class_name}.json"

        eq_filter = build_filter(class_name, filters)
        if filter_expr and eq_filter:
            params["query-target-filter"] = f"and({filter_expr},{eq_filter})"
        elif filter_expr:
            params["query-target-filter"] = filter_expr
        elif eq_filter:
            params["query-target-filter"] = eq_filter

        if order_by:
            params["order-by"] = order_by
        if include_children:
            params["rsp-subtree"] = "children"
            params["rsp-subtree-class"] = ",".join(include_children)
        if rsp_subtree_include:
            params["rsp-subtree-include"] = rsp_subtree_include
            # rsp-subtree-include selects *which* categories come back; it does
            # not ask for a subtree at all. Sent alone the APIC returns the
            # bare objects, so "give me BDs with their faults" silently became
            # "give me the BDs that have faults" — and the agent concluded
            # there were none.
            params.setdefault("rsp-subtree", "children")
        if time_range:
            params["time-range"] = time_range
        if config_only:
            params["rsp-prop-include"] = "config-only"

        return url, params

    @staticmethod
    def _parse_query_objects(
        body: dict[str, Any], include_children: list[str] | None
    ) -> list[dict[str, Any]]:
        """Parse one APIC class/subtree response body into attribute dicts."""
        objects: list[dict[str, Any]] = []
        for item in body.get("imdata", []):
            for cls, obj in item.items():
                attrs: dict[str, Any] = dict(obj.get("attributes", {}))
                attrs["_class"] = cls
                # Extract whatever children the APIC actually returned, not
                # only those this call asked for by class. rsp-subtree-include
                # produces children too (faults, health, audit-logs) and gating
                # on include_children dropped them on the floor, with the
                # object itself arriving intact — a silent partial answer.
                if "children" in obj:
                    children: list[dict[str, Any]] = []
                    for child_item in obj["children"]:
                        for child_cls, child_obj in child_item.items():
                            child_attrs = dict(child_obj.get("attributes", {}))
                            child_attrs["_class"] = child_cls
                            children.append(child_attrs)
                    attrs["_children"] = children
                objects.append(attrs)
        return objects

    @staticmethod
    def _reported_total(body: dict[str, Any]) -> int | None:
        """The APIC's totalCount as an int, or None when it did not say.

        None is a real answer, distinct from 0: the fetch_all loop must know
        whether the size of the set was reported or merely guessed, because a
        guess equal to "what I have so far" would end the walk after one page.
        """
        try:
            return int(body["totalCount"])
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _parse_total_available(body: dict[str, Any], fallback: int) -> int:
        """Coerce the APIC-reported totalCount, defensively.

        A missing or non-numeric totalCount falls back to `fallback` rather
        than raising.  Shared by query_class() (where `fallback` is the number
        of objects parsed from the page) and count_class() (where it is the
        number of objects in imdata), so both report the size of a result set
        from the same field and can never disagree about it.
        """
        reported = ApicClient._reported_total(body)
        return fallback if reported is None else reported

    async def query_class(
        self,
        class_name: str,
        filters: dict[str, str],
        scope_dn: str = "",
        limit: int = 20,
        order_by: str = "",
        include_children: list[str] | None = None,
        filter_expr: str | None = None,
        rsp_subtree_include: str | None = None,
        time_range: str | None = None,
        page: int | None = None,
        config_only: bool = False,
        fetch_all: bool = False,
    ) -> QueryResult:
        """Query ACI objects by class, optionally scoped to a subtree DN.

        When `scope_dn` is provided the request targets the APIC subtree
        endpoint (`/api/mo/{scope_dn}.json?query-target=subtree`) which is
        more efficient than a fabric-wide class scan for large deployments.

        When `include_children` is provided, the APIC `rsp-subtree=children`
        parameter is added so each returned object embeds its direct children
        of the listed classes as `_children` — equivalent to
        `moquery -x rsp-subtree=children -x rsp-subtree-class=X,Y`.

        The APIC filter string is built internally from `filters` via
        registry.filter.build_filter() — callers pass plain dicts.

        Args:
            class_name:          ACI class to query, e.g. "fvBD".
            filters:             Attribute equality filters {attr: value}.
            scope_dn:            Optional parent DN to scope the query.
            limit:               Maximum objects to return per page (APIC
                                 page-size). Has no effect when fetch_all=True,
                                 which pages at _FETCH_ALL_PAGE_SIZE whatever
                                 this is.
            order_by:            Optional ordering expression.
            include_children:    Child class names to embed via rsp-subtree=children.
            filter_expr:         Raw APIC filter string for complex predicates,
                                 e.g. 'wcard(fvBD.dn,"uni/tn-OT")' or
                                 'and(ne(fabricNode.role,"controller"),...)'.
                                 Combined with `filters` via and() when both set.
            rsp_subtree_include: Subtree categories to include, e.g. "faults",
                                 "health", "audit-logs", "faults,no-scoped".
            time_range:          Time range for log record classes, e.g. "24h",
                                 "1week", "2024-01-01|2024-01-31".
            page:                Page number for explicit manual pagination
                                 (0-based). Ignored when fetch_all=True, which
                                 always walks pages from 0.
            config_only:         When True, add rsp-prop-include=config-only so the
                                 APIC returns only user-configurable attributes and
                                 drops operational/internal noise (~40 attrs → the
                                 handful that define the object's intended config).
            fetch_all:           When True, loop pages (0, 1, 2, ...) at a fixed
                                 page size of _FETCH_ALL_PAGE_SIZE (200) —
                                 independent of `limit` — accumulating every
                                 matching object into one QueryResult instead of
                                 returning just the first page. Stops, in order
                                 of precedence, when:
                                   1. the distinct objects in hand reach
                                      the APIC's totalCount → complete=True
                                      (this includes a set that is an exact
                                      multiple of the page size, with no
                                      extra empty request);
                                   2. a page comes back short or empty — the
                                      natural end → complete=True when no
                                      totalCount was reported, but complete=
                                      False when one was and has not been
                                      reached (the APIC ran dry early; the
                                      loop never spins on empty pages);
                                   3. _MAX_PAGES pages (= _MAX_OBJECTS objects)
                                      have been fetched → complete=False.
                                 At most _MAX_OBJECTS objects are ever fetched
                                 or returned; none past the cap are trimmed
                                 after the fact, because the cap lands on a
                                 page boundary. `page` is ignored.
                                 An object that arrives twice — the set shifted
                                 between two pages — is kept once, by `dn`
                                 (first copy wins; an object with no `dn` is
                                 always kept), and counted once toward
                                 totalCount, so a repeat can never stand in for
                                 an object the walk missed.

        Returns:
            A QueryResult carrying:
              objects:         Attribute dicts with "_class" key (and
                               "_children" when include_children is set).
              total_available: The APIC-reported totalCount — the true number
                               of matching objects, regardless of how many
                               were actually fetched.
              complete:        False only when fetch_all=True stopped before
                               holding every object the APIC reported — at
                               the _MAX_OBJECTS cap, or on an early empty
                               page. True otherwise (including every call
                               without fetch_all, where it says nothing about
                               the result set).

        Raises:
            ApicAuthError:     Both the initial request and the re-auth retry
                               were rejected with 401/403.
            ApicConnectionError: The APIC host is still unreachable, or the
                               request still timed out, after exhausting the
                               retry budget (see _TRANSIENT_STATUSES).
            ApicRequestError:  APIC returned a non-2xx, non-auth status that
                               persisted across the retry budget — e.g. 400
                               for a malformed filter_expr (never retried,
                               since it is a permanent error), a 3xx for a
                               filter value invalid for its attribute (never
                               followed, never read as data), or a transient
                               500/502/503/504/404 that never recovered — or
                               a 2xx whose imdata holds an `error` object.
                               Carries the HTTP status and, when present, the
                               APIC's error code and text. On any page of a
                               fetch_all walk it ends the walk: no partial
                               QueryResult is returned.
            ApicResponseError: The response body is not valid JSON, or is
                               missing the expected 'imdata' key.
        """
        # A fetch_all walk pages at its own fixed size, whatever `limit` the
        # caller passed — see _FETCH_ALL_PAGE_SIZE for why.
        url, base_params = self._build_query_params(
            class_name, filters, scope_dn,
            _FETCH_ALL_PAGE_SIZE if fetch_all else limit,
            order_by, include_children,
            filter_expr, rsp_subtree_include, time_range, config_only,
        )

        if not fetch_all:
            params = dict(base_params)
            if page is not None:
                params["page"] = str(page)
            logger.debug("GET %s params=%s", url, params)
            body = await self._request_json(url, params)
            objects = self._parse_query_objects(body, include_children)
            total_available = self._parse_total_available(body, len(objects))
            logger.debug("query_class(%s) → %d objects", class_name, len(objects))
            return QueryResult(objects=objects, total_available=total_available)

        objects: list[dict[str, Any]] = []
        seen_dns: set[str] = set()
        duplicates = 0
        reported_total: int | None = None
        complete = False
        for page_num in range(_MAX_PAGES):
            params = dict(base_params)
            params["page"] = str(page_num)
            logger.debug("GET %s params=%s", url, params)
            body = await self._request_json(url, params)
            page_objects = self._parse_query_objects(body, include_children)

            # The set is not frozen while it is walked. An object created or
            # deleted between two requests shifts every later page by one, so
            # the object at a page boundary arrives twice (and another is
            # skipped). Counting the repeat toward totalCount would let the
            # walk reach the count one object short and call itself complete
            # with a duplicate in the answer, so the first copy is kept and
            # the walk is judged on the objects that are actually distinct. An
            # object with no dn cannot be told apart from another, so it is
            # always kept and counted.
            for obj in page_objects:
                dn = obj.get("dn")
                if dn is None:
                    objects.append(obj)
                elif dn in seen_dns:
                    duplicates += 1
                else:
                    seen_dns.add(dn)
                    objects.append(obj)

            # totalCount is re-read on every page and the latest value wins: a
            # set that grows or shrinks while it is being walked is then
            # reported as the APIC last saw it. A page that omits it keeps the
            # previous figure rather than forgetting it. The consequence is
            # that completeness is a count, not an identity check: if the set
            # shrinks to the number of distinct objects already in hand the
            # walk is complete, because that is every object the APIC says
            # there now is.
            page_total = self._reported_total(body)
            if page_total is not None:
                reported_total = page_total

            # 1. Everything the APIC says matches is in hand. Checked before
            #    the short-page test so a set that is an exact multiple of the
            #    page size ends here, without a further, empty request — and so
            #    is not mistaken for a capped one.
            if reported_total is not None and len(objects) >= reported_total:
                complete = True
                break

            # 2. A short page is the natural end of the set. With no
            #    totalCount to check it against, that is all the evidence there
            #    is, and it is what this loop has always trusted. With one that
            #    has not been reached the APIC has stopped answering before it
            #    has finished, so the result is NOT complete — and we stop
            #    rather than ask for page after empty page.
            #    "Short" is judged on what the APIC sent, not on what was
            #    kept: a full page that happened to repeat an object is not
            #    the end of the set.
            if len(page_objects) < _FETCH_ALL_PAGE_SIZE:
                complete = reported_total is None
                if not complete:
                    logger.warning(
                        "query_class(%s, fetch_all=True): page %d returned %d "
                        "objects but totalCount is %s and only %d are in hand "
                        "— stopping early, result is incomplete",
                        class_name, page_num, len(page_objects),
                        reported_total, len(objects),
                    )
                break
            # 3. Otherwise the page was full: ask for the next, until
            #    _MAX_PAGES. Falling out of the loop is the cap — `complete`
            #    stays False, and `total_available` says how much was left.

        if duplicates:
            logger.warning(
                "query_class(%s, fetch_all=True): dropped %d duplicate "
                "object(s) — the set changed while it was being read",
                class_name, duplicates,
            )

        # Without a totalCount from the APIC the best statement of the set's
        # size is what was fetched, the same fallback every other path uses.
        total_available = (
            reported_total if reported_total is not None else len(objects)
        )

        logger.debug(
            "query_class(%s, fetch_all=True) → %d/%d objects, complete=%s",
            class_name, len(objects), total_available, complete,
        )
        return QueryResult(
            objects=objects, total_available=total_available, complete=complete
        )

    async def _send(self, url: str, params: dict[str, str]) -> httpx.Response:
        """Issue one authenticated GET, handling 401/403 re-auth-and-retry.

        This is the low-level transport step shared by every endpoint via
        _request_json(): it does not interpret the response status beyond
        401/403 (re-authenticate once and retry) — deciding whether any
        other status is a transient condition worth retrying or a permanent
        failure is _request_json()'s job, not this method's, so that the
        retry budget in _request_json() governs the *outer* attempt loop
        while this method's own one-shot re-auth stays a single, independent
        step within each of those attempts.

        Raises:
            ApicConnectionError: Host unreachable or request timed out.
            ApicAuthError:       Still unauthorized (401/403) after re-authenticating.
        """
        try:
            resp = await self._client.get(url, params=params)
        except httpx.TimeoutException as exc:
            raise ApicConnectionError(self._host, f"request timed out: {exc}") from exc
        except httpx.ConnectError as exc:
            raise ApicConnectionError(self._host, str(exc)) from exc

        if resp.status_code in (401, 403):
            logger.warning(
                "APIC returned %d — re-authenticating and retrying", resp.status_code
            )
            await self.authenticate()
            try:
                resp = await self._client.get(url, params=params)
            except httpx.TimeoutException as exc:
                raise ApicConnectionError(
                    self._host, f"request timed out after re-auth: {exc}"
                ) from exc
            except httpx.ConnectError as exc:
                raise ApicConnectionError(self._host, str(exc)) from exc
            if resp.status_code in (401, 403):
                raise ApicAuthError(
                    self._host,
                    resp.status_code,
                    "still unauthorized after re-authentication",
                )

        return resp

    async def _request_json(
        self, url: str, params: dict[str, str]
    ) -> dict[str, Any]:
        """Issue an authenticated GET, retrying transient failures, and return
        the parsed APIC JSON body.

        Shared by query_class(), get_by_dn(), and count_class() — the single
        transport path for every read this client makes.

        Retries up to `self._retry_attempts` total attempts (small exponential
        backoff between them — see _backoff_delay()) when a request fails
        with either a connection-level error (timeout/refused) or a status in
        _TRANSIENT_STATUSES (404/500/502/503/504 — see that constant's
        docstring for why 404 is included here specifically). A genuine
        application-level error — a 400 from a malformed filter_expr, for
        instance — is raised immediately on the first attempt, since retrying
        it would only add latency to a failure that will never succeed. The
        401/403 re-authenticate-and-retry flow (see _send()) is a separate,
        one-shot step nested inside each attempt and is unaffected by this
        retry budget either way.

        A body is returned only when the status is 2xx and `imdata` is a list
        with no `error` object in it — which is what lets every caller parse
        `imdata` as managed objects, and read `totalCount` as a count. The
        APIC refuses a request with an error object, not always with a status
        a client would notice: it answers an invalid filter value with a 301.

        Args:
            url:    Absolute APIC URL to GET.
            params: Query-string parameters.

        Returns:
            The decoded response body — a dict whose "imdata" is a list of
            managed-object items, never of error objects.

        Raises:
            ApicConnectionError: Host unreachable or request timed out, after
                                 exhausting the retry budget.
            ApicAuthError:       Still unauthorized after re-authentication —
                                 never retried beyond _send()'s own one-shot
                                 re-auth, since a bad credential won't fix
                                 itself on a second attempt.
            ApicRequestError:    APIC returned a non-2xx, non-auth status that
                                 was either permanent (e.g. 400, or the 3xx it
                                 uses for an invalid filter value — redirects
                                 are not followed) or transient but never
                                 recovered within the retry budget; or answered
                                 2xx with an `error` object among the imdata
                                 items, alone or mixed with data rows. Carries
                                 the HTTP status and, when present, the APIC's
                                 error code and text.
            ApicResponseError:   Body is not valid JSON, lacks 'imdata', or has
                                 an 'imdata' that is not a list.
        """
        logger.debug("GET %s params=%s", url, params)
        last_exc: ApicConnectionError | None = None

        for attempt in range(1, self._retry_attempts + 1):
            try:
                resp = await self._send(url, params)
            except ApicConnectionError as exc:
                last_exc = exc
                if attempt < self._retry_attempts:
                    logger.warning(
                        "Connection error (attempt %d/%d) — retrying: %s",
                        attempt, self._retry_attempts, exc,
                    )
                    await asyncio.sleep(self._backoff_delay(attempt))
                    continue
                raise

            status = resp.status_code
            if status in _TRANSIENT_STATUSES:
                if attempt < self._retry_attempts:
                    logger.warning(
                        "APIC returned transient status %d (attempt %d/%d) — retrying",
                        status, attempt, self._retry_attempts,
                    )
                    await asyncio.sleep(self._backoff_delay(attempt))
                    continue
                raise _request_error(url, status, _error_body(resp))

            # Anything but 2xx is a failure from here on — 3xx included. It is
            # never parsed as data: the APIC's 301 carries an error object whose
            # totalCount of "1" is what count() used to report.
            if not 200 <= status < 300:
                raise _request_error(url, status, _error_body(resp))

            try:
                body = resp.json()
            except ValueError as exc:
                raise ApicResponseError(url, f"response is not valid JSON: {exc}") from exc

            if not isinstance(body, dict) or "imdata" not in body:
                raise ApicResponseError(url, "response body missing 'imdata' key")
            if not isinstance(body["imdata"], list):
                raise ApicResponseError(url, "response 'imdata' is not a list")

            # A 2xx whose imdata holds an error object is a refusal all the
            # same, and so is one that mixes error objects with data rows: the
            # rows of an answer that also says "no" cannot be trusted as the
            # whole answer, and are dropped rather than returned beside it.
            if errors := _apic_errors(body):
                if len(errors) < len(body["imdata"]):
                    logger.warning(
                        "APIC answered HTTP %d with %d error object(s) among %d "
                        "imdata item(s) — discarding the response",
                        status, len(errors), len(body["imdata"]),
                    )
                raise _request_error(url, status, body)

            return body

        # Unreachable when self._retry_attempts >= 1 (every loop iteration
        # either returns or raises) — satisfies the type checker without
        # papering over a real bug if it somehow is reached.
        raise last_exc or ApicConnectionError(self._host, "retry budget exhausted")

    async def get_by_dn(
        self,
        dn: str,
        config_only: bool = False,
        include_children: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Fetch a single managed object directly by its Distinguished Name.

        Targets the APIC managed-object endpoint (`GET /api/mo/{dn}.json`), the
        fast path when the exact DN is already known — no class scan, no filter.

        Args:
            dn:               Full Distinguished Name of the object, e.g.
                              "uni/tn-OT/BD-servers".
            config_only:      When True, add rsp-prop-include=config-only so only
                              user-configurable attributes are returned.
            include_children: Child class names to embed via rsp-subtree=children;
                              each appears in the returned object's "_children".

        Returns:
            The object's attribute dict with a "_class" key (and "_children" when
            include_children is set), or None when no object exists at that DN —
            the APIC returns an empty imdata list for a missing DN, which the
            caller turns into an explicit not-found message.

        Raises:
            ApicConnectionError / ApicAuthError / ApicRequestError / ApicResponseError on
            or protocol failures (see _request_json).
        """
        url = f"{self._base}/api/mo/{validate_dn(dn)}.json"
        params: dict[str, str] = {}
        if config_only:
            params["rsp-prop-include"] = "config-only"
        if include_children:
            params["rsp-subtree"] = "children"
            params["rsp-subtree-class"] = ",".join(include_children)

        body = await self._request_json(url, params)
        imdata = body.get("imdata", [])
        if not imdata:
            logger.debug("get_by_dn(%s) → not found", dn)
            return None

        for cls, obj in imdata[0].items():
            attrs: dict[str, Any] = dict(obj.get("attributes", {}))
            attrs["_class"] = cls
            # Same rule as query_class: take whatever children came back.
            if "children" in obj:
                children: list[dict[str, Any]] = []
                for child_item in obj["children"]:
                    for child_cls, child_obj in child_item.items():
                        child_attrs = dict(child_obj.get("attributes", {}))
                        child_attrs["_class"] = child_cls
                        children.append(child_attrs)
                attrs["_children"] = children
            return attrs
        return None

    async def count_class(
        self,
        class_name: str,
        filters: dict[str, str],
        scope_dn: str = "",
        filter_expr: str | None = None,
    ) -> int:
        """Count ACI objects of a class without transferring their attributes.

        Issues the same class or subtree query as query_class() with a page size
        of 1, then reads the APIC-reported `totalCount` from the response
        envelope — the true size of the matching set, independent of how many
        objects were actually fetched.  Exactly one object comes back instead of
        none; every other match stays on the APIC, so this is still far cheaper
        than fetching a whole result set just to measure it.  Filtering and
        scoping behave exactly as in query_class().

        This deliberately does NOT use the APIC `rsp-subtree-include=count`
        mechanism, even though that is the obvious idiom for a count and is what
        this method used until the tally it produces was measured against
        reality.  On APIC 6.0(9c) the returned `moCount` disagrees with the
        actual size of the result set, without ever erroring, so a caller
        cannot tell a wrong tally from a right one:

          count("fvBD")                        moCount 203   actual 403
          count("fvTenant")                    moCount  36   actual  48
          count("fvBD", filters={arpFlood:no}) moCount  99   actual 203
          count("faultInst")                   moCount 420   actual 420  (right)
          count("fvBD", scope_dn=<tenant A>)   moCount   0   actual 192
          count("fvBD", scope_dn=<tenant B>)   moCount 128   actual 128  (right)

        The failure is data-dependent, not systematic: sweeping every tenant on
        the lab fabric, 5 of the 28 holding bridge domains reported a scoped
        count of 0 while the subtree really held 1 to 192 of them; the other 23
        were exact.  It is however perfectly deterministic — six consecutive
        calls returned the same wrong 0 for the same tenant, and the same right
        answer for its neighbour — so it cannot be papered over with a retry.

        A wrong-but-plausible tally is bad; the 0 is worse.  Zero is the one
        answer a caller will not question, because it reads as a legitimate
        finding ("this tenant has no bridge domains") rather than as a failed
        lookup, which is exactly the error-as-answer failure mode the tool
        layer works to prevent everywhere else.

        `totalCount` was exact in every case measured, and is already the number
        query_class() reports as `total_available`, so count() and query() can
        no longer disagree about the size of the same result set.

        Measured on an APIC 6.0(9c) simulator; the `moCount` behaviour has not
        been re-confirmed against hardware.  That does not weaken the choice
        here — `totalCount` is exact on both, and is the mechanism this client
        already relies on everywhere else.  The root cause of the `moCount`
        discrepancy was not determined and is not needed: the field is simply
        not used any more.

        Args:
            class_name:  ACI class to count, e.g. "fvBD".
            filters:     Attribute equality filters {attr: value}.
            scope_dn:    Optional parent DN to scope the count to a subtree.
            filter_expr: Raw APIC filter string, combined with `filters` via and().

        Returns:
            The number of matching objects as an int.

        Raises:
            ApicConnectionError / ApicAuthError / ApicRequestError / ApicResponseError on
            or protocol failures (see _request_json).
        """
        params: dict[str, str] = {"page-size": "1"}
        if scope_dn:
            url = f"{self._base}/api/mo/{validate_dn(scope_dn, field='scope_dn')}.json"
            params["query-target"] = "subtree"
            params["target-subtree-class"] = class_name
        else:
            url = f"{self._base}/api/class/{class_name}.json"

        eq_filter = build_filter(class_name, filters)
        if filter_expr and eq_filter:
            params["query-target-filter"] = f"and({filter_expr},{eq_filter})"
        elif filter_expr:
            params["query-target-filter"] = filter_expr
        elif eq_filter:
            params["query-target-filter"] = eq_filter

        body = await self._request_json(url, params)
        count = self._parse_total_available(body, len(body.get("imdata", [])))
        logger.debug("count_class(%s) → %d", class_name, count)
        return count

    async def controller_version(self) -> str | None:
        """The firmware version the APIC controller is running, as it reports it.

        One GET of `firmwareCtrlrRunning` filtered to `type == "controller"`
        (the class also carries switch and plugin firmware), reading
        `version` off the first object returned. The class and both attributes
        are in the shipped catalogue; tests/unit/test_fabric_version.py checks
        that they stay there. A cluster answers with one object per controller,
        and only the first is read: a rolling upgrade can leave controllers on
        different releases, and this reports the one that answered first, not
        a consensus.

        The string is returned exactly as the APIC wrote it, e.g. "apic-6.0(9c)"
        or "6.0(9c)" — interpreting it is niwashi_mcp.apic.version's job, so that
        a format this client has never seen is still reported rather than
        discarded here.

        Goes through the same GET path as every other read, so it re-authenticates
        on 401/403 and retries transient failures like any query.

        Returns:
            The version string, or None when the APIC returned no controller
            firmware object — "nothing reported", which is not an error.

        Raises:
            ApicConnectionError / ApicAuthError / ApicRequestError on transport
            or protocol failures (see _request_json).
            ApicResponseError: an object came back but carries no usable
            `version` attribute — malformed, as opposed to absent.
        """
        url = f"{self._base}/api/class/firmwareCtrlrRunning.json"
        params = {
            "query-target-filter": build_filter(
                "firmwareCtrlrRunning", {"type": "controller"}
            )
        }
        body = await self._request_json(url, params)
        imdata = body["imdata"]
        if not imdata:
            logger.debug("controller_version() → no firmwareCtrlrRunning object")
            return None

        try:
            version = imdata[0]["firmwareCtrlrRunning"]["attributes"]["version"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ApicResponseError(
                url, f"firmwareCtrlrRunning object has no usable version: {exc!r}"
            ) from exc
        if not isinstance(version, str) or not version.strip():
            raise ApicResponseError(
                url, f"firmwareCtrlrRunning version is not a non-empty string: {version!r}"
            )
        logger.debug("controller_version() → %s", version)
        return version.strip()

    async def close(self) -> None:
        """Release the underlying HTTP connection pool."""
        await self._client.aclose()
