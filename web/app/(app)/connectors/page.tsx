import { listConnectors } from "@/lib/api";
import { AddProvider } from "./AddProvider";
import { ConnectorRow } from "./ConnectorRow";

export const dynamic = "force-dynamic";

type SearchParams = Promise<{ connected?: string; error?: string }>;

export default async function ConnectorsPage({
  searchParams,
}: {
  searchParams: SearchParams;
}) {
  const sp = await searchParams;
  const connectors = await listConnectors();

  return (
    <div className="max-w-4xl">
      <p className="text-sm uppercase tracking-widest text-muted">EHR connections</p>
      <h1 className="mt-2 font-serif text-3xl">Provider portals</h1>
      <p className="mt-3 max-w-2xl text-muted">
        Connect to your providers&apos; patient portals via SMART on FHIR. OwnChart
        receives an access token in your name (your data, your authorization)
        and pulls Conditions, Procedures, Medications, Observations, Documents,
        and more. Tokens are encrypted at rest. You can disconnect at any time.
      </p>

      {sp.connected && (
        <p className="mt-4 rounded-md border border-accent/30 bg-accent/10 p-3 text-sm">
          ✓ Connected to <strong>{sp.connected}</strong>. Click <em>Sync now</em> below to pull records.
        </p>
      )}
      {sp.error && (
        <p className="mt-4 rounded-md border border-caution/30 bg-caution/10 p-3 text-sm">
          Connection failed: <code>{sp.error}</code>
        </p>
      )}

      <section className="mt-8">
        <h2 className="font-serif text-xl">Add a provider</h2>
        <p className="mt-1 text-sm text-muted">
          Search Epic&apos;s open R4 endpoint directory, or paste a fhir_base URL for
          another vendor (Athena, Cerner). The chosen entry becomes a connector you
          can then authenticate against.
        </p>
        <AddProvider />
      </section>

      <section className="mt-12">
        <h2 className="font-serif text-xl">
          Your connectors {connectors.length > 0 ? `(${connectors.length})` : ""}
        </h2>
        {connectors.length === 0 ? (
          <p className="mt-3 text-muted">
            No connectors yet. Use the search above to add your first provider.
          </p>
        ) : (
          <ul className="mt-3 space-y-3">
            {connectors.map((c) => (
              <ConnectorRow key={c.id} c={c} />
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
