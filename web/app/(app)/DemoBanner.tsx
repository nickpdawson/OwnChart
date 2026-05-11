// Persistent banner rendered when the server-side
// /api/instance/info.demo_mode is true. Tells visitors loudly that
// they're looking at synthetic sample data on a read-only instance.

export function DemoBanner() {
  return (
    <div
      role="status"
      aria-label="Demo mode"
      className="mb-6 rounded-xl border-2 border-caution/40 bg-caution/10 px-4 py-3 text-sm text-caution"
    >
      <p className="font-medium">
        Demo · sample data · read-only
      </p>
      <p className="mt-1 text-caution/90">
        You&apos;re looking at the public OwnChart demo. The record below
        is synthetic data from the Epic FHIR sandbox; nothing here
        belongs to a real patient. Writes are disabled. Stand up your
        own instance from the{" "}
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
