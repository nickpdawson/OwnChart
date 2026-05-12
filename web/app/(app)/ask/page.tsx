import { AskClient } from "./AskClient";

export default function AskPage() {
  return (
    <div className="max-w-3xl">
      <p className="text-sm uppercase tracking-widest text-muted">Ask the records</p>
      <h1 className="mt-2 font-serif text-3xl">Start a conversation</h1>
      <p className="mt-3 max-w-2xl text-muted">
        Every answer cites the specific facts it draws from. If the evidence
        isn&apos;t there, the answer says so — never invented. The thread
        is saved automatically so you can come back to it, save it as a
        Dossier, or promote a specific event to an Episode.
      </p>
      <AskClient />
    </div>
  );
}
