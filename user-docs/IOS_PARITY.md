# iOS parity — backend + UX changes for native clients

Tracks the surface area an iOS (or other native) client should
mirror to feel current with the web client after the
"Make The Record Actually Readable" sprint.

Backend identifiers stay stable (`episodes` table, `episode_id`,
`candidate_type='episode'`, internal kind values). The product
noun changes for user-facing strings only — the API contract is
backwards-compatible.

> **Environments.** `dev` (ownchart.dzsec.net) and `main`
> (demo.ownchart.me) share the same API contract; the difference
> is whose data is in the database. Native clients can read the
> `X-OwnChart-Build` response header to know which environment
> they hit.

---

## 1. Vocabulary — "Event" replaces "Episode" in UI

Internal `episodes` table and `episode_id` stay the same. User-
facing strings should read **Event**.

| backend (don't show) | UI (show) |
|---|---|
| Episode candidate | Event candidate |
| Save as Episode | Save as Event |
| Episode page | Event page |
| Episode Intelligence | Event Intelligence |

The web client already swept these strings; iOS strings file
should match.

---

## 2. New Event fields

`GET /api/episodes/{id}` and the list endpoints now return:

```json
{
  "id": "uuid",
  "title": "Planner-derived label",            // immutable backing label
  "display_title": "User-chosen name",         // nullable; UI shows this when set
  "aliases": ["alt name 1", "alt name 2"],     // case-insensitive nicknames
  "summary": "...",
  "kind": "surgery",
  "date_start": "2026-01-01T00:00:00Z",
  "date_end": "2026-01-01T00:00:00Z",
  "primary_fact_id": "uuid",
  "created_by": "user",
  "created_at": "...",
  "payload": {
    "intelligence": { /* saved EI structured_output */ },
    "planner":      { /* anchor + windows + facts */ },
    "follow_up_questions": [ "..." ]
  },
  "members": [ /* fact / source / candidate / conversation linkages */ ]
}
```

Render `display_title || title` everywhere. Aliases are
case-insensitive nicknames the user can type in chat to refer to
this Event.

---

## 3. New + changed endpoints

### Rename + aliases

```
PATCH /api/episodes/{id}
Content-Type: application/json
{
  "display_title": "<user title>",
  "aliases": ["<alias 1>", "<alias 2>"],
  "summary": "...",            // optional
  "kind": "surgery",           // optional
  "significance": "major_procedure",  // applied to primary_fact_id
  "reason": "user rename"      // audit only
}
```

`aliases` is whole-array replace; pass `[]` to clear. Capped at
16, deduped case-insensitively, each ≤128 chars.

Additive alias ops (use when you don't want to read+write the
whole list):

```
POST   /api/episodes/{id}/aliases    { "alias": "..." }   → idempotent
DELETE /api/episodes/{id}/aliases/{alias}                  → idempotent
```

### Save-as-Event with rename in one round-trip

```
POST /api/episodes/from-candidate/{candidate_id}
Content-Type: application/json
{
  "display_title": "<user title>",
  "aliases": ["<alias 1>", "<alias 2>"]
}
```

Body is optional. When provided, the Event is created with the
chosen name immediately — no follow-up PATCH needed.

### Attach a candidate to an existing Event

```
POST /api/episodes/{episode_id}/attach-candidate/{candidate_id}
```

Merges the candidate's planner facts + sources into the target
Event's member set. Idempotent on overlap. The Event's title is
unchanged; the candidate is marked accepted.

### Conversations filtered by Event

```
GET /api/conversations?anchor_fact_id={uuid}
```

Returns conversations whose `scope.anchor_fact_id` matches —
powers the Event page's "Conversations about this Event"
section.

---

## 4. Async Event Intelligence — POLL after POST

This is the biggest change for native clients. EI questions used
to block 60+ seconds; now they return in <1 second and the
assistant message lands in the background.

**Old flow** (still used when `kind != "ask"` or when the message
isn't episode-shaped):

```
POST /api/conversations { first_message, scope: { whole_record } }
→ 60 s wait → 201 with messages: [user, assistant]
```

**New flow** when the message is episode-shaped (contains
surgery/procedure/diagnosis keyword) OR matches a saved Event
alias:

```
POST /api/conversations { first_message, scope: { whole_record } }
→ <1 s 201 with:
  {
    "id": "...",
    "kind": "episode_intelligence",
    "scope": { "type": "whole_record", "status": "running" },
    "messages": [ { "role": "user", "content": "..." } ]
  }
```

Then poll:

```
GET /api/conversations/{id}   every 2.5 s
```

Stop polling when:
- `messages` contains an `assistant` message with non-empty `content`, OR
- `scope.status === "failed"` (background hit an error; the
  assistant message will explain), OR
- 5 minutes elapsed (give up; surface a graceful timeout error).

While polling, show "OwnChart is reading the record …" + one-line
explainer:

> "Pulling the procedure, anesthesia notes, discharge instructions,
> and the wearable windows around the date. Usually 30–60 seconds."

Server stamps `scope.anchor_fact_id` when the planner finishes
resolving the anchor — clients can use that to navigate to the
Event once the user has saved.

---

## 5. Event Intelligence response shape

The structured_output an EI assistant message carries now has 13
fields. Keys to render:

```json
{
  "short_answer":              "…3-5 sentences, lead answer first, honors user-requested length",
  "anchor_acknowledgment":     "Anchored to your <…> — high confidence, …",
  "what_happened":             "…",
  "what_they_did":             "…",
  "meds_found":                "…",
  "meds_missing":              "…",
  "body_response":             "…sum-based endurance prose…",
  "body_response_observations": [
    { "window": "30d_baseline", "observation": "…" },
    { "window": "7d_before",    "observation": "…" },
    { "window": "day_of",       "observation": "…" },
    { "window": "7d_after",     "observation": "…" },
    { "window": "14d_after",    "observation": "…" }
  ],
  "travel_and_life":   "…",
  "interpretation":    "…",
  "evidence_summary":  "…",
  "follow_up_questions": ["…"],
  "citations":         [{ "citation_type": "fact|source|anchor|episode|candidate|event", "subject_id": "uuid", "claim_label": "…", "note": "…", "excerpt": "…" }],
  "safety_response":   null
}
```

The chat message's `content` field is a pre-rendered markdown
narrative — short_answer first, anchor italicized, sections as
markdown headers. Clients may render that markdown directly or
build native chrome from the individual structured fields.

When `safety_response` is non-null, render it alone and skip the
other sections.

---

## 6. Markdown in chat

The web client renders `**bold**`, `__bold__`, `*italic*`,
`_italic_`, `` `code` ``, and paragraph breaks on a blank line.
Native clients should do the same for assistant messages.
Specifically:

- `**section header**` on its own line is the section divider —
  treat as bold + slightly larger.
- `_one-line italic_` at the top is the anchor_acknowledgment.
  Render as a quieter inline note.
- Citations look like `[fact:UUID]` or `[source:UUID — Note Kind]`
  inline. Native clients should transform these into tappable
  chips that open a fact-detail or source-detail sheet.

---

## 7. Event page — 7 human sections

The web client renders the Event detail at `/events/{id}` with
these sections in order. Native Event Detail should match:

1. **Hero** — title (display_title || title), date, kind, alias chips
2. **Rename + Aliases** — single-form editor; sends one PATCH
3. **What happened** — short_answer in a highlighted card, then
   what_happened + what_they_did prose
4. **Why it matters** — interpretation prose
5. **What's connected** — member list (fact / source / candidate /
   conversation rows with member_type eyebrow)
6. **Recovery & body signal** — body_response + meds_found +
   meds_missing + travel_and_life
7. **Open questions** — follow_up_questions as chat-link chips
   (tapping prefills the composer with the question)
8. **Conversations about this Event** — fetched from
   `GET /api/conversations?anchor_fact_id={primary_fact_id}`
9. **Evidence** — evidence_summary prose

Hide sections whose content is empty rather than rendering a
placeholder.

---

## 8. Save / Attach flow from chat

When a chat thread has a pending Event candidate (the existing
`GET /api/conversations/{id}/candidates` endpoint surfaces these),
show two CTAs on the candidate card:

- **Save as new Event** — opens a sheet with two inputs
  (display_title, comma-separated aliases) → POST
  `/api/episodes/from-candidate/{id}` with
  `{display_title, aliases}`.
- **Add to existing Event** — opens a searchable list of the
  user's Events (search by title / display_title / any alias) →
  POST `/api/episodes/{ep_id}/attach-candidate/{cand_id}`.

After save, navigate to `/events/{episode_id}`.

The same dual flow applies to "Save as Dossier" for Topics, but
the Topics attach-candidate endpoint isn't shipped yet — that's
the remaining P1. Iterate when it lands.

---

## 9. Anchor resolution behavior (informational)

The server tries resolvers in this order when the user asks a
free-text EI question:

1. **Explicit fact_id / episode_id** (when passed).
2. **Saved alias** — substring match against any Event's
   `display_title` or `aliases`.
3. **Keyword anchor** (only when the question has no date phrase)
   — matches anatomy/procedure words to fact labels.
4. **Date window** — absolute ("January 5 2024") or relative
   ("10 days ago"); anchors on the most-significant procedure in
   that window. Multiple procedures on the same calendar day
   collapse to ONE event with HIGH confidence.
5. **Fallback** — most-recent major procedure with low confidence.
   The UI should show an orange "low confidence" banner when this
   path fires.

---

## 10. Auto-extraction on FHIR sync

When the user runs `POST /api/connectors/{id}/sync`, any
`clinical_note` or `ccda_xml` attachment that comes back now
schedules an LLM extraction as a background task. The sync
response returns immediately; the structured facts appear within
~30 s of the response.

**Failure visibility.** If the background extraction fails, the
SourceDocument's `raw_metadata` is stamped with:

```json
{
  "extraction_status": "failed",
  "extraction_error": "…short message…",
  "extraction_failed_at": "ISO-8601"
}
```

An `AuditEvent(event_type='clinical_note_extract_failed')` is
also written. Native clients should surface a "Retry extraction"
affordance on source detail pages whose `extraction_status` is
`failed`.

EI questions asked before extraction finishes will fall back to
the source-excerpt path; questions asked after will cite the
structured facts directly.

---

## 11. Clinical-note structured facts — new fact_types

The clinical-note extractor (extraction_method
`claude_clinical_note_v1`) produces:

- `condition` — diagnoses (with status: active / resolved /
  history_of / suspected)
- `procedure` — operations + procedural codes (with body_site,
  laterality)
- `medication` — with `coded_concepts.intent` ∈
  `{given_intraop, given_periop, prescribed, home_continued,
  home_held, reviewed_not_taken}`
- `provider_relationship` — clinicians named in the note
- `instruction` — care directives, with `description` carrying
  the applies_to: `activity / restriction / wound_care / diet /
  follow_up / red_flag / monitoring / return_to_work`
- `observation` — vitals + exam findings (BP, HR, SpO2,
  audiometry, etc.)

Existing native fact rendering should still work — these are the
same fact_type strings used elsewhere. The new `instruction`
type may not have native chrome yet; treat like a condition for
fallback rendering.

---

## 12. Open gaps native clients should NOT build UI for yet

These are flagged but not shipped:

- **Discover insights** cluster on the ingest date rather than
  the event date. The `connected_episode` insight type surfaces
  real events but the timestamp is wrong.
- **Timeline year-grain** returns null bucket labels for users
  with sparse pre-2024 data.
- **Topics attach-candidate** endpoint analogous to Events —
  remaining P1.
- **Multi-event compare** ("Compare event A to event B")
  currently anchors one event honestly and bails.
- **Chat-command intent detection** ("save this as an event
  called …") — backend is ready; just needs the chat-side parser.

---

## 13. Quick test checklist

1. Connect any FHIR source → sync → clinical_notes / ccda_xml
   attachments appear. Within ~60 s, an EI question about that
   data cites structured facts (not just source excerpts).
2. Ask an episode-shaped question with a date phrase → 201
   immediately; poll the conversation; assistant message lands
   ~60 s later with anchor confidence "high."
3. Save as Event → enter a display_title and aliases →
   confirm Event detail screen shows both.
4. Ask the question again using one of the aliases → alias
   resolution; anchor explanation should mention the alias.
5. Open the Event page → 7 sections render; "Conversations
   about this Event" shows prior chats grounded on the same
   anchor.
6. Ask about a historical procedure by anatomy alone (no date)
   → keyword anchor fires; resolved to the matching procedure
   fact.
7. Trigger a sync of a source likely to fail extraction → the
   source detail screen surfaces `extraction_status: failed`
   and offers Retry.

If any of these legitimately can't anchor, the server returns
`match_confidence: low` and the UI should show the orange
"low confidence" banner.
