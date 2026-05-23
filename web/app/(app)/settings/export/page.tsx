import { getMe, listExports, type ExportJob } from "@/lib/api";
import { ExportClient } from "./ExportClient";

export const dynamic = "force-dynamic";

// Section D — Export / Portability page.
//
// The active record is shown in the page header so the user
// can confirm which record they're exporting from before
// initiating a PHI egress. The actual create + list +
// download + delete affordances live in ExportClient (writes
// require credentials and same-origin POSTs).

export default async function ExportSettingsPage() {
  const [me, initial] = await Promise.all([
    getMe(),
    listExports().catch(() => [] as ExportJob[]),
  ]);
  const activeRecordName = me?.active_record?.display_name ?? null;
  const activeRecordRole = me?.active_record?.role ?? null;

  return (
    <div className="max-w-3xl">
      <p className="text-sm uppercase tracking-widest text-muted">
        <a href="/settings" className="hover:text-fg">
          Settings
        </a>{" "}
        / Export &amp; Portability
      </p>
      <h1 className="mt-2 font-serif text-3xl">Export this record</h1>
      <p className="mt-3 max-w-2xl text-muted">
        Download a copy of what OwnChart has stored for{" "}
        {activeRecordName ? (
          <>
            <strong className="text-ink">{activeRecordName}</strong>{" "}
            (role: {activeRecordRole}).
          </>
        ) : (
          "this record."
        )}{" "}
        Exports are PHI &mdash; treat the resulting files as
        sensitive. The link to each completed file expires after
        72 hours, after which OwnChart deletes the file from disk.
      </p>

      <div className="mt-4 rounded-md border border-caution/30 bg-caution/5 p-4 text-sm text-caution">
        <p className="font-medium">A few honest disclosures:</p>
        <ul className="mt-2 list-disc space-y-1 pl-5">
          <li>
            This export is <strong>not a complete medical record</strong>{" "}
            and <strong>not a legal document</strong>. It&rsquo;s the
            data this OwnChart instance has ingested, in the form
            OwnChart stores it.
          </li>
          <li>
            Body-signal (HealthKit / Auto Export) data is exported{" "}
            <strong>row by row</strong>, so an instance with years
            of continuous data may produce very large files. Aggregation
            is a follow-up.
          </li>
          <li>
            Large exports may take several minutes. Please leave this
            page open while the export runs; navigating away won&rsquo;t
            cancel the job server-side, but the in-page progress will
            reset.
          </li>
        </ul>
      </div>

      <ExportClient initialJobs={initial} />
    </div>
  );
}
