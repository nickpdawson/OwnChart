"""Global PHI consent gate.

V1 is single-tenant. Consent is a single boolean stored on the User row.
Every code path that ships PHI to Anthropic MUST call `require_phi_consent`
or the equivalent dependency before composing the request.
"""

from fastapi import HTTPException, status

from ..models.user import User


def require_phi_consent(user: User) -> None:
    if not user.phi_consent_granted:
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail={
                "error": "phi_consent_required",
                "message": (
                    "This action would send PHI to an external LLM provider. "
                    "Enable global LLM consent in settings to proceed."
                ),
            },
        )
