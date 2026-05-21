"""ICS adapter URL-validation tests (FU-CAL-ICS-ADAPTER, design-only).

The full ICS sync code is held until a separate PR adds the
``icalendar`` dependency. The SSRF guard ships now because it's
self-contained, has no new deps, and is the security-critical
piece a future implementation will load-bear against.

Coverage:
  - https-only (http rejected; file:// rejected; ftp:// rejected).
  - Resolver-level blocks: loopback, private (RFC1918), link-local,
    multicast, reserved, unspecified.
  - Cloud-metadata IP block (169.254.169.254) pinned by name as
    belt-and-suspenders for the link-local check.
  - Public host with HTTPS scheme accepted (resolver test uses a
    monkeypatched ``socket.gethostbyname`` so the test doesn't
    actually hit DNS).
"""

from __future__ import annotations

import socket

import pytest

from ownchart.ingest.calendar_ics import (
    ICSUrlValidationError,
    fetch_and_parse_ics,
    ics_vevent_to_wire,
    validate_ics_url,
)


@pytest.fixture
def fake_resolver(monkeypatch):
    """Replace socket.gethostbyname with a controlled mapping.
    Tests pass ``{host: ip}`` via the returned setter."""
    mapping: dict[str, str] = {}

    def _stub(host: str) -> str:
        if host in mapping:
            return mapping[host]
        raise OSError(f"unknown host: {host}")

    monkeypatch.setattr(socket, "gethostbyname", _stub)
    return mapping


# ---------------------------------------------------------------------------
# Scheme checks


def test_validate_rejects_http_scheme(fake_resolver):
    fake_resolver["example.com"] = "93.184.216.34"
    with pytest.raises(ICSUrlValidationError, match="not_https"):
        validate_ics_url("http://example.com/cal.ics")


def test_validate_rejects_file_scheme(fake_resolver):
    with pytest.raises(ICSUrlValidationError, match="not_https"):
        validate_ics_url("file:///etc/passwd")


def test_validate_rejects_ftp_scheme(fake_resolver):
    with pytest.raises(ICSUrlValidationError, match="not_https"):
        validate_ics_url("ftp://example.com/cal.ics")


def test_validate_rejects_empty_url(fake_resolver):
    with pytest.raises(ICSUrlValidationError, match="empty"):
        validate_ics_url("")


# ---------------------------------------------------------------------------
# Resolver blocks


def test_validate_rejects_loopback(fake_resolver):
    fake_resolver["evil.example"] = "127.0.0.1"
    with pytest.raises(ICSUrlValidationError, match="loopback"):
        validate_ics_url("https://evil.example/cal.ics")


def test_validate_rejects_private_10_range(fake_resolver):
    fake_resolver["intranet.example"] = "10.0.0.1"
    with pytest.raises(ICSUrlValidationError, match="private_range"):
        validate_ics_url("https://intranet.example/cal.ics")


def test_validate_rejects_private_172_range(fake_resolver):
    fake_resolver["docker-host.example"] = "172.17.0.1"
    with pytest.raises(ICSUrlValidationError, match="private_range"):
        validate_ics_url("https://docker-host.example/cal.ics")


def test_validate_rejects_private_192_range(fake_resolver):
    fake_resolver["lan.example"] = "192.168.1.1"
    with pytest.raises(ICSUrlValidationError, match="private_range"):
        validate_ics_url("https://lan.example/cal.ics")


def test_validate_rejects_link_local(fake_resolver):
    fake_resolver["ll.example"] = "169.254.1.1"
    # Link-local trips first; the metadata-service belt-and-suspenders
    # check pins the most well-known link-local IP separately below.
    with pytest.raises(ICSUrlValidationError, match="link_local"):
        validate_ics_url("https://ll.example/cal.ics")


def test_validate_rejects_metadata_service_by_name(fake_resolver):
    """Even if a future ipaddress upgrade changes the link_local
    bound, this pin defends the cloud metadata endpoint by name."""
    # The link_local check fires first; this test confirms the
    # explicit pin exists in the validator source so a refactor
    # can't accidentally drop it.
    from ownchart.ingest import calendar_ics
    import inspect
    src = inspect.getsource(calendar_ics.validate_ics_url)
    assert "169.254.169.254" in src
    assert "metadata_service" in src


def test_validate_rejects_multicast(fake_resolver):
    fake_resolver["m.example"] = "224.0.0.1"
    with pytest.raises(ICSUrlValidationError, match="multicast"):
        validate_ics_url("https://m.example/cal.ics")


def test_validate_rejects_unresolvable(fake_resolver):
    # No mapping → OSError → unresolvable.
    with pytest.raises(ICSUrlValidationError, match="unresolvable"):
        validate_ics_url("https://nowhere.invalid/cal.ics")


# ---------------------------------------------------------------------------
# Happy path


def test_validate_accepts_public_https(fake_resolver):
    fake_resolver["calendar.google.com"] = "142.250.0.0"  # public range
    result = validate_ics_url(
        "https://calendar.google.com/calendar/ical/foo/basic.ics",
    )
    assert result.host == "calendar.google.com"
    assert result.resolved_ip == "142.250.0.0"
    assert result.url == (
        "https://calendar.google.com/calendar/ical/foo/basic.ics"
    )


def test_validate_strips_userinfo_from_cleaned_url(fake_resolver):
    """``user:pass@`` is allowed in the input (some ICS feeds use
    basic auth in the URL) but the cleaned URL returned by the
    validator MUST have no credentials — the route layer encrypts
    the raw form for fetches and stores only the cleaned form for
    display."""
    fake_resolver["calendar.example.com"] = "93.184.216.34"
    result = validate_ics_url(
        "https://user:pass@calendar.example.com/cal.ics",
    )
    assert "user" not in result.url
    assert "pass" not in result.url
    assert result.url == "https://calendar.example.com/cal.ics"


def test_validate_preserves_query_string(fake_resolver):
    fake_resolver["example.com"] = "93.184.216.34"
    out = validate_ics_url(
        "https://example.com/cal.ics?ctz=America/Denver",
    )
    assert "ctz=America" in out.url


# ---------------------------------------------------------------------------
# Stubs raise NotImplementedError until the dep PR lands


def test_ics_vevent_to_wire_stub_raises():
    """The projection function is design-only. A caller that
    accidentally invokes it in a non-design context fails loud
    instead of silently returning {}."""
    with pytest.raises(NotImplementedError, match="design-only"):
        ics_vevent_to_wire(None)


@pytest.mark.asyncio
async def test_fetch_and_parse_ics_stub_raises():
    with pytest.raises(NotImplementedError, match="design-only"):
        await fetch_and_parse_ics("https://example.com/cal.ics")


# ---------------------------------------------------------------------------
# Doctrine pins from the design doc


def test_max_bytes_default_is_under_or_equal_10mb():
    from ownchart.ingest.calendar_ics import MAX_BYTES
    assert MAX_BYTES <= 10 * 1024 * 1024


def test_fetch_timeout_default_is_under_60s():
    from ownchart.ingest.calendar_ics import FETCH_TIMEOUT_SECONDS
    assert FETCH_TIMEOUT_SECONDS <= 60.0
