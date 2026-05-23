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

      {/* P0 fix (2026-05-23): "Your connectors" must be record-scoped
          authenticated connections only — not the global ProviderConnector
          catalog. Pre-fix, a fresh Test User on a fresh record saw every
          provider Nick had registered listed under "Your connectors,"
          which conflated catalog rows with this user's actual portal
          authentications. The route already returns `connection: null`
          for catalog rows the active user/record hasn't authenticated to;
          we split here so the rendering matches the semantic. Catalog
          rows the user has NOT yet authenticated to surface under
          "Available providers" so they're still reachable for a connect
          click after AddProvider creates them.
       */}
      {(() => {
        const yours = connectors.filter((c) => c.connection !== null);
        const available = connectors.filter((c) => c.connection === null);
        return (
          <>
            <section className="mt-12">
              <h2 className="font-serif text-xl">
                Your connectors {yours.length > 0 ? `(${yours.length})` : ""}
              </h2>
              {yours.length === 0 ? (
                <p className="mt-3 text-muted">
                  No connectors yet. Use the search above to add your
                  first provider, then click <em>Connect</em> on it
                  below to authenticate.
                </p>
              ) : (
                <ul className="mt-3 space-y-3">
                  {yours.map((c) => (
                    <ConnectorRow key={c.id} c={c} />
                  ))}
                </ul>
              )}
            </section>

            {available.length > 0 && (
              <section className="mt-12">
                <h2 className="font-serif text-xl">
                  Provider catalog on this instance ({available.length})
                </h2>
                <p className="mt-1 text-sm text-muted">
                  These are EHR portals the instance operator has
                  configured. They are <strong>not your data</strong>{" "}
                  &mdash; just endpoints you may authenticate against
                  with your own patient-portal credentials. Click{" "}
                  <em>Connect</em> on a row to start the OAuth flow.
                </p>
                <ul className="mt-3 space-y-3">
                  {available.map((c) => (
                    <ConnectorRow key={c.id} c={c} />
                  ))}
                </ul>
              </section>
            )}
          </>
        );
      })()}
    </div>
  );
}
