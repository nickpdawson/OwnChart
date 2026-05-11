import { getHomeAiPartner, listConversations, listProviders } from "@/lib/api";
import { ChatClient } from "./ChatClient";

export const dynamic = "force-dynamic";

// docs/10 — saved searchable conversations. The /chat page lists every
// thread and lets the user start a new one with optional scope +
// provider override. Individual threads live at /chat/[id].

export default async function ChatPage() {
  const [conversations, providers, partner] = await Promise.all([
    listConversations().catch(() => []),
    listProviders().catch(() => []),
    getHomeAiPartner().catch(() => null),
  ]);

  return (
    <div className="max-w-5xl">
      <p className="text-sm uppercase tracking-widest text-muted">AI partner</p>
      <h1 className="mt-2 font-serif text-3xl">Chat</h1>
      <p className="mt-3 max-w-2xl text-muted">
        Every conversation is saved, searchable, evidence-linked, and
        replayable. Click a suggested question to start a thread, or
        type your own below.
      </p>

      <ChatClient
        initialConversations={conversations}
        providers={providers}
        suggestedQuestions={partner?.suggested_questions ?? []}
      />
    </div>
  );
}
