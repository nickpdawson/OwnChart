import { cookies } from "next/headers";

const INTERNAL_API =
  process.env.OWNCHART_API_INTERNAL_URL || "http://api:8000";

export type Me = {
  id: string;
  email: string;
  phi_consent_granted: boolean;
};

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
};

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

export async function listTopics(): Promise<TopicSummary[]> {
  const r = await fetch(`${INTERNAL_API}/api/topics`, {
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

export type TimelineResponse = {
  grain: TimelineGrain;
  range_start: string;
  range_end: string;
  buckets: TimelineBucket[];
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

export type EpisodeDetail = {
  id: string;
  title: string;
  summary: string | null;
  kind: string;
  date_start: string | null;
  date_end: string | null;
  primary_fact_id: string | null;
  created_by: string;
  created_at: string;
  payload: Record<string, unknown>;
  members: EpisodeMember[];
};

export async function getEpisode(id: string): Promise<EpisodeDetail> {
  const r = await fetch(`${INTERNAL_API}/api/episodes/${encodeURIComponent(id)}`, {
    headers: await withSessionHeaders(),
    cache: "no-store",
  });
  if (!r.ok) throw new Error(`getEpisode failed: ${r.status}`);
  return (await r.json()) as EpisodeDetail;
}

export async function listEpisodes(): Promise<EpisodeDetail[]> {
  const r = await fetch(`${INTERNAL_API}/api/episodes`, {
    headers: await withSessionHeaders(),
    cache: "no-store",
  });
  if (!r.ok) throw new Error(`listEpisodes failed: ${r.status}`);
  return (await r.json()) as EpisodeDetail[];
}
