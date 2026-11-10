import { redirect } from "next/navigation";
import { getMe } from "@/lib/api";

export default async function Home() {
  const me = await getMe();
  if (!me) redirect("/login");
  redirect("/dashboard");
}
