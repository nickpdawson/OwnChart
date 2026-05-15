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
      <p className="mt-2 max-w-2xl text-xs text-muted">
        &quot;Sign in with Claude&quot; and &quot;Sign in with ChatGPT&quot;
        aren&apos;t available — neither Anthropic nor OpenAI exposes
        consumer OAuth for API access. API keys are the supported
        path until that changes upstream.
      </p>

      <div className="mt-4">
        <a
          href="/settings/providers/usage"
          className="inline-block rounded-md border border-accent/40 px-3 py-1.5 text-sm text-accent hover:bg-accent/5"
        >
          Where the spend went →
        </a>
      </div>

      <ProvidersClient
        catalog={catalog.providers}
        initialCredentials={credentials}
      />
    </div>
  );
}
