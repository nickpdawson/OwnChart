"""ICS (iCalendar feed) adapter — code-ready design + SSRF guard.

Status: **design-only with the SSRF validator implemented.** Full
parse + sync code is held until a separate PR adds the
``icalendar`` dependency. PM rationale (FU-CAL-ICS-ADAPTER): the
Google skeleton lands first; ICS is the lower-friction "any
calendar with an HTTPS feed URL" path, but introducing the
parsing dep + SSRF threat model warrants a dedicated review.

This module exists so:

  - the adapter_type='ics' value is already wired through the
    storage path (migration 0042, route allowlist),
  - the URL validator (the security-critical piece) is reviewable
    and testable before any network call exists,
  - the worker / route layer can import the contract from one
    place when implementation lands.

Adapter contract (locked):

  Bind:
    ``POST /api/calendar/ics/sources``
      body:
        - ``ics_url``: HTTPS only, validated by ``validate_ics_url``.
          Basic-auth-in-URL is allowed but the FULL url including
          credentials is encrypted at rest in a new column
          ``calendar_sources.ics_url_enc`` (LargeBinary; AES-256-GCM
          via core.crypto, same DEK as OAuth tokens). The public
          /api/calendar/sources list MUST never echo back the
          decrypted URL — the operator who bound it is responsible
          for keeping the URL out of unauthenticated views.
        - ``display_name``: required, 1-256 chars.
        - ``privacy_mode``, ``llm_full_details_consent``,
          ``history_window_back``: same shape as iOS / Google.
      returns: ``CalendarSourceOut`` with adapter_type='ics'.

  Sync:
    Periodic worker ``sync_ics_source(source_id)``:
      1. Decrypt ``ics_url_enc``.
      2. Re-validate the URL (defense in depth — the validator
         might have tightened since the bind happened).
      3. ``httpx.GET`` with:
            - ``follow_redirects=False`` (Step 2 re-validation is
              meaningless if a 302 takes us to a private IP).
            - ``timeout=15``.
            - ``max_redirects=0``.
         Stream the response and reject if it exceeds ``MAX_BYTES``.
      4. Parse with ``icalendar`` (dep added by the implementation
         PR). Filter VEVENTs to the configured history_window_back
         + 365d forward.
      5. Expand recurrences via ``recurring_ical_events`` (or
         hand-rolled DTSTART + RRULE / EXDATE handling — to be
         decided in the impl PR; document the choice there).
      6. Project each expanded instance into the same wire shape
         as ``IOSEventKitEvent`` (function:
         ``ics_vevent_to_wire(vevent: VEvent) -> dict``).
      7. Redact via ``redact_event_for_storage`` and upsert into
         ``calendar_events`` exactly the way the iOS + Google
         workers do. The redactor / projector contracts stay
         adapter-agnostic.
      8. Stamp ``calendar_sources.last_sync_at`` + status per
         FU-CAL-SOURCE-STATUS.

  Disconnect:
    Same as iOS / Google — DELETE on the CalendarSource cascades
    a tombstone over the events. The ``ics_url_enc`` blob stays
    on the row so a resurrection (re-bind under the same URL)
    can reuse the same external_id namespace.

  Schedule:
    ICS feeds are pull-only. The implementation PR adds a cron-style
    arq task (``cron_jobs`` on WorkerSettings) that scans active
    adapter_type='ics' sources hourly and enqueues a sync for each.
    Adaptive backoff on consecutive failures, capped at 24h.

Security boundary (load-bearing):

  - SSRF: every fetch goes through ``validate_ics_url`` which only
    permits https, blocks private + link-local + loopback +
    metadata IPs, and requires the resolved IP at fetch time to
    match the original validation (the impl PR adds a
    ``resolved_ip`` check between validate and fetch to defeat
    DNS rebinding).

  - URL secrets: the URL may contain ``user:pass@`` basic auth.
    Stored encrypted, never logged, never echoed back to the UI.
    The settings page surfaces only ``ics_url_host_redacted``
    (host + path, no creds).

  - Size cap: ``MAX_BYTES`` defaults to 10 MB. A misconfigured
    feed or hostile server can't OOM the worker.

  - Timeout: 15s total per fetch.

  - Content-Type: warn but don't reject — some feeds return
    text/plain. We trust the body parse + length cap instead of
    the header.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import NamedTuple
from urllib.parse import urlsplit, urlunsplit


# Hard size cap for one fetched ICS feed.
MAX_BYTES: int = 10 * 1024 * 1024  # 10 MB

# Total fetch timeout (connect + read).
FETCH_TIMEOUT_SECONDS: float = 15.0


class ICSUrlValidationError(ValueError):
    """Raised when ``validate_ics_url`` rejects a URL. The route
    layer catches and emits HTTP 400 with the reason — never the
    full URL in the response (a malicious operator may have
    pointed it at an internal host whose existence we don't want
    to confirm)."""


class ICSValidatedUrl(NamedTuple):
    """Output of ``validate_ics_url``. Carries the cleaned URL +
    the resolved IP at validation time. The worker re-resolves at
    fetch time and rejects if the resolution drifts to a different
    IP (DNS rebinding defense)."""
    url: str
    host: str
    resolved_ip: str


def validate_ics_url(raw_url: str) -> ICSValidatedUrl:
    """Validate that ``raw_url`` is an https URL pointing at a
    public host. Raises ``ICSUrlValidationError`` on any failure.

    Rules enforced:
      - Scheme must be https. http (cleartext) is rejected; the
        ICS feed may carry sensitive context.
      - Host must resolve to a non-private, non-loopback,
        non-link-local, non-multicast, non-metadata-service IP.
      - The 169.254.169.254 metadata service (AWS / GCP / Azure)
        is explicitly blacklisted even though it's covered by the
        link-local check; pinning it surfaces in tests.
      - userinfo (``user:pass@``) is allowed — stripped from the
        canonical URL for comparison but preserved for the
        encrypted storage value. Callers should encrypt the FULL
        url (with creds) and store ``url=raw_url`` here only for
        the cleaned form.
    """
    if not raw_url or not isinstance(raw_url, str):
        raise ICSUrlValidationError("ics_url_empty")
    try:
        parts = urlsplit(raw_url.strip())
    except ValueError as e:
        raise ICSUrlValidationError(f"ics_url_unparseable: {e}") from None

    if parts.scheme.lower() != "https":
        raise ICSUrlValidationError(
            "ics_url_scheme_not_https: only https feeds are accepted; "
            "http feeds may leak in transit."
        )

    host = parts.hostname
    if not host:
        raise ICSUrlValidationError("ics_url_host_missing")

    # Resolve once. The worker re-resolves at fetch time and
    # compares against this resolved IP to defeat rebinding.
    try:
        resolved = socket.gethostbyname(host)
    except OSError as e:
        raise ICSUrlValidationError(
            f"ics_url_host_unresolvable: {type(e).__name__}"
        ) from None

    try:
        ip = ipaddress.ip_address(resolved)
    except ValueError:
        raise ICSUrlValidationError("ics_url_resolved_ip_unparseable")

    # Reject every category of "not a public destination" the route
    # could be pointed at. Order matters: ``is_private`` in stdlib
    # is a superset of ``is_loopback`` and ``is_link_local``, so we
    # check the most specific category first to surface useful
    # error messages.
    if ip.is_loopback:
        raise ICSUrlValidationError("ics_url_resolves_to_loopback")
    if ip.is_link_local:
        raise ICSUrlValidationError("ics_url_resolves_to_link_local")
    if ip.is_multicast:
        raise ICSUrlValidationError("ics_url_resolves_to_multicast")
    if ip.is_reserved:
        raise ICSUrlValidationError("ics_url_resolves_to_reserved")
    if ip.is_unspecified:
        raise ICSUrlValidationError("ics_url_resolves_to_unspecified")
    if ip.is_private:
        raise ICSUrlValidationError("ics_url_resolves_to_private_range")
    # Belt-and-suspenders: pin the cloud-metadata IPs even though
    # link_local already covers them. A future ip_address upgrade
    # that misses the bound surfaces in tests.
    if str(ip) in {"169.254.169.254", "fd00:ec2::254"}:
        raise ICSUrlValidationError("ics_url_resolves_to_metadata_service")

    # Canonical form for storage / display: scheme://host[:port]/path
    # WITHOUT the userinfo. Callers encrypt the original raw_url
    # for actual fetches.
    cleaned = urlunsplit((
        "https",
        parts.hostname + (f":{parts.port}" if parts.port else ""),
        parts.path or "/",
        parts.query,
        "",  # fragment never useful for ICS
    ))
    return ICSValidatedUrl(url=cleaned, host=host, resolved_ip=str(ip))


# ---------------------------------------------------------------------------
# Parse + sync stubs — to be implemented in the follow-up PR that
# adds the ``icalendar`` dependency.


def ics_vevent_to_wire(vevent) -> dict:  # noqa: ANN001 - VEvent is dep-only
    """Project an icalendar VEVENT into the IOSEventKitEvent wire
    shape. Implementation lands with the dep add."""
    raise NotImplementedError(
        "ics_vevent_to_wire is design-only; impl PR adds the "
        "icalendar dependency and the actual projection."
    )


async def fetch_and_parse_ics(url: str) -> list[dict]:
    """Fetch + parse an ICS feed. Returns a list of wire-shaped
    event dicts ready for ``redact_event_for_storage``.
    Implementation lands with the dep add."""
    raise NotImplementedError(
        "fetch_and_parse_ics is design-only; impl PR wires the "
        "httpx fetch + icalendar parse path."
    )
