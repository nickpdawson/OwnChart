import { redirect } from "next/navigation";

// "Ask the records" and Chat were two doors to the same workflow (the
// Ask page only created a conversation and forwarded to /chat/<id>).
// One Ask destination now; saved links keep working.
export default function AskPage() {
  redirect("/chat");
}
