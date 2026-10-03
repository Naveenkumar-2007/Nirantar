import { api } from "@/lib/api";
import { ist } from "@/lib/format";
import { Badge, Card, Empty, PageHeader, Table, Td } from "@/components/ui";
import { revokeInvite } from "./actions";
import { InviteForm } from "./invite-form";

type Team = {
  members: { user_sub: string; email: string | null; name: string | null; roles: string[]; status: string; created_at: string }[];
  invites: { invite_id: string; roles: string[]; invited_by: string; created_at: string; expires_at: string }[];
};

export default async function TeamPage() {
  const team = await api<Team>("/v1/team");
  return (
    <>
      <PageHeader title="Team" subtitle="Who can see and act in this business. Roles are enforced by the API on every request." />
      <div className="space-y-4">
        <Card title="Invite someone"><InviteForm /></Card>
        <Card title="Members">
          {team.members.length === 0 ? <Empty title="No signed-in members yet" /> : (
            <Table head={["Name", "Email", "Role", "Since", "Status"]}>
              {team.members.map((m) => (
                <tr key={m.user_sub}>
                  <Td>{m.name ?? "—"}</Td>
                  <Td>{m.email ?? "—"}</Td>
                  <Td>{m.roles.map((r) => <Badge key={r}>{r.replaceAll("_", " ")}</Badge>)}</Td>
                  <Td>{ist(m.created_at)}</Td>
                  <Td><Badge tone={m.status === "active" ? "good" : "neutral"}>{m.status}</Badge></Td>
                </tr>
              ))}
            </Table>
          )}
        </Card>
        {team.invites.length > 0 && (
          <Card title="Pending invites">
            <Table head={["Role", "Invited", "Expires", ""]}>
              {team.invites.map((i) => (
                <tr key={i.invite_id}>
                  <Td>{i.roles.join(", ").replaceAll("_", " ")}</Td>
                  <Td>{ist(i.created_at)}</Td>
                  <Td>{ist(i.expires_at)}</Td>
                  <Td>
                    <form action={revokeInvite}>
                      <input type="hidden" name="invite_id" value={i.invite_id} />
                      <button className="text-xs text-[var(--muted)] hover:text-[var(--bad-fg)]">Revoke</button>
                    </form>
                  </Td>
                </tr>
              ))}
            </Table>
            <p className="mt-2 text-xs text-[var(--muted)]">For privacy, invite emails are stored only as a hash, so they aren&apos;t shown here.</p>
          </Card>
        )}
      </div>
    </>
  );
}
