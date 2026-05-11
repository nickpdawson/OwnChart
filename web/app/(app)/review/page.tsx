import { listFactsByState } from "@/lib/api";
import { MedicationPatterns } from "./MedicationPatterns";
import { ReviewClient } from "./ReviewClient";

export const dynamic = "force-dynamic";

export default async function ReviewPage() {
  const facts = await listFactsByState("needs_review");
  return (
    <div className="max-w-4xl">
      <p className="text-sm uppercase tracking-widest text-muted">Review inbox</p>
      <h1 className="mt-2 font-serif text-3xl">Facts that need a human eye</h1>
      <p className="mt-3 max-w-2xl text-muted">
        Provider-attested data from EHR exports auto-confirms; nursing-template noise
        auto-defers. What lands here are facts the system genuinely can&apos;t verify on
        its own — mostly Claude vision extractions from scanned documents. You can
        always correct or reject anything from any view; this is just where the
        system surfaces uncertainty.
      </p>
      <p className="mt-2 text-xs text-muted">{facts.length} pending</p>

      {/* Pattern-level decisions land first (docs/08 Review Queue Triage).
          Accepting a medication pattern suppresses its member rows from
          the inbox so they don't recur as 200 individual decisions. */}
      <MedicationPatterns />

      <ReviewClient initial={facts} />
    </div>
  );
}
