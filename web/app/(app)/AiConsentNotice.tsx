import Link from "next/link";

// Shown at the top of every AI surface (Home insight, Chat, Ask, topic
// briefs) when the signed-in user has not granted AI consent. The rest
// of the page keeps working; only model-backed features are off. The
// server refuses egress regardless (api/ownchart/llm/egress.py) — this
// is the explanation, not the enforcement.
export function AiConsentNotice({ feature }: { feature: string }) {
  return (
    <div className="rounded-xl border border-caution/30 bg-caution/5 p-4 text-sm">
      <p className="font-medium">AI is off for your account</p>
      <p className="mt-1 text-muted">
        {feature} uses an AI provider and sends it parts of your record. You
        have not enabled that, so this feature is unavailable. Your records,
        corrections, topics and exports keep working without it.
      </p>
      <Link
        href="/consent"
        className="mt-3 inline-flex min-h-[44px] items-center rounded-lg border border-muted/30 px-4 py-2 hover:bg-bg"
      >
        Review what AI would receive
      </Link>
    </div>
  );
}
