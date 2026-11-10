"""Prompt registry.

Prompts are YAML files in `ownchart/prompts/`. They are the editable surface
for all LLM behavior — never hardcode prompt strings in Python.

YAML schema:

    id: dossier_brief
    version: 1
    model: claude-opus-4-7
    purpose: dossier_brief
    system: |
      You are a research partner. Cite every fact. Never give treatment
      instructions. ...
    user_template: |
      Topic: {topic_name}
      Facts:
      {facts_excerpt}
    tools:                  # optional Anthropic tool-use definitions
      - name: emit_brief
        description: ...
        input_schema: {...}

`{var}` placeholders in `user_template` are filled with `Prompt.render(...)`.
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class PromptError(RuntimeError):
    pass


SHARED_CONTRACT_TOKEN = "{{shared_contract}}"


class Prompt(BaseModel):
    id: str
    version: int
    model: str
    purpose: str
    system: str
    user_template: str = ""
    tools: list[dict[str, Any]] = Field(default_factory=list)
    # Receipt fields (audit 2026-11-10, prompts section): PHILOSOPHY.md
    # promises a prompt file hash on every model run, but ModelRun only
    # carried `id@version`. The registry fills these from the file bytes
    # so a receipt can prove which exact text was sent.
    source_file: str | None = None
    content_sha256: str | None = None

    @property
    def version_tag(self) -> str:
        return f"{self.id}@{self.version}"

    def render(self, **vars: Any) -> str:
        try:
            return self.user_template.format(**vars)
        except KeyError as e:
            raise PromptError(
                f"Prompt {self.version_tag} missing required variable: {e}"
            ) from e


class PromptRegistry:
    def __init__(self, prompts_dir: Path) -> None:
        self.prompts_dir = prompts_dir
        self._prompts: dict[str, Prompt] = {}
        self._load()

    def _shared_contract(self) -> str:
        """Highest-versioned `shared/contract.v<N>.md`, or "" when absent.

        Prompts opt in with the literal token `{{shared_contract}}` in
        their `system` text (audit 2026-11-10, item 12: one short shared
        contract for ownership, corrections, evidence classes, scope and
        untrusted content; task prompts compose from it).
        """
        shared = self.prompts_dir / "shared"
        best: tuple[int, Path] | None = None
        if shared.exists():
            for p in shared.glob("contract.v*.md"):
                try:
                    n = int(p.stem.split(".v", 1)[1])
                except (IndexError, ValueError):
                    continue
                if best is None or n > best[0]:
                    best = (n, p)
        return best[1].read_text(encoding="utf-8").strip() if best else ""

    def _load(self) -> None:
        if not self.prompts_dir.exists():
            return
        contract = self._shared_contract()
        for path in sorted(self.prompts_dir.glob("*.yaml")):
            # Skip macOS AppleDouble sidecars (`._foo.yaml`) and any dotfile
            # that sneaks in via tar from a macOS dev machine — they're not
            # YAML, they're binary resource forks. Also skip Drive-sync
            # conflict siblings like `foo 2.yaml`.
            if path.name.startswith(".") or path.name.startswith("._"):
                continue
            data = path.read_bytes()
            raw = yaml.safe_load(data.decode("utf-8"))
            # Skip non-chat-prompt YAMLs that live in the same dir
            # (e.g. suggested_questions.v1.yaml is a question-template
            # config consumed by llm/suggested_questions.py, not a
            # chat prompt). Chat prompts always declare both `model`
            # and `system`; anything else is a different schema.
            if not isinstance(raw, dict) or "model" not in raw or "system" not in raw:
                continue
            system_text = str(raw.get("system") or "")
            if SHARED_CONTRACT_TOKEN in system_text:
                if not contract:
                    raise PromptError(
                        f"{path.name} includes {SHARED_CONTRACT_TOKEN} but no "
                        "prompts/shared/contract.v*.md exists"
                    )
                system_text = system_text.replace(SHARED_CONTRACT_TOKEN, contract)
            composed = {
                "system": system_text,
                "user_template": raw.get("user_template") or "",
                "tools": raw.get("tools") or [],
                "model": raw.get("model"),
            }
            # Hash the COMPOSED prompt (shared contract already inlined),
            # not the file bytes: the receipt must identify the text that
            # was actually sent.
            composed_hash = hashlib.sha256(
                json.dumps(composed, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()
            try:
                prompt = Prompt.model_validate({
                    **raw,
                    "system": system_text,
                    "source_file": path.name,
                    "content_sha256": composed_hash,
                })
            except Exception as e:  # noqa: BLE001
                raise PromptError(f"Invalid prompt file {path}: {e}") from e
            key = f"{prompt.id}@{prompt.version}"
            if key in self._prompts:
                raise PromptError(f"Duplicate prompt {key} in {path}")
            self._prompts[key] = prompt
            # Expose by bare id = highest numeric version. Filename order
            # ("v10" sorts before "v2") must not decide which prompt is
            # live.
            current = self._prompts.get(prompt.id)
            if current is None or prompt.version > current.version:
                self._prompts[prompt.id] = prompt

    def get(self, id_or_tag: str) -> Prompt:
        if id_or_tag not in self._prompts:
            raise PromptError(f"Unknown prompt: {id_or_tag}")
        return self._prompts[id_or_tag]

    def all(self) -> list[Prompt]:
        return [p for k, p in self._prompts.items() if "@" in k]


@lru_cache(maxsize=1)
def get_registry() -> PromptRegistry:
    here = Path(__file__).resolve().parent.parent / "prompts"
    return PromptRegistry(here)
