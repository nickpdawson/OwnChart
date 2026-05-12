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

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class PromptError(RuntimeError):
    pass


class Prompt(BaseModel):
    id: str
    version: int
    model: str
    purpose: str
    system: str
    user_template: str = ""
    tools: list[dict[str, Any]] = Field(default_factory=list)

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

    def _load(self) -> None:
        if not self.prompts_dir.exists():
            return
        for path in sorted(self.prompts_dir.glob("*.yaml")):
            with path.open("r", encoding="utf-8") as f:
                raw = yaml.safe_load(f)
            # Skip non-chat-prompt YAMLs that live in the same dir
            # (e.g. suggested_questions.v1.yaml is a question-template
            # config consumed by llm/suggested_questions.py, not a
            # chat prompt). Chat prompts always declare both `model`
            # and `system`; anything else is a different schema.
            if not isinstance(raw, dict) or "model" not in raw or "system" not in raw:
                continue
            try:
                prompt = Prompt.model_validate(raw)
            except Exception as e:  # noqa: BLE001
                raise PromptError(f"Invalid prompt file {path}: {e}") from e
            key = f"{prompt.id}@{prompt.version}"
            if key in self._prompts:
                raise PromptError(f"Duplicate prompt {key} in {path}")
            self._prompts[key] = prompt
            self._prompts[prompt.id] = prompt  # also expose by latest id

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
