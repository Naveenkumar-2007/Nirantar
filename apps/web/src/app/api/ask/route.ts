import { api } from "@/lib/api";

/** Proxies the policy assistant so the API key never reaches the browser. */
export async function POST(req: Request) {
  let question = "";
  try {
    question = String((await req.json()).question ?? "").slice(0, 500);
  } catch {
    return Response.json({ error: "invalid body" }, { status: 400 });
  }
  if (question.trim().length < 5) return Response.json({ error: "question too short" }, { status: 400 });
  try {
    return Response.json(await api("/v1/assistant/ask", { method: "POST", body: { question } }));
  } catch (e) {
    return Response.json({ error: e instanceof Error ? e.message : "failed" }, { status: 502 });
  }
}
