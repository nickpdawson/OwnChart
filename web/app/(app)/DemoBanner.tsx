// Persistent banner rendered when the server-side
// /api/instance/info.demo_mode is true. Tells visitors loudly that
// they're looking at a synthetic demo and must not type real medical
// information into it.

export function DemoBanner() {
  return (
    <div
      role="status"
      aria-label="Demo mode"
      className="mb-6 rounded-xl border-2 border-caution/40 bg-caution/10 px-4 py-3 text-sm text-caution"
    >
      <p className="font-medium">
        Demo · synthetic data · do not enter real medical information
      </p>
      <p className="mt-1 text-caution/90">
        You&apos;re looking at the public OwnChart demo, signed in as
        a shared sample account. The record below is a synthetic
        patient — not real PHI. Anything you type into Ask is
        processed by the demo&apos;s LLM provider, so do not enter
        your own medical details here. Stand up your own instance
        from the{" "}
        <a
          href="https://github.com/nickpdawson/OwnChart"
          className="underline-offset-4 hover:underline"
        >
          GitHub repo
        </a>{" "}
        to keep your own record.
      </p>
    </div>
  );
}
