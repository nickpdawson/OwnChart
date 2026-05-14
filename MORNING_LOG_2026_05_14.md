# Morning log — 2026-05-14

For Nick to read on wakeup. Everything below is on `dev` on
`ownchart.dzsec.net`, pushed to `origin/dev`.

## TL;DR

1. **You were right about the anesthesia record.** Stanford
   delivered 20 clinical_notes from your 2026-05-01 surgery —
   operative reports, anesthesia notes, discharge instructions —
   all sitting on disk with `has_plaintext=true`. The extractor
   that's supposed to turn them into structured facts has never
   run. **System-wide pattern: 184 clinical_notes across 6 health
   systems → 0 extracted facts.**
2. **Band-aid shipped + verified.** Planner now passes those notes'
   plaintext excerpts directly to the LLM. After refilling credits,
   the canonical question came back with **the surgeon's name
   (Scott Reed Lambert, MD), the facility (Byers Eye Institute), the
   real pre-op diagnosis quoted verbatim, and the perioperative
   meds named (acetaminophen 1000mg, scopolamine patch)** — none
   of which were in structured facts.
3. **Three more UX fixes shipped:** markdown now renders in chat
   (no more literal `**` asterisks), `proxyTimeout` bumped to 240s
   (the silent 504 you'd have seen on long EI runs is gone), and
   the "EPISODE CANDIDATE / Save as Episode" card is now
   "Event candidate / Save as Event" with helper copy that
   surfaces the rename feature.
4. **The real fix is still to come:** build a clinical_note
   extractor that turns the 184 already-fetched files into
   structured facts. The band-aid stays in place until then.

## What hit `dev` overnight (after your 20:55 screenshot)

| commit | what |
|---|---|
| `de1c139` | Web: bump `proxyTimeout` to 240s; inline markdown renderer in chat; Episode→Event in the candidate card |
| `86f30f3` | EI: surface `unparsed_clinical_notes` so the answer stops claiming "missing" — planner ships excerpts; prompt tells LLM to quote them |

Earlier in the same evening:

| commit | what |
|---|---|
| `dd148e7` | Save-as-Event: optional rename + aliases on save; new attach-to-existing endpoint |
| `49d36df` | Named Events: `display_title` + `aliases[]` schema + endpoints; alias-aware anchor resolution; chat auto-routes alias-only Qs to EI |
| `03f791d` | EI prompt: `short_answer` first; endurance via sums/workouts/training gaps; honest meds gap |

## The big finding — clinical_note extraction is system-wide broken

```
 source_system              clinical_notes ingested   facts extracted
 epic:bozeman-health                              97                 0
 epic:johns-hopkins-medicine                      55                 0
 epic:stanford-health-care                        20                 0
 athena:bridger-ortho                              6                 0
 epic:orthovirginia                                4                 0
 epic:uva-health                                   2                 0
```

Every one of those 184 rows has `raw_metadata.has_plaintext=true`
and a real `plaintext_excerpt`. For your strabismus episode
specifically the 20 Stanford notes are: Operative Report (×2),
Operative Note (×2), Anesthesia Procedure Notes (×2), Anesthesia
Preprocedure Evaluation (×2), Anesthesia Postprocedure Evaluation
(×2), H&P (×2), Interval H&P (×2), Patient Instructions (×2),
Discharge Instructions (×2), Progress Notes (×2).

**Real next-piece-of-work:** build / restore the clinical_note
extractor pipeline. It should read the RTF/HTML plaintext,
chunk it, run the existing LLM extractor, and produce
ExtractedFact rows with anchors back to the source document.
Once that runs, the 184 already-fetched files will yield
thousands of structured facts and the excerpt-quoting band-aid
becomes unnecessary.

## Confirmed acceptance result (curl-driven, 2026-05-14 03:33 UTC)

The canonical question — "I had eye surgery about 10 days ago.
Give me 3 sentences on what they did including intraoperative
meds. How did recovery affect endurance training?" — now answers:

> Scott Reed Lambert, MD repaired your eye alignment at Byers
> Eye Institute on May 1 — a bilateral strabismus repair that
> included recession/resection on two horizontal muscles and one
> vertical muscle, plus an adjustable suture, for "CONSECUTIVE
> ESOTROPIA; HISTORY OF STRABISMUS SURGERY; HYPOTROPIA OF LEFT
> EYE" [source:526b5624 — Anesthesia Preprocedure Evaluation].
> The intraoperative anesthesia agents themselves aren't in the
> structured record — `missing_anesthesia_record=true` and there
> are 29 unresolved MedicationRequest rows from that day whose
> drug names didn't resolve during ingest — but the discharge
> note confirms perioperative 1000 mg acetaminophen at 6:50 AM
> and a scopolamine patch behind your left ear for nausea
> [source:36b172cc — Discharge Instructions]. Endurance-wise you
> barely paused: 21 exercise minutes the day of, then 253 minutes
> / 27.4 miles over the next 7 days, with HRV actually rebounding
> (39.8 → 47.0 ms) and resting heart rate dropping to a 56 bpm
> mean — your body absorbed this one well.

Compare to the answer in your 20:55 screenshot ("the anesthesia
record didn't make it into your data"). Everything in this new
short_answer except the wearable totals came from the previously-
invisible RTF notes. The voice is also closer to "smart friend"
than "billing clerk."

## What still needs attention

### High priority — for the next morning of work

1. **Clinical-note extractor pipeline** (the real fix for the 184
   unparsed notes). The band-aid is a one-paragraph excerpt per
   note; the proper fix turns them into structured medication /
   provider / encounter facts so retrieval can find them and
   downstream surfaces (Home, Timeline, Discover) light up.
2. **Same-timestamp dedup in anchor confidence.** The May 1 anchor
   currently resolves as "medium confidence — 4 major procedure
   codes" because 4 CPT codes share `13:25:00`. They're the SAME
   event. Collapsing same-timestamp procedures into one event
   bumps confidence to high.
3. **Async EI route.** Current design holds the HTTP connection
   open for ~60s. Even at 240s timeout this is fragile and
   produces a dead UI. Return 202 + conversation_id immediately,
   run EI in a background task, poll for completion. (See also
   the "processing animation" carry-forward.)

### Medium priority

4. **Episode→Event everywhere else.** Tonight I only renamed the
   chat surface (the one in your screenshot). Same change needed
   in: `web/app/(app)/discover/page.tsx`,
   `web/app/(app)/chat/ChatClient.tsx`,
   `web/app/(app)/sources/[id]/MakeSenseButton.tsx`,
   `web/app/(app)/dashboard/DemoTour.tsx`.
5. **Human-centered Event page** (what happened / why it matters /
   what's connected / recovery / open questions / evidence /
   conversations). Backend exposes what's needed; new
   `/events/[id]` route.
6. **Dossier (Topic) parity with Events.** Topics already have
   slug-based aliases; need the same attach-candidate endpoint
   and "Save as existing Dossier" flow.
7. **Chat-command intent detection.** "save this as an event
   called Trip to Lynchburg 5/22–5/29" should route to the
   `POST /api/episodes/from-candidate/{id}` endpoint with the
   parsed name. Backend is ready; just needs the chat-side
   parser.
8. **Multi-event compare.** "Compare 2026 left eye to my knee
   surgery recovery" — currently resolves one event honestly
   and bails. Needs a two-anchor planner pass.

### Lower priority

9. **Claude auth via OAuth** so users can bring their own
   claude.ai subscription instead of the server-side API key
   (which ran out mid-sprint and took EI down). Saved to memory.
10. **Processing animation** during LLM-blocked waits. EI is
    50–70s of dead UI right now.
11. **Voice tightening pass.** The post-fix answer is much
    better but still occasionally lapses into "your record
    contains" phrasing. One more polish pass once you read a
    few responses.

## Where to look in the morning

- Visit any conversation with an EI response — the markdown
  will now render with proper bold section headers and italics
  for the anchor line. The chat ThreadClient was the only file
  touched; everything else still renders the same.
- Run the canonical question again. Expected: ~60s round-trip,
  short_answer leads with the surgeon's name, "Meds found"
  names the perioperative meds, "Meds missing" is honest about
  the anesthesia gap WITHOUT claiming the record "doesn't
  exist."
- The "Save as Event" button on a fresh EI thread should work
  end-to-end and respect rename. The save-to-existing flow is
  backend-only — no UI yet.
- Discover / Timeline / dashboard still say "Episode." Will
  rename those in batch once you confirm the chat surface
  reads right.

## Things I'm worried about

1. **The 240s proxyTimeout is a band-aid.** Real fix is async EI.
   If a single bad day on Anthropic pushes Opus to 70s + your
   planner takes 10s, you're at 80s for the user wait — fine for
   power users, terrible perceived performance for new ones. The
   processing animation on the UI side helps; the async pattern
   underneath is the real answer.
2. **Web container shows "unhealthy" in `docker ps`** but is
   serving traffic fine. This was already the case in the night-
   before status; not something I introduced. Worth investigating
   the health check definition when there's time.
3. **The clinical_note finding likely extends to ccda_xml too.**
   I checked clinical_note specifically because the screenshot
   pointed there. Worth running the same query for ccda_xml
   (80 rows on Nick's record) — if those also have 0 extracted
   facts, the gap is even bigger.
