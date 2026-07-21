"""One password policy for every set-password site (P1-9).

Before this, the three set-password sites disagreed: ``register`` (incl. the
first-owner bootstrap and invite-accept) had NO length check at all, while
``change`` and ``reset`` enforced a bare ``>= 8``. None guarded an upper
bound, so a multi-megabyte password could pin a CPU in Argon2.

The policy here is intentionally modest — matching the existing reset floor
rather than jumping to the NIST-800-63B 15-character single-factor bar, which
is a product/PM call, not an engineering one:

  - minimum 8 characters,
  - maximum 256 (an Argon2 CPU-DoS guard),
  - not empty / not whitespace-only.

Failures raise 422 with a stable ``password_policy`` code and the concrete
requirements in ``detail`` so a client can render them. The pure
``password_policy_violations`` is unit-tested without FastAPI.
"""

from __future__ import annotations

from fastapi import HTTPException, status

PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 256


def password_policy_violations(password: str | None) -> list[str]:
    """Return the list of violated-requirement codes ([] means OK). Pure."""
    if password is None or password.strip() == "":
        # Empty / whitespace-only is a single, clearest violation — don't
        # also pile on ``min_length``.
        return ["empty"]
    violations: list[str] = []
    if len(password) < PASSWORD_MIN_LENGTH:
        violations.append("min_length")
    if len(password) > PASSWORD_MAX_LENGTH:
        violations.append("max_length")
    return violations


def password_requirements() -> dict[str, object]:
    """Machine-readable requirement set echoed in the error detail."""
    return {
        "min_length": PASSWORD_MIN_LENGTH,
        "max_length": PASSWORD_MAX_LENGTH,
        "no_whitespace_only": True,
    }


def enforce_password_policy(password: str | None) -> None:
    """Raise 422 ``password_policy`` when ``password`` violates the policy."""
    violations = password_policy_violations(password)
    if not violations:
        return
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={
            "code": "password_policy",
            "message": (
                f"Password must be {PASSWORD_MIN_LENGTH}–{PASSWORD_MAX_LENGTH} "
                "characters and not blank."
            ),
            "requirements": password_requirements(),
            "violations": violations,
        },
    )
