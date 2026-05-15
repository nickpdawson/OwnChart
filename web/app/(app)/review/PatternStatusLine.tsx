"use client";

import { useEffect, useState } from "react";

// Surfaces "the system has already absorbed X review decisions for
// you via pattern acceptance" so the user can SEE the compression is
// doing work. Without this, accepted patterns are invisible from the
// inbox — the user gets the relief but no signal that it happened.

type Stats = {
  accepted_patterns: number;
  suppressed_member_facts: number;
  last_accepted_at: string | null;
};

export function PatternStatusLine() {
  const [stats, setStats] = useState<Stats | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await fetch("/api/review/pattern-stats", {
          credentials: "include",
          cache: "no-store",
        });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const s = (await r.json()) as Stats;
        if (!cancelled) setStats(s);
      } catch (e) {
        if (!cancelled) setError((e as Error).message);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (error || !stats || stats.suppressed_member_facts === 0) return null;

  return (
    <p className="mt-2 text-xs text-muted">
      <span className="text-accent">
        {stats.suppressed_member_facts.toLocaleString()} fact
        {stats.suppressed_member_facts === 1 ? "" : "s"}
      </span>{" "}
      already managed by {stats.accepted_patterns} accepted pattern
      {stats.accepted_patterns === 1 ? "" : "s"} —{" "}
      <a
        href="/audit?event=pattern_managed_suppression"
        className="underline-offset-4 hover:underline"
      >
        view audit log
      </a>
      .
    </p>
  );
}
