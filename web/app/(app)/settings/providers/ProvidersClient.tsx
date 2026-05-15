"use client";

import { useState } from "react";
import type { CredentialOut, ProviderShape } from "@/lib/api";

// V1 provider menu — Anthropic API key is fully wired.
// "Sign in with Claude / ChatGPT" rows are honest about the upstream
// gap: consumer OAuth for API access isn't something Anthropic or
// OpenAI offers today. "Coming soon" would imply OwnChart-side roadmap;
// "Not currently supported" puts the gap where it belongs — with the
// provider, not us. If/when the upstream ships it, our wiring is
// already in place to flip status="live".
const PROVIDER_ROWS: {
  key: string;
  label: string;
  blurb: string;
  status: "live" | "unsupported_upstream" | "stub";
  href?: string;
}[] = [
  {
    key: "anthropic",
    label: "Anthropic API key",
    blurb:
      "Paste a key from console.anthropic.com. OwnChart bills your account; the deployment default isn't touched.",
    status: "live",
  },
  {
    key: "claude_oauth",
    label: "Sign in with Claude (claude.ai)",
    blurb:
      "Not currently supported by Anthropic. OwnChart supports API keys today. If Anthropic exposes consumer OAuth for API access later, this can become a sign-in flow.",
    status: "unsupported_upstream",
    href: "/settings/providers/claude-oauth",
  },
  {
    key: "openai",
    label: "OpenAI API key",
    blurb:
      "Bring a key from platform.openai.com. The OpenAI provider exists, but model routing for OpenAI isn't wired into Ask/EI yet.",
    status: "stub",
  },
  {
    key: "chatgpt_oauth",
    label: "Sign in with ChatGPT",
    blurb:
      "Not currently supported by OpenAI. Same situation as Claude — API access requires an API key, not a consumer subscription login.",
    status: "unsupported_upstream",
  },
  {
    key: "local_openai",
    label: "Local OpenAI-compatible (Ollama / LM Studio / vLLM)",
    blurb:
      "Point OwnChart at a local endpoint. Best for full-record questions without ever leaving your machine.",
    status: "stub",
  },
];

type Props = {
  catalog: ProviderShape[];
  initialCredentials: CredentialOut[];
};

export function ProvidersClient({ catalog, initialCredentials }: Props) {
  const [credentials, setCredentials] = useState<CredentialOut[]>(initialCredentials);
  const [error, setError] = useState<string | null>(null);

  async function refresh() {
    try {
      const r = await fetch("/api/llm-providers/credentials", {
        credentials: "include",
        cache: "no-store",
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setCredentials((await r.json()) as CredentialOut[]);
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function saveKey(provider: string, secret: string, label: string | null) {
    setError(null);
    try {
      const r = await fetch("/api/llm-providers/credentials", {
        method: "POST",
        headers: { "content-type": "application/json" },
        credentials: "include",
        body: JSON.stringify({
          provider,
          auth_kind: "api_key",
          secret,
          label,
        }),
      });
      if (!r.ok) {
        const detail = await r.text();
        throw new Error(detail || `HTTP ${r.status}`);
      }
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function revoke(id: string) {
    setError(null);
    try {
      const r = await fetch(`/api/llm-providers/credentials/${id}`, {
        method: "DELETE",
        credentials: "include",
      });
      if (!r.ok && r.status !== 204) throw new Error(`HTTP ${r.status}`);
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  const liveByProvider = new Map<string, CredentialOut[]>();
  for (const c of credentials) {
    if (c.revoked_at) continue;
    const list = liveByProvider.get(c.provider) ?? [];
    list.push(c);
    liveByProvider.set(c.provider, list);
  }
  const deploymentByProvider = new Map<string, boolean>();
  for (const p of catalog) {
    // catalog.configured is True when EITHER the deployment default
    // works OR the user has a row — so we can't read it as "server
    // has a key." Use user_credential_count as the divider.
    deploymentByProvider.set(
      p.key,
      p.configured && (p.user_credential_count ?? 0) === 0,
    );
  }

  return (
    <div className="mt-7 space-y-5">
      {error && (
        <p className="rounded-md border border-caution/30 bg-caution/10 p-3 text-sm text-caution">
          {error}
        </p>
      )}

      {PROVIDER_ROWS.map((row) => {
        const userKeys = liveByProvider.get(row.key) ?? [];
        const fallbackOnDeploy = deploymentByProvider.get(row.key) ?? false;
        return (
          <ProviderCard
            key={row.key}
            row={row}
            userKeys={userKeys}
            fallbackOnDeploy={fallbackOnDeploy}
            onSave={(secret, label) => saveKey(row.key, secret, label)}
            onRevoke={revoke}
          />
        );
      })}

      <p className="pt-3 text-xs text-muted">
        Keys are encrypted with AES-256-GCM and only decrypted in
        memory for the model call. Revoking soft-deletes the row;
        the audit trail keeps the disposition.
      </p>
    </div>
  );
}

function ProviderCard({
  row,
  userKeys,
  fallbackOnDeploy,
  onSave,
  onRevoke,
}: {
  row: (typeof PROVIDER_ROWS)[number];
  userKeys: CredentialOut[];
  fallbackOnDeploy: boolean;
  onSave: (secret: string, label: string | null) => Promise<void>;
  onRevoke: (id: string) => Promise<void>;
}) {
  const [secret, setSecret] = useState("");
  const [label, setLabel] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!secret) return;
    setBusy(true);
    try {
      await onSave(secret, label || null);
      setSecret("");
      setLabel("");
    } finally {
      setBusy(false);
    }
  }

  const isLive = row.status === "live";
  const isStub = row.status === "stub";
  const isUnsupportedUpstream = row.status === "unsupported_upstream";

  return (
    <section className="rounded-xl border border-muted/15 bg-surface p-5">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <div>
          <h2 className="font-serif text-lg">{row.label}</h2>
          <p className="mt-1 max-w-2xl text-sm text-muted">{row.blurb}</p>
        </div>
        <StatusBadge status={row.status} />
      </div>

      {userKeys.length > 0 && (
        <ul className="mt-4 space-y-2">
          {userKeys.map((c) => (
            <li
              key={c.id}
              className="flex flex-wrap items-baseline justify-between gap-3 rounded-lg border border-accent/30 bg-accent/5 p-3 text-sm"
            >
              <div>
                <p className="font-medium">
                  {c.label || "(unlabeled key)"}
                </p>
                <p className="text-xs text-muted">
                  Added {new Date(c.created_at).toLocaleDateString()}
                  {c.last_used_at && (
                    <>
                      {" · "}last used {new Date(c.last_used_at).toLocaleString()}
                    </>
                  )}
                </p>
              </div>
              <button
                type="button"
                onClick={() => onRevoke(c.id)}
                className="rounded-md border border-muted/30 px-2 py-1 text-xs hover:bg-muted/5"
              >
                Revoke
              </button>
            </li>
          ))}
        </ul>
      )}

      {userKeys.length === 0 && fallbackOnDeploy && isLive && (
        <p className="mt-3 rounded-md bg-muted/5 p-2 text-xs text-muted">
          Using the deployment&apos;s shared key. Add your own below to
          stop sharing the bill.
        </p>
      )}

      {isLive && (
        <form onSubmit={submit} className="mt-4 space-y-2">
          <label className="block text-xs uppercase tracking-widest text-muted">
            New {row.label}
          </label>
          <input
            type="password"
            value={secret}
            onChange={(e) => setSecret(e.target.value)}
            placeholder="sk-ant-..."
            className="w-full rounded-md border border-muted/30 bg-bg/40 px-2 py-1.5 text-sm font-mono"
            autoComplete="off"
          />
          <input
            type="text"
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            placeholder="Label (optional, e.g. 'personal — billed to me')"
            className="w-full rounded-md border border-muted/30 bg-bg/40 px-2 py-1.5 text-sm"
          />
          <button
            type="submit"
            disabled={busy || !secret}
            className="rounded-md bg-accent px-3 py-1.5 text-sm text-surface hover:opacity-90 disabled:opacity-50"
          >
            {busy ? "Saving…" : "Save key"}
          </button>
        </form>
      )}

      {isUnsupportedUpstream && row.href && (
        <a
          href={row.href}
          className="mt-4 inline-block text-sm text-accent underline-offset-4 hover:underline"
        >
          Why it&apos;s not available →
        </a>
      )}

      {isStub && (
        <p className="mt-3 text-xs text-muted">
          The provider plumbing is here; routing Ask / Episode
          Intelligence to it isn&apos;t. Track progress in the morning
          log.
        </p>
      )}
    </section>
  );
}

function StatusBadge({ status }: { status: "live" | "unsupported_upstream" | "stub" }) {
  if (status === "live") {
    return (
      <span className="rounded-md bg-evidence/15 px-2 py-0.5 text-xs text-evidence">
        Live
      </span>
    );
  }
  if (status === "unsupported_upstream") {
    return (
      <span className="rounded-md bg-muted/15 px-2 py-0.5 text-xs text-muted">
        Not currently supported
      </span>
    );
  }
  return (
    <span className="rounded-md bg-caution/10 px-2 py-0.5 text-xs text-caution">
      Stub
    </span>
  );
}
