# iOS parity — backend + UX changes since 8eb66eb / 5de40e2

This is the surface area an iOS client needs to mirror to feel native
after the "Make The Record Actually Readable" sprint. Backend
identifiers stay stable (episode_id / episodes table / kind values);
the product noun changes user-facing surfaces to **Event**.

> Branches: `dev` runs on ownchart.dzsec.net (Nick's daily). `main`
> is the release branch that deploys to demo.ownchart.me. Both share
> the same API contract — the only difference is whose data is in
> the database. iOS should target `https://api.ownchart.me` (or
> `https://api.ownchart.dzsec.net` for dev) and read the
> `X-OwnChart-Build` header to know which it hit.

---

## 1. Vocabulary — "Event" replaces "Episode" in UI

Internal: `episodes` table, `episode_id`, `candidate_type='episode'`
stay the same.

User-facing strings should read **Event**:

| old (don't show) | new (show) |
|---|---|
| Episode candidate | Event candidate |
| Save as Episode | Save as Event |
| Episode page | Event page |
| Episode Intelligence | Event Intelligence |
| Save this as an episode | Save this as an Event |

The web client already swept these on chat ThreadClient, Discover,
ChatClient, MakeSenseButton, DemoTour, EventPage. iOS strings file
should match.

---

## 2. New Episode/Event fields

`GET /api/episodes/{id}` and the list endpoints now return:

```json
{
  "id": "uuid",
  "title": "Strabismus surgery",          // planner-derived backing label (immutable)
  "display_title": "2026 left eye surgery", // user rename, nullable
  "aliases": ["2026 left eye", "May 1 eye surgery", "Stanford eye surgery"],
  "summary": "...",
  "kind": "surgery",
  "date_start": "2026-05-01T13:25:00Z",
  "date_end": "2026-05-01T13:25:00Z",
  "primary_fact_id": "uuid",
  "created_by": "user",
  "created_at": "...",
  "payload": {
    "intelligence": { /* the saved EI structured_output — short_answer, what_happened, etc. */ },
    "planner": { /* the planner JSON, anchor + windows + facts */ },
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
  "display_title": "2026 left eye surgery",
  "aliases": ["2026 left eye", "May 1 eye surgery"],
  "summary": "...",        // optional
  "kind": "surgery",       // optional
  "significance": "major_procedure", // applies to primary_fact_id
  "reason": "user rename"  // audit only
}
```

Aliases is whole-array replace; pass `[]` to clear. Capped at 16,
deduped case-insensitively, each ≤128 chars.

Additive alias ops (use when you don't want to read+write the whole list):

```
POST   /api/episodes/{id}/aliases    { "alias": "left eye" }    → idempotent
DELETE /api/episodes/{id}/aliases/{alias}                         → idempotent
```

### Save-as-Event with rename in one round-trip

```
POST /api/episodes/from-candidate/{candidate_id}
Content-Type: application/json
{
  "display_title": "2026 left eye surgery",
  "aliases": ["2026 left eye", "Stanford eye surgery"]
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

## 4. Async Episode Intelligence — POLL after POST

This is the biggest change for iOS. EI questions used to block
60+ seconds; now they return in <1s and the assistant message
lands in the background.

**Old flow** (still works for `kind != "ask"` or non-episode-shaped
questions):

```
POST /api/conversations { first_message, scope: { whole_record } }
→ 60s wait → 201 with messages: [user, assistant]
```

**New flow** when the message is episode-shaped (contains
surgery/procedure/diagnosis keyword) OR matches a saved Event
alias:

```
POST /api/conversations { first_message, scope: { whole_record } }
→ <1s 201 with:
  {
    "id": "...",
    "kind": "episode_intelligence",
    "scope": { "type": "whole_record", "status": "running" },
    "messages": [
      { "role": "user", "content": "..." }
    ]
  }
```

iOS must then **poll**:

```
GET /api/conversations/{id}   every 2.5s
```

Stop polling when:
- `messages` contains an `assistant` message with non-empty `content`, OR
- `scope.status` is `"failed"` (background hit an error; the
  assistant message will explain), OR
- 5 minutes elapsed (give up; surface a graceful error).

While polling, show "OwnChart is reading the record …" + one-line
explainer:

> "Pulling the procedure, anesthesia notes, discharge instructions,
> and the wearable windows around the date. Usually 30–60 seconds."

Server stamps `scope.anchor_fact_id` when the planner finishes
resolving the anchor — iOS can use that to navigate to the Event
once the user has saved.

---

## 5. Episode Intelligence response shape

The structured_output an EI assistant message carries now has 13
fields (vs. the previous 9). These are the keys iOS should render:

```json
{
  "short_answer":         "…3-5 sentences, direct answer first, honors user-requested length",
  "anchor_acknowledgment": "Anchored to your May 1, 2026 strabismus surgery — high confidence …",
  "what_happened":        "…",
  "what_they_did":        "…",
  "meds_found":           "…",   // NEW (was anesthesia + perioperative + other)
  "meds_missing":         "…",   // NEW
  "body_response":        "…",   // sum-based endurance prose
  "body_response_observations": [
    { "window": "30d_baseline", "observation": "…" },
    { "window": "7d_before",    "observation": "…" },
    { "window": "day_of",       "observation": "…" },
    { "window": "7d_after",     "observation": "…" },
    { "window": "14d_after",    "observation": "…" }
  ],
  "travel_and_life":      "…",
  "interpretation":       "…",
  "evidence_summary":     "…",   // NEW — names sources the answer leans on
  "follow_up_questions":  ["…", "…", "…"],
  "citations":            [{ "citation_type": "fact|source|anchor|episode|candidate|event", "subject_id": "uuid", "claim_label": "…", "note": "…", "excerpt": "…" }],
  "safety_response":      null  // set only on self-harm intent; show alone if present
}
```

The chat message's `content` field is the rendered narrative —
short_answer first, then anchor italicized, then each section as a
markdown header. iOS may either render that markdown directly or
render each structured field with native chrome.

---

## 6. Markdown in chat

The web client renders `**bold**`, `__bold__`, `*italic*`, `_italic_`,
\`code\`, and paragraph breaks on a blank line. iOS should do the
same for assistant messages. Specifically:
- `**section header**` on its own line is the section divider —
  treat as bold + slightly larger.
- `_one-line italic_` at the top after short_answer is the
  anchor_acknowledgment. Render as a quieter inline note.
- Lines starting with `1.` / `-` are not currently emitted by EI;
  no list rendering required yet.
- Fact citations look like `[fact:UUID]` or `[source:UUID — Note Kind]`
  inline. iOS may transform these to tappable chips that open a
  fact-detail or source-detail sheet.

---

## 7. Event page — 7 human sections

The web client renders the Event detail at `/events/{id}` with
these sections in order. iOS Event Detail screen should match:

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
`/api/conversations/{id}/candidates` endpoint surfaces these), show
two CTAs:

- **Save as new Event** — opens a sheet with two inputs (title,
  comma-separated aliases) → `POST /api/episodes/from-candidate/{id}`
  with `{display_title, aliases}`.
- **Add to existing Event** — opens a list of the user's Events
  (search by title/display_title/alias) → `POST /api/episodes/{ep_id}/attach-candidate/{cand_id}`.

After save, navigate to `/events/{episode_id}`.

The same dual flow applies to "Save as Dossier" for Topics, but the
Topics attach-candidate endpoint isn't shipped yet — that's the
remaining P1 in this sprint. Iterate when it lands.

---

## 9. Anchor resolution behavior (informational)

iOS doesn't need to call this directly, but it changes how
questions get matched and is worth knowing for UX:

When the user asks a free-text EI question, the server tries
resolvers in this order:

1. **Explicit fact_id / episode_id** (when iOS passes one).
2. **Saved alias** — substring match against any Event's
   `display_title` or `aliases`. "How did 2026 left eye affect
   training?" → resolves directly to that Event.
3. **Keyword anchor** (when no date phrase is present) — matches
   anatomy/procedure words to fact labels. "Tell me about my fibula
   surgery" → resolves to the ORIF fact even with no date. Skipped
   when the question contains "10 days ago" / "May 1 2026" / etc.
4. **Date window** — `_parse_relative_window` (absolute date
   "May 1 2026" or relative "10 days ago") then anchors on the
   most-significant procedure/event in that window. Multiple
   procedure rows on the same calendar day collapse to ONE event
   with HIGH confidence (was MEDIUM before).
5. **Fallback** — most-recent major procedure with low confidence.
   This now rarely fires; when it does, the UI shows the orange
   "low confidence" banner.

---

## 10. Auto-extraction on new FHIR sync

iOS doesn't need to do anything here, but it's worth knowing:
when the user runs a sync (`POST /api/connectors/{id}/sync`), any
clinical_note / ccda_xml attachments that come back now schedule
their LLM extraction as a background task. The sync response
returns immediately; the structured facts appear within ~30s of
the response. EI questions asked before extraction finishes will
fall back to the source-excerpt path; questions asked after will
cite the structured facts directly.

---

## 11. Clinical-note structured facts — new fact_types iOS may see

The clinical-note extractor (extraction_method `claude_clinical_note_v1`)
produces:

- `condition` — diagnoses (with status: active / resolved / history_of / suspected)
- `procedure` — operations + procedural codes (with body_site, laterality)
- `medication` — with `coded_concepts.intent` ∈ {given_intraop, given_periop, prescribed, home_continued, home_held, reviewed_not_taken}
- `provider_relationship` — clinicians named in the note
- `instruction` — care directives, with `description` carrying the applies_to: activity, restriction, wound_care, diet, follow_up, red_flag, monitoring, return_to_work
- `observation` — vitals + exam findings (BP, HR, SpO2, audiometry, etc.)

Existing iOS fact rendering should still work — these are the
same fact_type strings used elsewhere. The new `instruction`
type may not have iOS chrome yet; treat like a condition for
fallback rendering.

---

## 12. Open gaps to track

These are flagged but not shipped:

- **Discover insights** cluster on the ingest date (2026-05-09)
  rather than the event date. The "connected_episode" type
  surfaces real events but the timestamp is wrong.
- **Timeline** year-grain returns null bucket labels for users
  with sparse pre-2024 data. Separate fix.
- **Topics attach-candidate** endpoint analogous to Events —
  P1 still open.
- **Multi-event compare** ("Compare 2026 left eye to my knee
  surgery") currently anchors one event honestly and bails.
  Future planner change.
- **Chat-command intent detection** ("save this as an event
  called Trip to Lynchburg 5/22–5/29"). Backend's ready; just
  needs the chat-side parser.

---

## 13. Quick test checklist for iOS

1. Connect Stanford FHIR → sync → see clinical_notes flow in.
   Within ~60s, the May 1 procedure has structured facts cited
   in an EI question.
2. Ask "I had eye surgery about 10 days ago" → 201 immediately;
   poll the conversation; assistant message lands ~60s later
   with anchor confidence "high."
3. Save as Event → enter display_title "2026 left eye surgery"
   + aliases → confirm Event detail screen shows both.
4. Ask "How did 2026 left eye affect my training?" → alias
   resolution; anchor explanation mentions the alias by name.
5. Open the Event page → all 7 sections render; "Conversations
   about this Event" shows both prior chats.
6. Ask "Tell me about my fibula surgery" (or any other
   anatomy-named historical procedure) → keyword anchor fires
   even with no date.
7. Ear/hearing surgery question → resolves to the Hopkins
   tympanoplasty / audiology consult, names the surgeon, draws
   the planned-vs-done distinction honestly.

If any of these fall back to "low confidence — fell back to most
recent procedure," surface the orange banner; that's still the
right UX when EI legitimately can't anchor.
