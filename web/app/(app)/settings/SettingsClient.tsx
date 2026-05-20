"use client";

import { useState } from "react";
import type { SettingShape } from "@/lib/api";

type Props = {
  registry: SettingShape[];
  initialValues: Record<string, unknown>;
  email: string | null;
  phiConsent: boolean;
};

const SECTIONS: { key: string; label: string; blurb: string }[] = [
  {
    key: "profile",
    label: "Profile",
    blurb: "Your account. Read-only in V1.",
  },
  {
    key: "privacy_ai",
    label: "Privacy and AI",
    blurb:
      "Controls how much of your record OwnChart may send to an LLM, and when. Off means no external AI calls.",
  },
  {
    key: "review_discovery",
    label: "Review and Discovery",
    blurb:
      "What lands in your review inbox and how often Discover surfaces new items.",
  },
  {
    key: "display",
    label: "Display",
    blurb: "Theme, units, and timeline density.",
  },
];

export function SettingsClient({
  registry,
  initialValues,
  email,
  phiConsent,
}: Props) {
  const [values, setValues] = useState<Record<string, unknown>>(initialValues);
  const [saving, setSaving] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function save(key: string, value: unknown) {
    setSaving(key);
    setError(null);
    try {
      const r = await fetch(`/api/settings/user`, {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ key, value }),
      });
      if (!r.ok) {
        const detail = await r.text();
        throw new Error(detail || `HTTP ${r.status}`);
      }
      const next = (await r.json()) as { key: string; value: unknown };
      setValues((prev) => ({ ...prev, [next.key]: next.value }));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(null);
    }
  }

  return (
    <div className="mt-7 space-y-9">
      {/* Profile section — flat read-only summary. */}
      <section>
        <h2 className="text-xs uppercase tracking-widest text-muted">Profile</h2>
        <p className="mt-1 text-sm text-muted">
          Your account. Read-only in V1.
        </p>
        <dl className="mt-3 grid grid-cols-1 gap-2 rounded-xl border border-muted/15 bg-surface p-4 text-sm sm:grid-cols-[8rem_1fr]">
          <dt className="text-muted">Email</dt>
          <dd className="font-mono text-xs">{email ?? "—"}</dd>
          <dt className="text-muted">PHI / LLM consent</dt>
          <dd>
            {phiConsent ? (
              <span className="text-evidence">Granted</span>
            ) : (
              <span className="text-caution">Not granted</span>
            )}
          </dd>
        </dl>
      </section>

      {/* AI providers — link out to the BYOK page. Inline editor
          would dwarf the rest of Settings; keep it on its own. */}
      <section>
        <h2 className="text-xs uppercase tracking-widest text-muted">
          AI providers
        </h2>
        <p className="mt-1 max-w-2xl text-sm text-muted">
          Bring your own Anthropic / OpenAI key, or use the
          deployment&apos;s shared key. Sign in with Claude when
          Anthropic exposes it.
        </p>
        <a
          href="/settings/providers"
          className="mt-3 inline-block rounded-md border border-accent/40 px-3 py-1.5 text-sm text-accent hover:bg-accent/10"
        >
          Manage AI providers →
        </a>
      </section>

      <section>
        <h2 className="text-xs uppercase tracking-widest text-muted">
          Calendar sources
        </h2>
        <p className="mt-1 max-w-2xl text-sm text-muted">
          Connected calendars for this record. Privacy mode controls
          what gets stored at ingest; AI exposure is a separate decision.
        </p>
        <a
          href="/settings/calendar"
          className="mt-3 inline-block rounded-md border border-accent/40 px-3 py-1.5 text-sm text-accent hover:bg-accent/10"
        >
          Manage calendar sources →
        </a>
      </section>

      {SECTIONS.filter((s) => s.key !== "profile").map((section) => {
        const items = registry.filter((r) => r.section === section.key);
        if (items.length === 0) return null;
        return (
          <section key={section.key}>
            <h2 className="text-xs uppercase tracking-widest text-muted">
              {section.label}
            </h2>
            <p className="mt-1 max-w-2xl text-sm text-muted">{section.blurb}</p>
            <ul className="mt-3 space-y-3">
              {items.map((s) => (
                <SettingRow
                  key={s.key}
                  setting={s}
                  value={values[s.key]}
                  saving={saving === s.key}
                  onChange={(v) => save(s.key, v)}
                />
              ))}
            </ul>
          </section>
        );
      })}

      {error && (
        <p className="text-sm text-caution">Couldn&apos;t save: {error}</p>
      )}
    </div>
  );
}

function SettingRow({
  setting,
  value,
  saving,
  onChange,
}: {
  setting: SettingShape;
  value: unknown;
  saving: boolean;
  onChange: (v: unknown) => void;
}) {
  return (
    <li className="rounded-xl border border-muted/15 bg-surface p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <div>
          <p className="font-medium">{setting.label}</p>
          <p className="mt-1 max-w-2xl text-sm text-muted">
            {setting.description}
          </p>
        </div>
        <div className="text-right">
          <Control
            setting={setting}
            value={value}
            disabled={!setting.ui_writable || saving}
            onChange={onChange}
          />
        </div>
      </div>
      <div className="mt-3 flex flex-wrap gap-2 text-[10px] uppercase tracking-widest text-muted">
        <SourceLabel storage={setting.storage} />
        {setting.phi_sensitive && (
          <span className="rounded-md bg-caution/15 px-1.5 py-0.5 text-caution">
            PHI-sensitive
          </span>
        )}
        {setting.requires_restart && (
          <span className="rounded-md bg-muted/10 px-1.5 py-0.5">
            Restart required
          </span>
        )}
        {setting.admin_lockable && (
          <span className="rounded-md bg-muted/10 px-1.5 py-0.5">
            Admin-lockable
          </span>
        )}
        {saving && <span className="text-muted">Saving…</span>}
      </div>
    </li>
  );
}

function SourceLabel({ storage }: { storage: string }) {
  if (storage === "database") {
    return (
      <span className="rounded-md bg-muted/10 px-1.5 py-0.5">
        Stored in your profile
      </span>
    );
  }
  if (storage === "config_file") {
    return (
      <span className="rounded-md bg-muted/10 px-1.5 py-0.5">
        Stored in config/ownchart.yaml
      </span>
    );
  }
  return (
    <span className="rounded-md bg-muted/10 px-1.5 py-0.5">
      Stored: {storage}
    </span>
  );
}

function Control({
  setting,
  value,
  disabled,
  onChange,
}: {
  setting: SettingShape;
  value: unknown;
  disabled: boolean;
  onChange: (v: unknown) => void;
}) {
  if (setting.type === "boolean") {
    const checked = Boolean(value);
    return (
      <button
        type="button"
        disabled={disabled}
        onClick={() => onChange(!checked)}
        className={
          "inline-flex h-7 w-12 items-center rounded-full transition-colors disabled:opacity-50 " +
          (checked ? "bg-accent" : "bg-muted/30")
        }
        aria-pressed={checked}
        aria-label={setting.label}
      >
        <span
          className={
            "inline-block h-5 w-5 transform rounded-full bg-surface shadow transition-transform " +
            (checked ? "translate-x-6" : "translate-x-1")
          }
        />
      </button>
    );
  }
  if (setting.type === "enum") {
    return (
      <select
        disabled={disabled}
        value={String(value ?? "")}
        onChange={(e) => onChange(e.target.value)}
        className="rounded-md border border-muted/30 bg-surface px-2 py-1 text-sm disabled:opacity-50"
      >
        {setting.choices.map((c) => (
          <option key={String(c)} value={String(c)}>
            {String(c).replace(/_/g, " ")}
          </option>
        ))}
      </select>
    );
  }
  if (setting.type === "integer") {
    return (
      <input
        type="number"
        disabled={disabled}
        defaultValue={Number(value ?? 0)}
        onBlur={(e) => {
          const v = Number(e.target.value);
          if (Number.isFinite(v)) onChange(v);
        }}
        className="w-24 rounded-md border border-muted/30 bg-surface px-2 py-1 text-right text-sm disabled:opacity-50"
      />
    );
  }
  return (
    <input
      type="text"
      disabled={disabled}
      defaultValue={String(value ?? "")}
      onBlur={(e) => onChange(e.target.value)}
      className="w-48 rounded-md border border-muted/30 bg-surface px-2 py-1 text-sm disabled:opacity-50"
    />
  );
}
