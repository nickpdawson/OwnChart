"""DELETE /api/connectors/{id-or-slug} — admin-only gate.

Beta 1 patch (PM #214 promotion, 2026-05-24). Tightens the
existing DELETE route from "any caregiver+" to "instance admin
only" because ProviderConnector is instance-global; allowing any
record owner to delete an operator-curated catalog row is too
broad in the multi-tenant posture.

What this pins:
  - Non-admin caller → 403 with detail.code='not_instance_admin'.
  - Admin caller hitting a missing slug → 404.
  - Admin caller hitting a row with an active ProviderConnection →
    409 with detail.code='has_active_connection'.
  - Source-level confirmation that the route only emits slug +
    user_id to logs (no client_id, no tokens, no patient names).

The live-DB happy path (admin + unconnected → 204 + row deleted)
runs in the post-deploy smoke against Maverick; we don't reach
the DB layer here.
"""

from __future__ import annotations

import inspect
import uuid

from ownchart.routes import connectors as connectors_route
from ownchart.tests.conftest import authed_client


def test_delete_connector_403_for_non_admin(app_fixture):
    """Caregiver on their own record but NOT is_instance_admin must
    get 403, not 204. Beta 1 patch — pre-patch the route was open
    to any caregiver+."""
    client = authed_client(
        app_fixture,
        role="owner",  # max non-admin role still on their own record
        is_instance_admin=False,
    )
    r = client.delete(f"/api/connectors/{uuid.uuid4()}")
    assert r.status_code == 403, r.text
    body = r.json()
    assert body["detail"]["code"] == "not_instance_admin"


def test_delete_connector_403_for_caregiver_too(app_fixture):
    """Even a caregiver who'd otherwise pass require_role('caregiver')
    gets 403 — the new gate is admin-only, not role-tier-only."""
    client = authed_client(
        app_fixture,
        role="caregiver",
        is_instance_admin=False,
    )
    r = client.delete(f"/api/connectors/{uuid.uuid4()}")
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "not_instance_admin"


def test_delete_connector_403_message_is_user_facing(app_fixture):
    """The 403 detail.message must be patient-facing copy the UI can
    surface verbatim — not a stack trace or internal code."""
    client = authed_client(app_fixture, is_instance_admin=False)
    r = client.delete(f"/api/connectors/{uuid.uuid4()}")
    msg = r.json()["detail"]["message"]
    assert isinstance(msg, str)
    assert "instance admin" in msg.lower()
    # Belt: no leakage of internals.
    assert "Traceback" not in msg
    assert "ctx.user" not in msg


def test_delete_route_log_call_emits_only_slug_and_user_id():
    """Source-level pin on the `log.info("connector_deleted", ...)`
    line specifically — not the whole function (whose docstring may
    legitimately reference forbidden field names as 'never logged
    here'). The log line is the only audit-trail surface the
    handler writes; PHI / secrets must never appear in its kwargs."""
    import re
    src = inspect.getsource(connectors_route.delete_connector)
    # Capture just the log.info("connector_deleted", ...) call args.
    # The route's logger call is a single statement spanning at most
    # a few lines; regex captures up to its closing paren.
    m = re.search(
        r'log\.info\(\s*"connector_deleted"\s*,([^)]*)\)',
        src,
        re.DOTALL,
    )
    assert m is not None, "log.info('connector_deleted', ...) call not found"
    log_kwargs = m.group(1)
    # Allowed identifiers in the log kwargs.
    for forbidden in (
        "client_id",
        "access_token",
        "refresh_token",
        "patient_display_name",
        "patient_fhir_id",
        "cached_resource_counts",
        "pkce_verifier",
        "pkce_challenge",
    ):
        assert forbidden not in log_kwargs, (
            f"log.info kwargs contain {forbidden!r}; "
            "audit logs must not surface PHI/secrets."
        )


def test_delete_route_409_carries_structured_detail():
    """The pre-existing 409 (route refuses to delete when an active
    connection exists) must now carry a structured detail body so
    the UI can render a specific message. Pre-patch it was a string."""
    src = inspect.getsource(connectors_route.delete_connector)
    assert '"code": "has_active_connection"' in src
    assert "Disconnect" in src


def test_delete_route_response_is_204_no_content():
    """Pre-pinned status code — the success body MUST be empty so
    we don't accidentally start returning the deleted row's
    client_id or anything else after the delete."""
    src = inspect.getsource(connectors_route.delete_connector)
    assert "HTTP_204_NO_CONTENT" in src


def test_delete_route_admin_gate_uses_is_instance_admin():
    """Pin the gate predicate so a future refactor that swaps in
    `ctx.active_role == 'owner'` (which would re-broaden access)
    fails CI."""
    src = inspect.getsource(connectors_route.delete_connector)
    assert "is_instance_admin" in src
    assert "not_instance_admin" in src
