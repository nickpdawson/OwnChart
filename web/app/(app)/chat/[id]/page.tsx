import Link from "next/link";
import { notFound } from "next/navigation";
import {
  getConversation,
  listConversationCandidates,
  listProviders,
} from "@/lib/api";
import { ThreadClient } from "./ThreadClient";

export const dynamic = "force-dynamic";

type Params = { id: string };

export default async function ChatThreadPage({
  params,
}: {
  params: Promise<Params>;
}) {
  const { id } = await params;
  let thread;
  try {
    thread = await getConversation(id);
  } catch (e) {
    if ((e as Error).message.includes("404")) notFound();
    throw e;
  }
  const [providers, candidates] = await Promise.all([
    listProviders().catch(() => []),
    listConversationCandidates(id).catch(() => []),
  ]);

  return (
    <div className="max-w-4xl">
      <p className="text-sm uppercase tracking-widest text-muted">
        <Link href="/chat" className="hover:underline">
          Chat
        </Link>{" "}
        · {thread.kind}
      </p>
      <h1 className="mt-2 font-serif text-2xl">
        {thread.title ?? "(untitled thread)"}
      </h1>
      {thread.provider && (
        <p className="mt-1 text-xs text-muted">
          via {thread.provider} · {thread.model ?? "?"}
          {thread.privacy_mode && ` · privacy ${thread.privacy_mode}`}
        </p>
      )}
      <ThreadClient
        thread={thread}
        providers={providers}
        candidates={candidates}
      />
    </div>
  );
}
