import { redirect } from "next/navigation";
import { getMe } from "@/lib/api";

export default async function OnboardingLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const me = await getMe();
  if (!me) redirect("/login");
  if (me.phi_consent_granted) redirect("/dashboard");
  return <>{children}</>;
}
