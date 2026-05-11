import { getEffectiveSettings, getMe, getSettingsRegistry } from "@/lib/api";
import { SettingsClient } from "./SettingsClient";

export const dynamic = "force-dynamic";

// docs/09 — Settings page. V1 ships Profile (read-only account
// summary) + Privacy and AI + Review and Discovery + Display, all
// driven by the registry. Admin / instance / household sections will
// arrive when there's a second user to admin.

export default async function SettingsPage() {
  const [me, registry, effective] = await Promise.all([
    getMe(),
    getSettingsRegistry(),
    getEffectiveSettings(),
  ]);

  return (
    <div className="max-w-3xl">
      <p className="text-sm uppercase tracking-widest text-muted">Settings</p>
      <h1 className="mt-2 font-serif text-3xl">How OwnChart works for you</h1>
      <p className="mt-3 max-w-2xl text-muted">
        Each setting shows where it&apos;s stored and whether it controls
        anything PHI-sensitive. Changes are audited.
      </p>

      <SettingsClient
        registry={registry.settings}
        initialValues={effective.values}
        email={me?.email ?? null}
        phiConsent={me?.phi_consent_granted ?? false}
      />
    </div>
  );
}
