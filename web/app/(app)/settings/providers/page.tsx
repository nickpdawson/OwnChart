import { getProviderCatalog, listCredentials } from "@/lib/api";
import { ProvidersClient } from "./ProvidersClient";

export const dynamic = "force-dynamic";

export default async function ProvidersPage() {
  const [catalog, credentials] = await Promise.all([
    getProviderCatalog(),
    listCredentials(),
  ]);

  return (
    <div className="max-w-3xl">
      <p className="text-sm uppercase tracking-widest text-muted">
        Settings · AI providers
      </p>
      <h1 className="mt-2 font-serif text-3xl">
        Bring your own AI key
      </h1>
      <p className="mt-3 max-w-2xl text-muted">
        By default, OwnChart uses the deployment&apos;s shared
        Anthropic key. Add your own API key here and OwnChart will
        bill <em>your</em> account for every model call — Episode
        Intelligence, Ask, sensemaking, the lot. Keys are encrypted
        at rest and only decrypted in memory for the call.
      </p>

      <ProvidersClient
        catalog={catalog.providers}
        initialCredentials={credentials}
      />
    </div>
  );
}
