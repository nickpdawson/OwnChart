"""Injection-safe rendering of the file-based email templates.

Per project doctrine, email bodies are externalized files
(``ownchart/email/templates/*.{html,txt}``) — never hard-coded strings —
and referenced by code. This module loads them and substitutes a fixed set
of ``{{placeholder}}`` tokens.

Rendering rules (deliberately minimal — this is not a general template
engine, and must not become one):

  - Only ``{{name}}`` tokens whose ``name`` is a key of the supplied context
    are substituted. Unknown tokens are left verbatim.
  - Substitution is a SINGLE pass via ``re.sub``. A value that itself
    contains ``{{other}}`` is therefore NOT re-expanded — closing the
    template-injection hole where a user-supplied field could smuggle in
    another placeholder.
  - HTML templates escape every substituted value with ``html.escape`` so a
    value like ``<script>`` or ``"`` can't break out of the markup. Text
    templates substitute verbatim (no markup to escape).

The context values come from a mix of operator config (instance name),
system-composed links, and — for invitations — the invitee email captured
at invite-creation time. Escaping the lot keeps even the operator-config
path safe.
"""

from __future__ import annotations

import html
import re
from functools import lru_cache
from pathlib import Path

_TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "email" / "templates"

_TOKEN_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


@lru_cache(maxsize=64)
def _load_template(filename: str) -> str:
    path = _TEMPLATES_DIR / filename
    return path.read_text(encoding="utf-8")


def _render(raw: str, context: dict[str, str], *, escape: bool) -> str:
    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in context:
            return match.group(0)  # leave unknown tokens verbatim
        value = str(context[key])
        return html.escape(value) if escape else value

    return _TOKEN_RE.sub(_sub, raw)


def render_email(name: str, context: dict[str, str]) -> tuple[str, str]:
    """Render the ``{name}.html`` and ``{name}.txt`` templates.

    Returns ``(html_body, text_body)``. Raises ``FileNotFoundError`` if a
    template file is missing (a packaging error, surfaced loudly rather than
    sending a half-broken email).
    """
    html_body = _render(_load_template(f"{name}.html"), context, escape=True)
    text_body = _render(_load_template(f"{name}.txt"), context, escape=False)
    return html_body, text_body
