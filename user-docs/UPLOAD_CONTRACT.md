# Upload contract — iOS / web → OwnChart

The contract every native client (iOS, future Android, web SPA) can depend on when uploading binary content. Stable across releases; changes here require a parity-doc update on the iOS side.

## Single-origin

All uploads land on the user's instance origin:
```
POST https://<instance>/api/sources/photo
POST https://<instance>/api/sources/pdf
POST https://<instance>/api/healthkit/sync
POST https://<instance>/api/auto-export/push
```
No `api.*` subdomain. See `IOS_PARITY.md`.

## Body-size limits

| Layer | Limit | Notes |
|---|---|---|
| FastAPI handler | No explicit max (streamed) | Practical ceiling set by the reverse proxy. |
| Native HealthKit sync handler | No explicit max | Samples are JSON; 1k-sample batches are typical. |
| `infra/docker-compose.yml` API container | none | uvicorn default. |
| Reverse proxy (nginx / NPM) | **Operator-configurable** | OwnChart recommends `client_max_body_size 200m;`. |
| Cloudflare Free | 100 MB | Independent of origin. |
| Cloudflare Pro/Business/Enterprise | 200 MB / 500 MB / 500 MB | See cloudflare-docs. |

iPhone HEICs are typically 2–10 MB but burst / Live Photos / videos can exceed 100 MB. **Operators MUST set their reverse proxy to allow 200 MB** before claiming photo-upload support for iOS. See `INSTALL.md` reverse-proxy section.

## Error contract — every failure returns JSON

Every error response from app code has shape:
```json
{ "detail": "<single human-readable string>" }
```
with `Content-Type: application/json`. No plain-text `"Internal Server Error"`. A catch-all handler in `api/ownchart/main.py` guarantees this even for unexpected exceptions.

### HTTP status mapping for `/api/sources/photo`

| Status | `detail` shape | Cause | iOS behavior |
|---|---|---|---|
| 201 Created | (returns `SourceDetail` JSON) | Upload succeeded. | Show in vault; queue for vision if not `batch_import`. |
| 400 Bad Request | "Couldn't read uploaded file: ..." | Read failed mid-stream. | Surface, offer retry. |
| 400 Bad Request | "Empty file" | Zero-byte upload. | Surface, offer pick-again. |
| 401 Unauthorized | "Unauthorized" | Session missing / expired. | **Route to re-pair flow.** Do NOT retry upload. |
| 415 Unsupported Media Type | "Unsupported content-type: ..." | MIME not in allowed list (image/jpeg, image/png, image/heic, image/heif, image/webp). | Surface, suggest JPEG export. |
| 415 Unsupported Media Type | "Image is N bytes — too small ..." | <8 KB photo (thumbnail / icon). | Surface; offer re-pick original. |
| 415 Unsupported Media Type | "Couldn't decode this image. The file may be corrupt or an unsupported variant ..." | PIL decode failed (HEIC depth, burst, or corruption). | Surface; suggest JPEG export. |
| 507 Insufficient Storage | "Couldn't write to evidence vault: ..." | Disk full / permission. | Surface; not retryable from iOS — operator action. |
| 507 Insufficient Storage | "Couldn't process image: OSError" | Disk error during thumbnail generation. | Same as above. |
| 500 Internal Server Error | "Image processing failed: <ExceptionClass>" | Unexpected PIL / image-lib failure. | Surface with timestamp; admin investigation. |
| 500 Internal Server Error | "Couldn't save photo metadata: <ExceptionClass>" | DB commit failed. | Surface with timestamp. |
| 500 Internal Server Error | "Server error: <ExceptionClass>. The exception is logged server-side ..." | Catch-all. | Surface with timestamp; logged server-side. |

**Nearby-context failure is NEVER fatal.** If `attach_nearby_clinical_events` raises after the file + fact have been written, the upload still returns 201; the failure is logged + recorded on `raw_metadata.nearby_clinical_events_error` and `nearby_clinical_events: []` is returned.

### iOS retry / recovery rules

1. **401 → re-pair, not retry.** Auth failures mean the device token is invalid or revoked. Drop the photo back to the user-staged queue and route to the re-pair flow.
2. **No automatic retry on 4xx.** 4xx codes carry user-actionable detail (re-pick, re-export, re-format). Surface the `detail` and let the user decide.
3. **No automatic retry on 5xx without backoff.** A failed 507 means disk full at the operator — retrying in 5 seconds won't help. If the user explicitly taps retry, allow it; never silent-retry.
4. **No retry storm.** A single failure must not enqueue N retries. One automatic attempt → on failure surface immediately.
5. **HEIC fallback.** When a 415-decode error comes back specifically for HEIC, offer a "Try JPEG instead" affordance that re-exports the asset via `PHImageManager` JPEG mode.

## Photo upload payload (multipart/form-data)

| Field | Required | Type | Notes |
|---|---|---|---|
| `file` | yes | binary | image/jpeg, image/png, image/heic, image/heif, image/webp. |
| `caption` | no | string | If supplied, a `life_context_event` fact is created with this label. |
| `event_date` | no | ISO date | When the photo's content occurred (not the upload date). |
| `source_label` | no | string | Provider / hospital / event name. |
| `batch_import` | no | bool | `true` for multi-pick imports — defers Claude vision to an explicit `POST /api/sources/{id}/analyze`. Default `false` (intentional single-photo, auto-vision on). |

## Native HealthKit sync (`POST /api/healthkit/sync`)

Same error-shape contract. Idempotency is **client-driven** — every sample carries a `client_sample_key`; the server applies `ON CONFLICT DO NOTHING` against the partial unique index. Re-uploading the same anchor batch is safe.

See `routes/healthkit_sync.py` for the full body schema.

## Auto Export push (`POST /api/auto-export/push`)

Bearer-token auth via `OWNCHART_AUTO_EXPORT_TOKEN` (not session). 202 return + async worker — see `api/ownchart/workers/auto_export_job.py`. The async worker pre-fetches existing `client_sample_keys` for medication samples and skips duplicates; surfaces `dedup_skipped_count` on the SourceDocument metadata.

## See also

- `IOS_PARITY.md` — single-origin contract.
- `SOURCE_AUTHORITY_DOCTRINE.md` — how ingested sources rank.
- `INSTALL.md` — operator setup including reverse-proxy body-size requirement.
