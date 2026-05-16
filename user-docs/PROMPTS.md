# LLM Prompts and AI Configuration

> Every substantive LLM prompt in OwnChart lives in a versioned YAML
> file under `api/ownchart/prompts/`. Nothing important is
> hardcoded. Operators (and curious patients) can read, diff, and
> edit any prompt before deploying.

## Where prompts live

```
api/ownchart/prompts/
├── ask_query.v1.yaml              # Ask: natural-language question over the record
├── dossier_brief.v1.yaml          # Dossier executive brief
├── dossier_brief.v2.yaml          # newer dossier brief variant
├── dossier_followup.v1.yaml       # follow-up question generation in dossier
├── episode_intelligence.v1.yaml   # Event Intelligence (long-form event analysis)
├── extract_clinical_note.v1.yaml  # Structured fact extraction from clinical notes
├── extract_fax_vision.v1.yaml     # Fax / scanned-PDF vision extraction
├── general_ask.v1.yaml            # General Ask (non-episode-shaped) path
├── home_insight.v1.yaml           # Home-screen "Worth Noticing" insight
├── personal_photo_vision.v1.yaml  # Vaccine cards, lab snapshots, Rx labels
├── relabel_clinical.v1.yaml       # User-driven relabel suggestion
├── source_sensemaking.v1.yaml     # Per-source sensemaking
├── suggest_topic_from_chat.v1.yaml # Topic/Dossier suggestion from a chat
└── suggested_questions.v1.yaml    # Follow-up question generation
```

Each file is plain YAML. A prompt has a stable shape:

```yaml
id: ask_query                # logical name; used in audit
version: 1                   # bump when you change wording
model: claude-opus-4-7       # default model for this prompt (overridable at call time)
purpose: ask_query           # routes the call into the consent gate's purpose channel

system: |
  <system message — the model's instructions / role>

user_template: |
  <user message, with {placeholders} the runtime fills in>

tools:                       # optional — tool-use schemas (Anthropic / OpenAI)
  - name: emit_answer
    description: ...
    input_schema: { ... }
```

The runtime loads these once at api startup and re-loads on container
restart. Editing is straightforward: clone the file to a higher
version (e.g. `ask_query.v2.yaml`), edit the body, set `version: 2`,
restart the api container.

## How to review or edit a prompt

### Reading the live wording

```sh
docker compose -f infra/docker-compose.yml --env-file infra/.env \
  exec api cat /app/ownchart/prompts/ask_query.v1.yaml
```

Or just read the file in the repo — what's deployed is what's
checked in.

### Editing a prompt

In **0.1 alpha**, prompt editing is **file-editable**:

1. Edit `api/ownchart/prompts/<name>.<version>.yaml` in your repo
   clone.
2. If the change is semantic (different instructions, different
   output schema), bump `version:` and rename the file to match.
   Keep the old version on disk for a release or two so old
   `ModelRun` rows still resolve their wording.
3. Redeploy (`docker compose ... up -d --build`).

A **UI-editable** prompt admin surface is on the roadmap. The schema
already supports it (the prompts table is content-addressed by SHA);
the management UI is not in the alpha.

### Adding a new prompt

1. Create `api/ownchart/prompts/<name>.v1.yaml` with the four required
   top-level keys (`id`, `version`, `model`, `purpose`) plus `system`
   and either `user_template` or per-tool schemas.
2. Register the prompt id in the caller (the LLM code in
   `api/ownchart/llm/`). Forks and patches should keep this list
   tight — every prompt is a new line of attack surface for hidden
   behavior.

## Audit: ModelRun records every call

Every LLM call writes a `ModelRun` row that captures, at minimum:

- `provider` (`anthropic`, `openai`, `local`)
- `model` (e.g. `claude-opus-4-7`)
- `prompt_id` and `prompt_version` (and `prompt_sha` for tamper
  evidence)
- `purpose` (which consent-gate channel was used)
- `privacy_mode` at the time of the call
- input source hashes (no PHI in the audit row itself)
- output hash
- token usage and estimated cost
- `safety_refusal` flag (set when the model refused under the safety
  boundary)
- `billed_to` (`deployment_default` or `user_byok`) and
  `billed_credential_id` when BYOK is in use

You can answer "why did OwnChart say this?" by joining the chat
message back to its `ModelRun`:

```sql
SELECT id, prompt_id, prompt_version, model, usage, billed_to
FROM model_runs
ORDER BY created_at DESC
LIMIT 20;
```

There is a UI for this at `/settings/providers/usage` (cost
attribution view, shipped in alpha).

## Versioning expectations

- **Bump `version:` on any change** that could materially alter the
  output. Wording tweaks for clarity that don't change semantics can
  stay at the current version, but err on the side of bumping.
- **Keep old prompt files around** for at least one release after
  introducing a new version. `ModelRun.prompt_version` references the
  old version on historical rows; if the file is gone, "why did
  OwnChart say this in March?" stops being answerable from disk.
- **Don't change `id:`.** That's the stable name the code looks up.

## The safety boundary (prompt-level)

Every prompt that produces user-facing output is required to include,
at minimum, instructions equivalent to:

- Never instruct the user to start, stop, change, or substitute a
  medication.
- Never deliver diagnostic verdicts.
- On self-harm intent, emit only the `safety_response` field and
  redirect to crisis support (988 / regional services).
- Label correlation as correlation, not causation.

The `safety_response` field is hard-coded into the tool schemas so the
model has to opt into emitting it (vs. emitting other fields), which
makes refusals auditable.

If you fork OwnChart, do not strip these instructions. The license
permits forks; the doctrine in [PHILOSOPHY.md](../PHILOSOPHY.md) is
what makes a fork still OwnChart-like.

## Roadmap

- **Prompt-edit UI.** A `/settings/prompts` admin page with diff view,
  version history, and a dry-run "what would this look like on a known
  input?" panel.
- **Per-user prompt overrides.** A patient with a specific voice
  preference (more clinical, more plain-language, etc.) being able to
  bias the same prompt without forking the whole instance.
- **Local-model defaults per prompt.** Some prompts (cheap label
  classification, structured extraction) want a local 7B model; the
  same prompt + same call site already supports this in the registry,
  but the UI to pick model-per-prompt isn't shipped.
