import { getMe } from "@/lib/api";
import { RecordsListClient } from "./RecordsListClient";

export const dynamic = "force-dynamic";

// Beta 1 Section B — Multi-tenant Settings → Records page.
// Lists every record the signed-in user has access to and lets
// them switch between them from one place. This is the durable
// management surface; the sidebar `RecordSwitcher` is the
// in-flight quick-switch.

export default async function RecordsSettingsPage() {
  const me = await getMe();
  const memberships = me?.memberships ?? [];
  const activeId = me?.active_record?.id ?? null;

  return (
    <div className="max-w-3xl">
      <p className="text-sm uppercase tracking-widest text-muted">
        <a href="/settings" className="hover:text-fg">
          Settings
        </a>{" "}
        / Records
      </p>
      <h1 className="mt-2 font-serif text-3xl">Records you can view</h1>
      <p className="mt-3 max-w-2xl text-muted">
        Each record is a separate person whose data OwnChart organizes
        for you. Your role on a record controls what you can do — owners
        can add or remove caregivers, caregivers can add data, viewers
        can read but not edit.
      </p>

      <RecordsListClient
        memberships={memberships}
        activeRecordId={activeId}
      />
    </div>
  );
}
