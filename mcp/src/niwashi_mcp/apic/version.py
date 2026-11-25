# Copyright (C) 2026 Khalid El-Ouiali — MONARK AIOPS srl
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""
apic/version.py

Compare the APIC release the object-model catalogue describes with the release
of the fabric the server is actually connected to, and say so at startup when
they differ.

Why this exists. The catalogue travels inside the niwaki dependency and is built
from one APIC release; the operator's fabric can be any release. Until this
check the startup log named only the catalogue's release, which says nothing
about the fabric, so an operator on another train had no way to learn that a
class or attribute might be missing or wrong until a query came back empty.
The failure is quiet in both directions:

  * a class or attribute the fabric has and the catalogue lacks is refused as
    unknown by this server before the APIC is ever asked;
  * one the catalogue has and the fabric lacks is sent, and the APIC answers
    with an empty result that reads as "there are none".

The check is advisory. It never blocks startup — a fabric on another release is
a supported, if narrower, way to run (see SUPPORTED-APIC.md) — and a lookup that
fails or an answer it cannot read is reported as unknown, never as a mismatch.
Claiming a mismatch on a string this module merely failed to parse would be the
same kind of wrong answer the warning exists to prevent.
"""

import logging
import re
from typing import Protocol

logger = logging.getLogger("niwashi-mcp.apic")

_SUPPORTED_APIC_URL = (
    "https://github.com/k3l0-dev/niwashi-mcp/blob/main/SUPPORTED-APIC.md"
)

# A release string as Cisco writes it: major.minor(maintenance+patch), e.g.
# "6.0(9c)", with an optional lowercase product prefix the controller puts in
# front of it ("apic-6.0(9c)"; a switch reports "n9000-16.0(9c)"). Engineering
# and special builds carry dotted maintenance parts, "6.0(3.82a)", hence the
# repeated group. Matched against the lower-cased, stripped string, so case and
# surrounding whitespace never decide a comparison.
_RELEASE = re.compile(
    r"^(?:[a-z][a-z0-9_]*-)*(\d+)\.(\d+)\(([0-9a-z]+(?:\.[0-9a-z]+)*)\)$"
)


def normalise_apic_version(raw: object) -> str | None:
    """Canonical `major.minor(maint)` form of an APIC release string, or None.

    "6.0(9c)", " APIC-6.0(9C) " and "apic-6.0(9c)" all become "6.0(9c)". None
    means the string is not one this function understands — a different format,
    an empty value, a non-string — and callers must treat that as *unknown*, not
    as a different release.
    """
    if not isinstance(raw, str):
        return None
    match = _RELEASE.match(raw.strip().lower())
    if match is None:
        return None
    major, minor, maintenance = match.groups()
    return f"{major}.{minor}({maintenance})"


class _VersionSource(Protocol):
    async def controller_version(self) -> str | None: ...


async def check_fabric_version(
    backend: _VersionSource, catalogue_version: str
) -> str | None:
    """Read the fabric's APIC release, compare it with the catalogue's, and log.

    Never raises: a startup that dies because it could not read a version string
    would trade a quiet hazard for a loud outage on a fabric that is, in every
    other respect, working. Whatever goes wrong is logged as a warning and the
    result is None.

    Args:
        backend:           Anything with `async controller_version()` — the
                           ApicClient, already authenticated.
        catalogue_version: The release the catalogue was built from
                           (`registry.catalog.apic_version()`).

    Returns:
        The fabric's release in canonical form ("6.0(9c)"), or None when it
        could not be read or understood. Equal-or-not to the catalogue's is
        logged here, not returned: the caller stores the answer, it does not
        need to re-derive the verdict.
    """
    try:
        reported = await backend.controller_version()
    except Exception as exc:  # deliberate: nothing here may fail startup
        logger.warning(
            "Could not read the fabric's APIC version (%s: %s). The server "
            "cannot tell whether the fabric runs the release its object-model "
            "catalogue describes (APIC %s); classes or attributes that differ "
            "between releases may be refused as unknown or come back empty. "
            "See %s",
            type(exc).__name__, exc, catalogue_version, _SUPPORTED_APIC_URL,
        )
        return None

    if reported is None:
        logger.warning(
            "The APIC reported no controller firmware version, so the fabric's "
            "release is unknown. The object-model catalogue describes APIC %s; "
            "if the fabric runs another release, classes or attributes that "
            "differ may be refused as unknown or come back empty. See %s",
            catalogue_version, _SUPPORTED_APIC_URL,
        )
        return None

    fabric = normalise_apic_version(reported)
    if fabric is None:
        logger.warning(
            "The APIC reported its version as %r, which is not a release string "
            "this server can read, so the fabric's release is unknown. The "
            "object-model catalogue describes APIC %s; if the fabric runs "
            "another release, classes or attributes that differ may be refused "
            "as unknown or come back empty. See %s",
            reported, catalogue_version, _SUPPORTED_APIC_URL,
        )
        return None

    catalogue = normalise_apic_version(catalogue_version)
    if catalogue is None:
        logger.warning(
            "The fabric runs APIC %s but the catalogue's release (%r) is not "
            "one this server can read, so the two cannot be compared.",
            fabric, catalogue_version,
        )
        return fabric

    if fabric == catalogue:
        logger.info("Fabric runs APIC %s, matching the object-model catalogue", fabric)
    else:
        logger.warning(
            "Fabric runs APIC %s but the object-model catalogue describes APIC "
            "%s. Classes and attributes added, removed or renamed between the "
            "two releases may be refused as unknown (the fabric has them, the "
            "catalogue does not) or come back empty (the catalogue has them, "
            "the fabric does not) — and an empty result reads as \"there are "
            "none\". See %s",
            fabric, catalogue, _SUPPORTED_APIC_URL,
        )
    return fabric
