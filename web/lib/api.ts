import { cookies } from "next/headers";

const INTERNAL_API =
  process.env.OWNCHART_API_INTERNAL_URL || "http://api:8000";

export type Membership = {
  person_record_id: string;
  role: "viewer" | "caregiver" | "owner";
  display_name: string;
  is_self: boolean;
};

export type ActiveRecord = {
  id: string;
  display_name: string;
  role: "viewer" | "caregiver" | "owner";
};

export type Me = {
  id: string;
  email: string;
  phi_consent_granted: boolean;
  // M02 additions (PM Decision Note §1). Optional in the type so
  // pre-M02 backends still parse, but the live backend always
  // sends them now.
  is_instance_admin?: boolean;
  default_person_record_id?: string | null;
  memberships?: Membership[];
  active_record?: ActiveRecord | null;
};

// --- FU-MULTITENANT-ONBOARDING -------------------------------------------

export type InviteStatus = "active" | "accepted" | "revoked" | "expired";
export type InviteTargetKind = "existing_record" | "new_record";
export type InviteRole = "viewer" | "caregiver" | "owner";
export type InviteExpiryPreset = "24h" | "7d" | "30d";

export type Invitation = {
  id: string;
  invited_email: string;
  target_kind: InviteTargetKind;
  target_person_record_id: string | null;
  target_display_name: string | null;
  proposed_record_name: string | null;
  role: InviteRole;
  expires_at: string;
  created_at: string;
  created_by_user_id: string;
  accepted_at: string | null;
  accepted_by_user_id: string | null;
  revoked_at: string | null;
  status: InviteStatus;
};

export type InvitationCreated = Invitation & {
  invite_url: string;
};

export type InvitationPreview = {
  invited_email: string;
  role: InviteRole;
  target_kind: InviteTargetKind;
  target_display_name: string | null;
  proposed_record_name: string | null;
  expires_at: string;
};

export async function listInvitations(): Promise<Invitation[]> {
  const r = await fetch(`${INTERNAL_API}/api/invitations`, {
    headers: await withSessionHeaders(),
    cache: "no-store",
  });
  if (r.status === 401 || r.status === 403) return [];
  if (!r.ok) throw new Error(`listInvitations failed: ${r.status}`);
  return (await r.json()) as Invitation[];
}

export async function getInvitationPreview(
  token: string,
): Promise<{ preview: InvitationPreview | null; error: string | null }> {
  // Server-side fetch from the /invite/[token] page. No auth.
  const r = await fetch(
    `${INTERNAL_API}/api/invitations/preview?token=${encodeURIComponent(token)}`,
    { cache: "no-store" },
  );
  if (r.status === 410) {
    return {
      preview: null,
      error: "This invite is no longer available.",
    };
  }
  if (!r.ok) {
    return {
      preview: null,
      error: `Could not load invite (${r.status}).`,
    };
  }
  return { preview: (await r.json()) as InvitationPreview, error: null };
}

async function withSessionHeaders(): Promise<HeadersInit> {
  const cookieStore = await cookies();
  const token = cookieStore.get("ownchart_session")?.value;
  return token ? { cookie: `ownchart_session=${token}` } : {};
}

export async function getMe(): Promise<Me | null> {
  const r = await fetch(`${INTERNAL_API}/api/auth/me`, {
    headers: await withSessionHeaders(),
    cache: "no-store",
  });
  if (r.status === 401) return null;
  if (!r.ok) throw new Error(`getMe failed: ${r.status}`);
  return (await r.json()) as Me;
}

// Public deployment-level info — used by the AppShell to render the
// demo banner. No auth required.
export type InstanceInfo = {
  demo_mode: boolean;
  public_base_url: string;
  demo_user_email: string | null;
};

export async function getInstanceInfo(): Promise<InstanceInfo | null> {
  try {
    const r = await fetch(`${INTERNAL_API}/api/instance/info`, {
      cache: "no-store",
    });
    if (!r.ok) return null;
    return (await r.json()) as InstanceInfo;
  } catch {
    return null;
  }
}

export type SourceSummary = {
  id: string;
  source_type: string;
  original_filename: string | null;
  source_label: string | null;
  captured_at: string | null;
  user_supplied_event_date: string | null;
  user_supplied_caption: string | null;
};

export async function listSources(): Promise<SourceSummary[]> {
  const r = await fetch(`${INTERNAL_API}/api/sources`, {
    headers: await withSessionHeaders(),
    cache: "no-store",
  });
  if (!r.ok) throw new Error(`listSources failed: ${r.status}`);
  return (await r.json()) as SourceSummary[];
}

export type TopicSummary = {
  id: string;
  name: string;
  slug: string;
  aliases: string[];
  description: string | null;
  // Aliases the backend silently dropped from retrieval because
  // they're <3 chars (would cause seq-scan explosions). UI should
  // warn and suggest renaming. Present only on /api/topics responses
  // shaped by /api/topics/{slug}; older callers may not see this.
  ignored_aliases?: string[];
};

export type FactDateProvenance =
  | "explicit"
  | "encounter_proximate"
  | "issued_approximate"
  | "user_canonical";

export type FactHistoricalStatus = "resolved" | "inactive" | "remission";

export type FactReadout = {
  id: string;
  fact_type: string;
  label: string;
  // docs/07 R5 — patient-readable candidate label; UI prefers this
  // when present, original `label` is the source-of-truth.
  display_label: string | null;
  description: string | null;
  date_start: string | null;
  date_end: string | null;
  date_precision: string | null;
  // Section C Phase 1 — how the date was derived. UI uses this for
  // copy ("from this visit" / "approximate" / "you confirmed this")
  // and Home/Discover filter to 'explicit' to avoid surfacing
  // import-date artifacts as recent events. Optional in the type so
  // pre-Phase-1 backends parse cleanly.
  date_provenance?: FactDateProvenance | null;
  historical_status?: FactHistoricalStatus | null;
  confidence: number | null;
  review_state: string;
  extraction_method: string;
  body_site: string | null;
  laterality: string | null;
  canonical_label: string | null;
  canonical_description: string | null;
  canonical_date_start: string | null;
  source_id: string | null;
  source_page: number | null;
  source_anchor_id: string | null;
  source_anchor_type: string | null;
  source_anchor_excerpt: string | null;
  source_anchor_section_path: string | null;
};

export type AnchorReadout = {
  id: string;
  anchor_type: string;
  page_number: number | null;
  section_path: string | null;
  text_excerpt: string | null;
};

export async function listSourceAnchors(sourceId: string): Promise<AnchorReadout[]> {
  const r = await fetch(`${INTERNAL_API}/api/sources/${sourceId}/anchors`, {
    headers: await withSessionHeaders(),
    cache: "no-store",
  });
  if (!r.ok) throw new Error(`listSourceAnchors failed: ${r.status}`);
  return (await r.json()) as AnchorReadout[];
}

export type FactCluster = {
  cluster_id: string;
  fact_type: string;
  label: string;
  date_start_min: string | null;
  date_start_max: string | null;
  fact_count: number;
  source_count: number;
  needs_review_count: number;
};

export type Dossier = {
  topic: TopicSummary;
  clusters: FactCluster[];
  total_facts: number;
  timeline_facts: FactReadout[];
};

export async function getDossier(slug: string): Promise<Dossier> {
  const r = await fetch(`${INTERNAL_API}/api/topics/${slug}`, {
    headers: await withSessionHeaders(),
    cache: "no-store",
  });
  if (!r.ok) throw new Error(`getDossier(${slug}) failed: ${r.status}`);
  return (await r.json()) as Dossier;
}

export async function listTopics(q?: string): Promise<TopicSummary[]> {
  const params = new URLSearchParams();
  if (q) params.set("q", q);
  const url = `${INTERNAL_API}/api/topics${params.toString() ? `?${params}` : ""}`;
  const r = await fetch(url, {
    headers: await withSessionHeaders(),
    cache: "no-store",
  });
  if (!r.ok) throw new Error(`listTopics failed: ${r.status}`);
  return (await r.json()) as TopicSummary[];
}

export type FactDetail = FactReadout & {
  evidence_anchor_ids: string[];
  canonical_date_end: string | null;
  // Friendly source label resolved from the first evidence anchor.
  // Used by the review inbox lane split (#54) to group provider/
  // contact facts by source for "Defer all from this source".
  source_label: string | null;
  source_type: string | null;
  // docs/07 Priority 1 review reasons.
  why_needs_review_code: string | null;
  why_needs_review_text: string | null;
  review_priority: number | null;
  review_task_type: string | null;
  source_context_only_eligible: boolean;
};

export type SourceReviewSummary = {
  source_id: string;
  total_facts: number;
  needs_review_count: number;
  timeline_relevant_needs_review: number;
  provider_contact_needs_review: number;
  confirmed_count: number;
  deferred_or_resolved_count: number;
  by_fact_type: Record<string, number>;
};

export async function getSourceReviewSummary(
  sourceId: string,
): Promise<SourceReviewSummary> {
  const r = await fetch(
    `${INTERNAL_API}/api/sources/${sourceId}/review-summary`,
    { headers: await withSessionHeaders(), cache: "no-store" },
  );
  if (!r.ok) throw new Error(`getSourceReviewSummary failed: ${r.status}`);
  return (await r.json()) as SourceReviewSummary;
}

// docs/07 R2 — patient-meaningful source page lead. Replaces the
// file-inspector header (filename/MIME/SHA) with what the source
// actually contributed to the record.
export type SourceTopEvent = {
  id: string;
  fact_type: string;
  label: string;
  display_label: string | null;
  date_start: string | null;
  review_state: string;
};

export type SourceDossierLinkage = {
  slug: string;
  name: string;
  fact_count: number;
};

export type SourceContributionSummary = {
  source_id: string;
  source_name: string;
  summary: string;            // one-paragraph patient-readable
  total_facts: number;
  needs_review_count: number;
  fact_type_counts: Record<string, number>;
  date_min: string | null;
  date_max: string | null;
  top_events: SourceTopEvent[];
  dossier_linkages: SourceDossierLinkage[];
};

export async function getSourceContributionSummary(
  sourceId: string,
): Promise<SourceContributionSummary> {
  const r = await fetch(
    `${INTERNAL_API}/api/sources/${sourceId}/contribution-summary`,
    { headers: await withSessionHeaders(), cache: "no-store" },
  );
  if (!r.ok) throw new Error(`getSourceContributionSummary failed: ${r.status}`);
  return (await r.json()) as SourceContributionSummary;
}

// docs/07 R3 — fact context view (sidesheet) types.
export type FactContextSource = {
  source_id: string | null;
  source_name: string | null;
  source_type: string | null;
  source_page: number | null;
};

export type FactContextRelated = {
  id: string;
  label: string;
  display_label: string | null;
  fact_type: string;
  significance: string | null;
  date_start: string | null;
  relation: "same_day_same_source" | "shared_equivalence_key";
};

export type AlsoRecordedBy = {
  fact_id: string;
  source_id: string | null;
  source_name: string | null;
  extraction_method: string;
};

export type FactContextEpisode = {
  kind: string;
  title: string;
  date_start: string | null;
  fact_count: number;
};

export type FactContextDossier = {
  slug: string;
  name: string;
};

export type FactContextAction = {
  kind:
    | "confirm"
    | "edit"
    | "view_source"
    | "ask"
    | "source_only"
    | "open_dossier";
  label: string;
  href: string | null;
};

export type FactContext = {
  id: string;
  fact_type: string;
  label: string;
  display_label: string | null;
  description: string | null;
  date_start: string | null;
  // Section C Phase 1.
  date_provenance?: FactDateProvenance | null;
  historical_status?: FactHistoricalStatus | null;
  review_state: string;
  extraction_method: string;
  confidence: number | null;
  significance: string | null;
  significance_source: string | null;
  what_this_is: string;
  why_needs_review_text: string | null;
  source_context_only_eligible: boolean;
  source: FactContextSource;
  episode: FactContextEpisode | null;
  related_facts: FactContextRelated[];
  also_recorded_by: AlsoRecordedBy[];
  matching_dossiers: FactContextDossier[];
  suggested_actions: FactContextAction[];
};

// Type re-exports for client components. The actual fetch lives
// inline in ReviewClient (importing a runtime function from this
// module would pull `next/headers` into the client bundle).
export type BulkAssertionType = "confirm" | "correct" | "reject" | "annotate";

export type BulkResult = {
  updated: number;
  not_found: string[];
  failed: string[];
  review_state: string | null;
};

export async function listFactsByState(state: string): Promise<FactDetail[]> {
  const r = await fetch(`${INTERNAL_API}/api/facts?review_state=${encodeURIComponent(state)}&limit=200`, {
    headers: await withSessionHeaders(),
    cache: "no-store",
  });
  if (!r.ok) throw new Error(`listFactsByState failed: ${r.status}`);
  return (await r.json()) as FactDetail[];
}

export type SourceDetail = SourceSummary & {
  storage_uri: string;
  hash: string;
  mime_type: string | null;
  acquired_at: string;
  raw_metadata: Record<string, unknown> | null;
  exif_metadata: Record<string, unknown> | null;
  has_gps: boolean;
};

export async function getSourceDetail(id: string): Promise<SourceDetail> {
  const r = await fetch(`${INTERNAL_API}/api/sources/${id}`, {
    headers: await withSessionHeaders(),
    cache: "no-store",
  });
  if (!r.ok) throw new Error(`getSourceDetail(${id}) failed: ${r.status}`);
  return (await r.json()) as SourceDetail;
}

export async function listFactsForSource(
  sourceId: string,
  opts?: { includeSourceOnly?: boolean },
): Promise<FactDetail[]> {
  const params = new URLSearchParams({
    source_id: sourceId,
    limit: "500",
  });
  if (opts?.includeSourceOnly) {
    params.set("include_source_only", "true");
  }
  const r = await fetch(`${INTERNAL_API}/api/facts?${params.toString()}`, {
    headers: await withSessionHeaders(),
    cache: "no-store",
  });
  if (!r.ok) throw new Error(`listFactsForSource failed: ${r.status}`);
  return (await r.json()) as FactDetail[];
}

export type ModelRunReadout = {
  id: string;
  provider: string;
  model: string;
  purpose: string;
  prompt_version: string;
  consent_state: boolean;
  input_source_ids: string[];
  input_hash: string | null;
  output_hash: string | null;
  usage: Record<string, unknown> | null;
  error: string | null;
  created_at: string;
};

export async function listModelRuns(): Promise<ModelRunReadout[]> {
  const r = await fetch(`${INTERNAL_API}/api/audit/model-runs?limit=200`, {
    headers: await withSessionHeaders(),
    cache: "no-store",
  });
  if (!r.ok) throw new Error(`listModelRuns failed: ${r.status}`);
  return (await r.json()) as ModelRunReadout[];
}

export type BriefMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  citations: { fact_id: string; note?: string }[];
  retrieved_fact_count: number | null;
  model_run_id: string | null;
  safety_response: string | null;
  error: string | null;
  created_at: string;
};

export async function getBriefThread(slug: string): Promise<BriefMessage[]> {
  const r = await fetch(`${INTERNAL_API}/api/topics/${slug}/thread`, {
    headers: await withSessionHeaders(),
    cache: "no-store",
  });
  if (!r.ok) throw new Error(`getBriefThread(${slug}) failed: ${r.status}`);
  return (await r.json()) as BriefMessage[];
}

export type DashboardStats = {
  source_count: number;
  facts: {
    total: number;
    confirmed: number;
    needs_review: number;
    deferred: number;
    rejected: number;
  };
  topics: {
    id: string;
    slug: string;
    name: string;
    description: string | null;
    fact_count: number;
  }[];
  recent_sources: {
    id: string;
    source_type: string;
    source_label: string | null;
    original_filename: string | null;
    acquired_at: string;
  }[];
};

export async function getDashboardStats(): Promise<DashboardStats> {
  const r = await fetch(`${INTERNAL_API}/api/dashboard`, {
    headers: await withSessionHeaders(),
    cache: "no-store",
  });
  if (!r.ok) throw new Error(`getDashboardStats failed: ${r.status}`);
  return (await r.json()) as DashboardStats;
}

export type DirectoryEntry = {
  name: string;
  fhir_base: string;
  ehr_vendor: string;
  suggested_slug: string;
  has_client_id: boolean;
};

export type ConnectorSummary = {
  id: string;
  slug: string;
  name: string;
  ehr_vendor: string | null;
  fhir_base: string;
  enabled: boolean;
  has_client_id: boolean;
  connection: {
    id: string;
    status: string;
    expires_at: string | null;
    last_synced_at: string | null;
    patient_display_name: string | null;
    cached_resource_counts: Record<string, number> | null;
  } | null;
};

export async function listConnectors(): Promise<ConnectorSummary[]> {
  const r = await fetch(`${INTERNAL_API}/api/connectors`, {
    headers: await withSessionHeaders(),
    cache: "no-store",
  });
  if (!r.ok) throw new Error(`listConnectors failed: ${r.status}`);
  return (await r.json()) as ConnectorSummary[];
}

// --- Global timeline (docs/06 §99) ----------------------------------------

export type TimelineGrain = "year" | "month" | "week";

export type NarrativeEvent = {
  fact_type: string;
  label: string;
  display_label: string | null;
  source_label: string | null;
};

export type NarrativeImport = {
  source_label: string;
  fact_count: number;
};

// docs/07 + product/design team 2026-05-10: care density ≠ data
// density. care_meaningful = we can name a procedure/condition/
// encounter. data_dense = imports landed but no care signal. quiet
// = nothing to narrate yet.
export type BucketNarrativeKind = "care_meaningful" | "data_dense" | "quiet";

export type BucketNarrative = {
  kind: BucketNarrativeKind;
  summary: string;
  top_events: NarrativeEvent[];
  top_imports: NarrativeImport[];
};

export type TimelineBucket = {
  start: string;
  end: string;
  grain: TimelineGrain;
  clinical: { event_count: number; by_type: Record<string, number> };
  wearable: { fact_count: number; by_metric: Record<string, number> };
  source: { document_count: number; by_type: Record<string, number> };
  narrative: BucketNarrative | null;
};

export type UndatedHistoryItem = {
  fact_id: string;
  fact_type: string;
  label: string;
  display_label: string | null;
  source_label: string | null;
};

export type UndatedHistoryGroup = {
  total: number;
  items: UndatedHistoryItem[];
};

export type TimelineResponse = {
  grain: TimelineGrain;
  range_start: string;
  range_end: string;
  buckets: TimelineBucket[];
  // Section C Phase 1 — facts the FHIR ingest could not pin to an
  // explicit occurrence date (typical: Condition with only a
  // recordedDate). Surfaced as a collapsed group instead of being
  // silently dropped.
  undated_history?: UndatedHistoryGroup;
};

export async function getTimeline(opts?: {
  from?: string;
  to?: string;
  grain?: TimelineGrain;
}): Promise<TimelineResponse> {
  const qs = new URLSearchParams();
  if (opts?.from) qs.set("from", opts.from);
  if (opts?.to) qs.set("to", opts.to);
  if (opts?.grain) qs.set("grain", opts.grain);
  const url = `${INTERNAL_API}/api/timeline${qs.size ? `?${qs}` : ""}`;
  const r = await fetch(url, {
    headers: await withSessionHeaders(),
    cache: "no-store",
  });
  if (!r.ok) throw new Error(`getTimeline failed: ${r.status}`);
  return (await r.json()) as TimelineResponse;
}

// --- Notable events (docs/07 episode-view anchor) -------------------------

export type NotableEvent = {
  id: string;
  label: string;
  display_label: string | null;
  fact_type: string;
  date_start: string;
  source_id: string | null;
  source_label: string | null;
};

export async function getNotableEvents(limit?: number): Promise<NotableEvent[]> {
  const url =
    `${INTERNAL_API}/api/timeline/notable-events` +
    (limit ? `?limit=${limit}` : "");
  const r = await fetch(url, {
    headers: await withSessionHeaders(),
    cache: "no-store",
  });
  if (!r.ok) throw new Error(`getNotableEvents failed: ${r.status}`);
  const data = (await r.json()) as { events: NotableEvent[] };
  return data.events;
}

// --- Discover feed (docs/06 §122) -----------------------------------------

export type SignalStrength = "strong" | "moderate" | "needs_review";

export type DiscoverItem = {
  id: string;
  type: string;
  title: string;
  why_surfaced: string;
  evidence_window: { start: string; end: string } | null;
  signal_strength: SignalStrength;
  suggested_action: string;
  action_href: string | null;
};

export type DiscoverResponse = { items: DiscoverItem[] };

export async function getDiscover(limit?: number): Promise<DiscoverResponse> {
  const url =
    `${INTERNAL_API}/api/discover` + (limit ? `?limit=${limit}` : "");
  const r = await fetch(url, {
    headers: await withSessionHeaders(),
    cache: "no-store",
  });
  if (!r.ok) throw new Error(`getDiscover failed: ${r.status}`);
  return (await r.json()) as DiscoverResponse;
}

// --- Period drill ---------------------------------------------------------

export type PeriodEvent = {
  id: string;
  fact_type: string;
  label: string;
  display_label: string | null;
  date_start: string | null;
  extraction_method: string;
  review_state: string;
  source_id: string | null;
  source_anchor_id: string | null;
};

export type PeriodCluster = {
  cluster_id: string;
  fact_type: string;
  label: string;
  fact_count: number;
  source_count: number;
  needs_review_count: number;
  date_start_min: string | null;
  date_start_max: string | null;
};

export type PeriodSource = {
  id: string;
  source_type: string;
  original_filename: string | null;
  source_label: string | null;
  event_date: string;
};

export type PeriodResponse = {
  range_start: string;
  range_end: string;
  // Clinical lane is rolled-up clusters (the dossier's pattern). 4,000+
  // medication administrations in a year would otherwise drown out the
  // surrounding clinical events.
  clinical_clusters: PeriodCluster[];
  wearable_summary: { fact_count: number; by_metric: Record<string, number> };
  sources: PeriodSource[];
};

export async function getTimelinePeriod(
  start: string,
  end: string,
): Promise<PeriodResponse> {
  const url = `${INTERNAL_API}/api/timeline/period?start=${encodeURIComponent(
    start,
  )}&end=${encodeURIComponent(end)}`;
  const r = await fetch(url, {
    headers: await withSessionHeaders(),
    cache: "no-store",
  });
  if (!r.ok) throw new Error(`getTimelinePeriod failed: ${r.status}`);
  return (await r.json()) as PeriodResponse;
}

// ---------------------------------------------------------------------------
// Settings (docs/09).

export type SettingShape = {
  key: string;
  label: string;
  description: string;
  section: string;
  scope: string;
  storage: string;
  type: "boolean" | "enum" | "integer" | "string";
  default: unknown;
  choices: unknown[];
  ui_writable: boolean;
  file_writable: boolean;
  requires_restart: boolean;
  phi_sensitive: boolean;
  admin_lockable: boolean;
};

export type SettingsRegistry = { settings: SettingShape[] };
export type SettingsEffective = { values: Record<string, unknown> };

export async function getSettingsRegistry(): Promise<SettingsRegistry> {
  const r = await fetch(`${INTERNAL_API}/api/settings/registry`, {
    headers: await withSessionHeaders(),
    cache: "no-store",
  });
  if (!r.ok) throw new Error(`getSettingsRegistry failed: ${r.status}`);
  return (await r.json()) as SettingsRegistry;
}

export async function getEffectiveSettings(): Promise<SettingsEffective> {
  const r = await fetch(`${INTERNAL_API}/api/settings/effective`, {
    headers: await withSessionHeaders(),
    cache: "no-store",
  });
  if (!r.ok) throw new Error(`getEffectiveSettings failed: ${r.status}`);
  return (await r.json()) as SettingsEffective;
}

// ---------------------------------------------------------------------------
// Sensemaking candidates (docs/08).

export type SensemakingCandidate = {
  id: string;
  candidate_type: string;
  title: string | null;
  summary_text: string | null;
  payload: Record<string, unknown>;
  claim_label: string | null;
  confidence: number | null;
  source_ids: string[];
  fact_ids: string[];
  evidence_anchor_ids: string[];
  disposition: string;
  disposition_at: string | null;
  user_edit: string | null;
  created_at: string;
};

export type SensemakingJob = {
  id: string;
  job_type: string;
  status: string;
  privacy_mode: string;
  scope: Record<string, unknown>;
  model_run_id: string | null;
  started_at: string | null;
  completed_at: string | null;
  error: string | null;
  candidates: SensemakingCandidate[];
};

export async function listSourceCandidates(
  sourceId: string,
): Promise<SensemakingCandidate[]> {
  const r = await fetch(
    `${INTERNAL_API}/api/sources/${encodeURIComponent(sourceId)}/candidates`,
    {
      headers: await withSessionHeaders(),
      cache: "no-store",
    },
  );
  if (!r.ok) throw new Error(`listSourceCandidates failed: ${r.status}`);
  return (await r.json()) as SensemakingCandidate[];
}

// Significance taxonomy (2026-05-11). Lower index = louder in the UI.
export const SIGNIFICANCE_CHOICES = [
  "major_event",
  "major_diagnosis",
  "major_procedure",
  "major_medication",
  "major_activity_lifestyle",
  "background",
  "source_only",
] as const;
export type Significance = (typeof SIGNIFICANCE_CHOICES)[number];

// ---------------------------------------------------------------------------
// Conversations (docs/10).

export type ConvCitation = {
  id: string;
  citation_type: string;
  subject_id: string;
  claim_label: string | null;
  excerpt: string | null;
  note: string | null;
  ordinal: number;
};

export type ConvMessage = {
  id: string;
  role: "user" | "assistant" | "system" | "tool";
  content: string;
  provider: string | null;
  model: string | null;
  prompt_version: string | null;
  privacy_mode: string | null;
  model_run_id: string | null;
  structured_output: Record<string, unknown> | null;
  usage: Record<string, unknown> | null;
  citations: ConvCitation[];
  created_at: string;
};

export type ConvSummary = {
  id: string;
  title: string | null;
  kind: string;
  scope: Record<string, unknown>;
  provider: string | null;
  model: string | null;
  privacy_mode: string | null;
  starred: boolean;
  archived: boolean;
  last_message_at: string | null;
  created_at: string;
};

export type ConvDetail = ConvSummary & { messages: ConvMessage[] };

export type ProviderShape = {
  key: string;
  label: string;
  configured: boolean;
  capabilities: Record<string, unknown>;
  // Present on /api/llm-providers (the BYOK catalog). Absent on
  // /api/conversations/providers (the chat provider list). Optional
  // so both endpoints can deserialize into the same type.
  user_credential_count?: number;
};

export async function listConversations(
  q?: string,
): Promise<ConvSummary[]> {
  const params = new URLSearchParams();
  if (q) params.set("q", q);
  const url = `${INTERNAL_API}/api/conversations${params.toString() ? `?${params}` : ""}`;
  const r = await fetch(url, {
    headers: await withSessionHeaders(),
    cache: "no-store",
  });
  if (!r.ok) throw new Error(`listConversations failed: ${r.status}`);
  return (await r.json()) as ConvSummary[];
}

export async function getConversation(id: string): Promise<ConvDetail> {
  const r = await fetch(`${INTERNAL_API}/api/conversations/${encodeURIComponent(id)}`, {
    headers: await withSessionHeaders(),
    cache: "no-store",
  });
  if (!r.ok) throw new Error(`getConversation failed: ${r.status}`);
  return (await r.json()) as ConvDetail;
}

export async function listProviders(): Promise<ProviderShape[]> {
  const r = await fetch(`${INTERNAL_API}/api/conversations/providers`, {
    headers: await withSessionHeaders(),
    cache: "no-store",
  });
  if (!r.ok) throw new Error(`listProviders failed: ${r.status}`);
  return (await r.json()) as ProviderShape[];
}

export type CandidateRef = {
  id: string;
  candidate_type: string;
  title: string | null;
  disposition: string;
  match_confidence: string | null;
  match_explanation: string | null;
};

export async function listConversationCandidates(
  convId: string,
): Promise<CandidateRef[]> {
  const r = await fetch(
    `${INTERNAL_API}/api/conversations/${encodeURIComponent(convId)}/candidates`,
    { headers: await withSessionHeaders(), cache: "no-store" },
  );
  if (!r.ok) throw new Error(`listConversationCandidates failed: ${r.status}`);
  return (await r.json()) as CandidateRef[];
}

// ---------------------------------------------------------------------------
// Home AI partner (docs/10).

export type SuggestedQuestion = {
  visible_text: string;
  submitted_text: string;
  scope_hint: string;
  scope: Record<string, unknown> | null;
};

export type HomeAiPartner = {
  suggested_questions: SuggestedQuestion[];
  recent_conversations: Array<{
    id: string;
    title: string | null;
    kind: string;
    provider: string | null;
    model: string | null;
    last_message_at: string | null;
  }>;
  recent_episodes: Array<{
    id: string;
    title: string;
    kind: string;
    date_start: string | null;
    summary: string | null;
  }>;
  make_sense_targets: Array<{
    kind: string;
    label: string;
    href: string | null;
    detail: string | null;
  }>;
  providers: ProviderShape[];
  insight?: {
    body: string;
    question: string | null;
    related_fact_ids: string[];
    generated_at: string;
  } | null;
};

export async function getHomeAiPartner(): Promise<HomeAiPartner> {
  const r = await fetch(`${INTERNAL_API}/api/home/ai-partner`, {
    headers: await withSessionHeaders(),
    cache: "no-store",
  });
  if (!r.ok) throw new Error(`getHomeAiPartner failed: ${r.status}`);
  return (await r.json()) as HomeAiPartner;
}

// ---------------------------------------------------------------------------
// Episodes (docs/10).

export type EpisodeMember = {
  id: string;
  member_type: string;
  subject_id: string;
  role: string;
  ordinal: number;
  note: string | null;
};

export type RelatedConversation = {
  id: string;
  title: string | null;
  kind: string;
  last_message_at: string | null;
  // "member" = explicit attach via episode_members.
  // "anchor_fact" = legacy EI link via scope.anchor_fact_id.
  link_source: "member" | "anchor_fact";
};

export type EpisodeSummary = {
  id: string;
  title: string;
  display_title: string | null;
  aliases: string[];
  summary: string | null;
  kind: string;
  date_start: string | null;
  date_end: string | null;
  primary_fact_id: string | null;
  created_by: string;
  created_at: string;
};

export type EpisodeDetail = EpisodeSummary & {
  payload: Record<string, unknown>;
  members: EpisodeMember[];
  related_conversations: RelatedConversation[];
};

export async function getEpisode(id: string): Promise<EpisodeDetail> {
  const r = await fetch(`${INTERNAL_API}/api/episodes/${encodeURIComponent(id)}`, {
    headers: await withSessionHeaders(),
    cache: "no-store",
  });
  if (!r.ok) throw new Error(`getEpisode failed: ${r.status}`);
  return (await r.json()) as EpisodeDetail;
}

export async function listEpisodes(opts?: {
  q?: string;
  kind?: string;
  date_from?: string;
  date_to?: string;
  limit?: number;
}): Promise<EpisodeSummary[]> {
  const params = new URLSearchParams();
  if (opts?.q) params.set("q", opts.q);
  if (opts?.kind) params.set("kind", opts.kind);
  if (opts?.date_from) params.set("date_from", opts.date_from);
  if (opts?.date_to) params.set("date_to", opts.date_to);
  if (opts?.limit) params.set("limit", String(opts.limit));
  const url = `${INTERNAL_API}/api/episodes${params.toString() ? `?${params}` : ""}`;
  const r = await fetch(url, {
    headers: await withSessionHeaders(),
    cache: "no-store",
  });
  if (!r.ok) throw new Error(`listEpisodes failed: ${r.status}`);
  return (await r.json()) as EpisodeSummary[];
}

// ---------------------------------------------------------------------------
// Save / attach a Conversation to an Event or Dossier. Used by the chat
// Save menu (#89). No LLM calls — pure structural plumbing.

export type SaveAsEventBody = {
  title: string;
  display_title?: string | null;
  aliases?: string[];
  summary?: string | null;
  kind?: string | null;
  date_start?: string | null;
};

export type SaveAsEventResult = { episode_id: string };

export type SaveAsTopicBody = {
  name: string;
  aliases?: string[];
  description?: string | null;
};

export type SaveAsTopicResult = {
  topic_id: string;
  slug: string;
  conflict: boolean;
};

export type AttachToTopicResult = {
  slug: string;
  conversation_id: string;
  already_attached: boolean;
};

// ---------------------------------------------------------------------------
// LLM providers — BYOK settings UI. (ProviderShape is declared above
// next to the chat provider list — same shape, with an optional
// `user_credential_count` set only by the /api/llm-providers catalog.)

export type ProviderCatalog = {
  providers: ProviderShape[];
};

export type CredentialOut = {
  id: string;
  provider: string;
  auth_kind: string;
  label: string | null;
  default_model: string | null;
  endpoint_url: string | null;
  capabilities: Record<string, unknown>;
  last_used_at: string | null;
  revoked_at: string | null;
  created_at: string;
  has_secret: boolean;
};

export async function getProviderCatalog(): Promise<ProviderCatalog> {
  const r = await fetch(`${INTERNAL_API}/api/llm-providers`, {
    headers: await withSessionHeaders(),
    cache: "no-store",
  });
  if (!r.ok) throw new Error(`getProviderCatalog failed: ${r.status}`);
  return (await r.json()) as ProviderCatalog;
}

export async function listCredentials(): Promise<CredentialOut[]> {
  const r = await fetch(`${INTERNAL_API}/api/llm-providers/credentials`, {
    headers: await withSessionHeaders(),
    cache: "no-store",
  });
  if (!r.ok) throw new Error(`listCredentials failed: ${r.status}`);
  return (await r.json()) as CredentialOut[];
}

// ---------------------------------------------------------------------------
// Calendar sources (FU-CAL-WEB-SETTINGS-UI)
//
// Mirrors `routes/calendar.py::CalendarSourceOut`. The active-record
// scope is enforced server-side; this client doesn't need to pin a
// record. iOS EventKit is the only adapter wired today; the UI
// surfaces other adapter_type values as placeholders.

export type CalendarPrivacyMode = "full_details" | "title_and_time" | "busy_only";

export type CalendarSyncStatus = "ok" | "empty";

export type CalendarSourceOut = {
  id: string;
  adapter_type: string;
  external_id: string;
  display_name: string;
  privacy_mode: CalendarPrivacyMode;
  llm_full_details_consent: boolean;
  connected_at: string;
  disconnected_at: string | null;
  // FU-CAL-SOURCE-STATUS — populated by the backend so the web
  // settings UI doesn't have to client-side count events.
  last_sync_at: string | null;
  last_sync_status: CalendarSyncStatus | null;
  visible_event_count: number;
  stored_event_count: number;
  // FU-CAL-HISTORY-WINDOW — per-source enum: 90d / 1y / 3y / 5y / all.
  history_window_back: string;
};

export async function listCalendarSources(): Promise<CalendarSourceOut[]> {
  const r = await fetch(`${INTERNAL_API}/api/calendar/sources`, {
    headers: await withSessionHeaders(),
    cache: "no-store",
  });
  if (!r.ok) throw new Error(`listCalendarSources failed: ${r.status}`);
  return (await r.json()) as CalendarSourceOut[];
}

// ---------------------------------------------------------------------------
// Google Calendar OAuth (Beta 1 Section A)
//
// Mirrors `routes/calendar_google.py`. The connect-start endpoint
// returns 503 when the operator hasn't configured the three env
// vars — the UI must surface "not configured by this OwnChart
// operator" rather than appearing broken.
//
// FU-CAL-MULTI-CALENDAR-PICKER (2026-05-22) — a single Google
// account can contain multiple calendars; the UI distinguishes
// the connected ``CalendarOAuthCredential`` from the per-calendar
// ``CalendarSource`` rows.

export type GoogleConnectStartResponse = {
  authorize_url: string;
  state: string;
};

export type GoogleCalendarChoice = {
  external_id: string;
  summary: string;
  primary: boolean;
  time_zone: string | null;
  background_color: string | null;
};

export type GoogleCallbackResponse = {
  credential_id: string;
  google_account_email: string;
  calendars: GoogleCalendarChoice[];
};

export type GoogleCredentialOut = {
  id: string;
  google_account_email: string;
  status: string;
  last_synced_at: string | null;
  bound_source_count: number;
};

export type GoogleBindRequest = {
  external_id: string;
  display_name: string;
  privacy_mode: CalendarPrivacyMode;
  llm_full_details_consent: boolean;
  history_window_back: string;
};

export type GoogleBindResponse = {
  source_id: string;
  adapter_type: string;
  external_id: string;
  display_name: string;
  privacy_mode: string;
  history_window_back: string;
};

export type GoogleConfiguredStatus =
  | { configured: true }
  | { configured: false; detail: string };

/** SSR-friendly probe: hits connect-start; treats 503 as "not
 * configured by operator" (the canonical message). 401/403 mean
 * the user isn't authenticated/authorized — the layout handles
 * that already. Any other status is treated as configured=false
 * to avoid surfacing a broken-feeling UI. */
export async function probeGoogleCalendarConfigured(): Promise<GoogleConfiguredStatus> {
  const r = await fetch(`${INTERNAL_API}/api/calendar/google/connect-start`, {
    method: "POST",
    headers: await withSessionHeaders(),
    cache: "no-store",
  });
  if (r.ok) return { configured: true };
  if (r.status === 503) {
    return {
      configured: false,
      detail:
        "Google Calendar is not configured by this OwnChart operator.",
    };
  }
  // 401/403/etc. — we don't surface to the user from here; the
  // settings page will fail the layout check first.
  return { configured: false, detail: `HTTP ${r.status}` };
}

export async function listGoogleCredentials(): Promise<GoogleCredentialOut[]> {
  const r = await fetch(
    `${INTERNAL_API}/api/calendar/google/credentials`,
    { headers: await withSessionHeaders(), cache: "no-store" },
  );
  if (!r.ok) throw new Error(`listGoogleCredentials failed: ${r.status}`);
  return (await r.json()) as GoogleCredentialOut[];
}

/** Server-side helper for the /settings/calendar/google/callback
 * Next.js page: exchanges Google's `code` + `state` for a stored
 * credential and returns the calendar picker payload. */
export async function exchangeGoogleCallback(
  code: string,
  state: string,
): Promise<GoogleCallbackResponse> {
  const params = new URLSearchParams({ code, state });
  const r = await fetch(
    `${INTERNAL_API}/api/calendar/google/callback?${params}`,
    { headers: await withSessionHeaders(), cache: "no-store" },
  );
  if (!r.ok) {
    const body = await r.text();
    throw new Error(
      `Google OAuth callback failed (HTTP ${r.status}): ${body}`,
    );
  }
  return (await r.json()) as GoogleCallbackResponse;
}


// --- Section D: Export / Portability -------------------------------------

export type ExportStatus = "pending" | "running" | "completed" | "failed";
export type ExportFormat =
  | "ownchart_json"
  | "txt"
  | "pictal_json"
  | "all";
export type ExportFileType = "ownchart_json" | "txt" | "pictal_json";
export type ExportDateRangeKind = "all" | "last_90d" | "last_1y" | "custom";
export type ExportDomain = "clinical" | "body_signals" | "calendar";

export type ExportFiltersIn = {
  date_range_kind: ExportDateRangeKind;
  date_range_start?: string | null;
  date_range_end?: string | null;
  domains: ExportDomain[];
};

export type ExportFile = {
  id: string;
  file_type: ExportFileType;
  byte_size: number | null;
  sha256: string | null;
};

export type ExportJob = {
  id: string;
  requested_format: ExportFormat;
  status: ExportStatus;
  requested_at: string;
  started_at: string | null;
  completed_at: string | null;
  failed_at: string | null;
  expires_at: string | null;
  error_message: string | null;
  files: ExportFile[];
  filters: ExportFiltersIn | null;
};

export type CreateExportBody = {
  requested_format: ExportFormat;
  filters?: ExportFiltersIn;
};

export async function listExports(): Promise<ExportJob[]> {
  const r = await fetch(`${INTERNAL_API}/api/exports`, {
    headers: await withSessionHeaders(),
    cache: "no-store",
  });
  if (r.status === 401 || r.status === 403) return [];
  if (!r.ok) throw new Error(`listExports failed: ${r.status}`);
  return (await r.json()) as ExportJob[];
}
