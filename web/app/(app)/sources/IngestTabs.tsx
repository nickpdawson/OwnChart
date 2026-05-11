"use client";

import { useState } from "react";
import { AutoExportUploader } from "./AutoExportUploader";
import { CcdaUploader } from "./CcdaUploader";
import { NoteUploader } from "./NoteUploader";
import { PdfUploader } from "./PdfUploader";
import { PhotoUploader } from "./PhotoUploader";

const TABS = ["Photo", "PDF", "CCDA", "Note", "Auto Export"] as const;
type Tab = (typeof TABS)[number];

export function IngestTabs() {
  const [tab, setTab] = useState<Tab>("Photo");
  return (
    <div className="rounded-xl border border-muted/15 bg-surface p-4">
      <div className="flex flex-wrap gap-1 border-b border-muted/10">
        {TABS.map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => setTab(t)}
            className={`-mb-px border-b-2 px-3 py-2 text-sm ${
              tab === t ? "border-accent text-ink" : "border-transparent text-muted hover:text-ink"
            }`}
          >
            {t}
          </button>
        ))}
      </div>
      <div className="pt-4">
        {tab === "Photo" && <PhotoUploader />}
        {tab === "PDF" && <PdfUploader />}
        {tab === "CCDA" && <CcdaUploader />}
        {tab === "Note" && <NoteUploader />}
        {tab === "Auto Export" && <AutoExportUploader />}
      </div>
    </div>
  );
}
