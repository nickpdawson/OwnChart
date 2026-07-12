import { getMe, listAdminUsers } from "@/lib/api";
import { UsersClient } from "./UsersClient";

export const dynamic = "force-dynamic";

export default async function AdminUsersPage() {
  const [me, list] = await Promise.all([
    getMe(),
    listAdminUsers({ limit: 200, sort: "created_at", order: "desc" }),
  ]);
  return (
    <UsersClient
      initial={list.users}
      total={list.total}
      selfId={me?.id ?? null}
    />
  );
}
