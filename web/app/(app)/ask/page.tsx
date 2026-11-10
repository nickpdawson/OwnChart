import { getMe } from "@/lib/api";
import { AiConsentNotice } from "../AiConsentNotice";
import { AskClient } from "./AskClient";

export const dynamic = "force-dynamic";

export default async function AskPage() {
  const me = await getMe();
  const aiEnabled = me?.phi_consent_granted === true;
  return (
    <div className="max-w-3xl">
      <p className="text-sm uppercase tracking-widest text-muted">Ask the records</p>
      <h1 className="mt-2 font-serif text-3xl">Start a conversation</h1>
      <p className="mt-3 max-w-2xl text-muted">
        Answers link to the records they draw from so you can check them.
        When the evidence is not there, the answer says what was searched
        and what was missing. The thread is saved so you can come back to it.
      </p>
      {aiEnabled ? (
        <AskClient />
      ) : (
        <div className="mt-6">
          <AiConsentNotice feature="Asking questions about your records" />
        </div>
      )}
    </div>
  );
}
