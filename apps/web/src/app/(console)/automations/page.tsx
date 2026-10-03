import Link from "next/link";
import { api } from "@/lib/api";
import { ist } from "@/lib/format";
import { Badge, Card, Mono, PageHeader, Table, Td } from "@/components/ui";
import { Field, RefreshLearnedButton, SettingsForm, inputCls } from "./forms";
import type { LearnedResponse, RiskThresholdSettings, SettingsResponse, Versioned } from "./types";

const ARMS = ["whatsapp", "voice"] as const;
const rupees = (paise: number) => (paise / 100).toLocaleString("en-IN", { maximumFractionDigits: 2 });
const pct = (p: number) => `${(p * 100).toFixed(1)}%`;

function Origin({ v }: { v: Versioned }) {
  return v.version === 0
    ? <Badge tone="neutral">platform default</Badge>
    : <span className="text-xs text-[var(--muted)]"><Badge tone="info">v{v.version}</Badge> {v.changed_by} · {ist(v.created_at)} · “{v.reason}”</span>;
}

function SourceBadge({ source }: { source: string }) {
  const tone = source.startsWith("learned") ? "good" : source.startsWith("fallback") ? "warn" : "neutral";
  return <Badge tone={tone}>{source}</Badge>;
}

export default async function AutomationsPage() {
  const [s, learned] = await Promise.all([api<SettingsResponse>("/v1/settings"), api<LearnedResponse>("/v1/learned")]);
  const ns = s.namespaces;
  const ch = ns.channels.value as { capacity_per_round: Record<string, number>; cost_minor: Record<string, number> };
  const rt = (ns.strategy.value as { risk_threshold: RiskThresholdSettings }).risk_threshold;
  const eff = ns.effects.value as { mode: string; prior_strength: number; min_per_group: number; prior: Record<string, Record<string, number>> };
  const exp = ns.experiments.value as { holdout_bp: number; holdout_opt_in: boolean };
  const pol = s.effective.policy;
  const base = s.policy_platform;
  const thrEv = learned.risk_threshold?.evidence;
  const effEv = learned.effects?.evidence;

  return (
    <>
      <PageHeader title="Automations & settings"
        subtitle="Every number the agents use is yours: versioned, audited, and — where marked learned — estimated from your own verified outcomes."
        right={<Link href="/automations/templates" className="rounded-md border border-[var(--border)] px-3 py-1.5 text-sm">Message templates →</Link>} />

      <div className="grid gap-4 xl:grid-cols-2">
        {/* ------------------------------------------------ what's in force now */}
        <Card title="In force right now" className="xl:col-span-2">
          <div className="grid gap-3 text-sm sm:grid-cols-2 lg:grid-cols-4">
            <div><div className="text-xs text-[var(--muted)]">M1 risk threshold</div>
              <div className="text-lg font-semibold tabular-nums">{s.effective.risk_threshold.toFixed(2)}</div>
              <SourceBadge source={s.effective.sources.risk_threshold} /></div>
            <div><div className="text-xs text-[var(--muted)]">Contact effects</div>
              <div className="text-lg font-semibold">{effEv ? `${effEv.outcomes_used} outcomes` : "—"}</div>
              <SourceBadge source={s.effective.sources.effects} /></div>
            <div><div className="text-xs text-[var(--muted)]">Capacity per round</div>
              <div className="text-lg font-semibold tabular-nums">WA {s.effective.capacity.whatsapp} · Voice {s.effective.capacity.voice}</div>
              <SourceBadge source={`channels ${s.effective.sources.channels}`} /></div>
            <div><div className="text-xs text-[var(--muted)]">Contact window</div>
              <div className="text-lg font-semibold tabular-nums">{pol.contact_window[0]}–{pol.contact_window[1]}</div>
              <SourceBadge source={`policy ${s.effective.sources.policy}`} /></div>
          </div>
        </Card>

        {/* ------------------------------------------------ channels */}
        <Card title="Channels: capacity and cost">
          <div className="mb-3"><Origin v={ns.channels} /></div>
          <SettingsForm namespace="channels" version={ns.channels.version} canEdit={s.can_edit}>
            <div className="grid gap-3 sm:grid-cols-2">
              {ARMS.map((a) => (
                <Field key={`cap-${a}`} label={`${a === "whatsapp" ? "WhatsApp" : "Voice"} contacts per round`}
                  hint={a === "voice" ? "Keep 0 until telephony is connected and your TRAI header is registered." : undefined}>
                  <input name={`capacity.${a}`} type="number" min={0} step={1} defaultValue={ch.capacity_per_round[a]} className={inputCls} />
                </Field>
              ))}
              {ARMS.map((a) => (
                <Field key={`cost-${a}`} label={`${a === "whatsapp" ? "WhatsApp" : "Voice"} cost per contact (₹)`}
                  hint={ns.channels.version === 0 ? "Placeholder — enter your provider's rate." : undefined}>
                  <input name={`cost.${a}`} type="number" min={0} step={0.01} defaultValue={rupees(ch.cost_minor[a]).replace(/,/g, "")} className={inputCls} />
                </Field>
              ))}
            </div>
          </SettingsForm>
        </Card>

        {/* ------------------------------------------------ strategy */}
        <Card title="Pre-debit risk threshold (Debit Strategist)">
          <div className="mb-3"><Origin v={ns.strategy} /></div>
          <SettingsForm namespace="strategy" version={ns.strategy.version} canEdit={s.can_edit}>
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="Mode" hint="Learned: chosen from your labelled outcomes to maximise value. Fixed: always the value below.">
                <select name="mode" defaultValue={rt.mode} className={inputCls}>
                  <option value="learned">learned</option><option value="fixed">fixed</option>
                </select>
              </Field>
              <Field label="Fixed / fallback threshold" hint="Used in fixed mode, and while there is too little evidence.">
                <input name="fixed" type="number" min={0.01} max={0.99} step={0.01} defaultValue={rt.fixed} className={inputCls} />
              </Field>
              <Field label="Cost of flagging a debit (₹)" hint="Enhanced notice / follow-up effort per flagged debit.">
                <input name="flag_cost" type="number" min={0} step={0.01} defaultValue={rt.flag_cost_minor / 100} className={inputCls} />
              </Field>
              <Field label="Share of a flagged failure prevented (%)" hint="Assumption until a pre-debit experiment measures it.">
                <input name="prevention_pct" type="number" min={0.1} max={100} step={0.1} defaultValue={+(rt.prevention_share * 100).toFixed(2)} className={inputCls} />
              </Field>
              <Field label="Minimum labelled debits before learning">
                <input name="min_labels" type="number" min={50} step={1} defaultValue={rt.min_labels} className={inputCls} />
              </Field>
            </div>
          </SettingsForm>
          {thrEv && (
            <p className="mt-3 text-xs text-[var(--muted)]">
              Last learning run (v{learned.risk_threshold!.version}, {ist(learned.risk_threshold!.created_at)}): {thrEv.labels} labelled debits, {thrEv.positives} failures.{" "}
              {thrEv.threshold !== undefined
                ? <>Best threshold {thrEv.threshold} · precision {thrEv.precision ?? "—"} · recall {thrEv.recall} · value ₹{rupees(thrEv.value_minor_at_threshold ?? 0)} vs ₹{rupees(thrEv.value_minor_at_fallback ?? 0)} at the fallback.</>
                : <>Not learned yet: {thrEv.why}.</>}
            </p>
          )}
        </Card>

        {/* ------------------------------------------------ effects */}
        <Card title="Contact effects (Contact Arbiter)" className="xl:col-span-2">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <Origin v={ns.effects} />
            <RefreshLearnedButton canEdit={s.can_edit} />
          </div>
          <p className="mb-3 text-xs text-[var(--muted)]">
            Incremental recovery probability of one contact, per failure category. Learned from your randomised holdout
            (intention-to-treat ÷ contact rate), shrunk towards your prior; categories with too little data keep the prior.
          </p>
          <SettingsForm namespace="effects" version={ns.effects.version} canEdit={s.can_edit}>
            <div className="grid gap-3 sm:grid-cols-3">
              <Field label="Mode"><select name="mode" defaultValue={eff.mode} className={inputCls}>
                <option value="learned">learned</option><option value="prior">prior only</option></select></Field>
              <Field label="Prior strength" hint="How many observations your prior is worth.">
                <input name="prior_strength" type="number" min={0} step={1} defaultValue={eff.prior_strength} className={inputCls} /></Field>
              <Field label="Minimum per group" hint="Treated and holdout outcomes needed per category.">
                <input name="min_per_group" type="number" min={10} step={1} defaultValue={eff.min_per_group} className={inputCls} /></Field>
            </div>
            <Table head={["Category", "Prior WhatsApp %", "Prior voice %", "In force WhatsApp", "In force voice", "Evidence"]}>
              {Object.keys(eff.prior).map((cat) => {
                const ev = effEv?.categories[cat];
                return (
                  <tr key={cat}>
                    <Td><Mono>{cat}</Mono></Td>
                    {ARMS.map((a) => (
                      <Td key={a}><input aria-label={`${cat} ${a} prior %`} name={`prior.${cat}.${a}`} type="number" min={0} max={100} step={0.1}
                        defaultValue={+(eff.prior[cat][a] * 100).toFixed(2)} className={`${inputCls} w-24`} /></Td>
                    ))}
                    {ARMS.map((a) => <Td key={`e-${a}`} className="tabular-nums">{pct(s.effective.effects[cat]?.[a] ?? 0)}</Td>)}
                    <Td className="text-xs text-[var(--muted)]">
                      {!ev ? "—" : ev.source === "learned"
                        ? <>treated {ev.treated} / holdout {ev.holdout} · recovery {pct(ev.recovery_treated!)} vs {pct(ev.recovery_holdout!)} · effect {pct(ev.cace!)} (95% CI {pct(ev.cace_ci95![0])}–{pct(ev.cace_ci95![1])})</>
                        : <>prior kept: {ev.treated} treated / {ev.holdout} holdout</>}
                    </Td>
                  </tr>
                );
              })}
            </Table>
          </SettingsForm>
        </Card>

        {/* ------------------------------------------------ experiments */}
        <Card title="Holdout (measuring what the agents add)">
          <div className="mb-3"><Origin v={ns.experiments} /></div>
          <SettingsForm namespace="experiments" version={ns.experiments.version} canEdit={s.can_edit}>
            <Field label="Holdout size for new experiments (%)" hint="Customers in the holdout get only mandatory messages. Lending/MFI: 0 unless you opt in.">
              <input name="holdout_pct" type="number" min={0} max={30} step={0.5} defaultValue={exp.holdout_bp / 100} className={inputCls} />
            </Field>
            <label className="flex items-center gap-2 text-sm">
              <input name="holdout_opt_in" type="checkbox" defaultChecked={exp.holdout_opt_in} /> Lending/MFI holdout opt-in
            </label>
          </SettingsForm>
        </Card>

        {/* ------------------------------------------------ policy */}
        <Card title="Compliance tightening">
          <div className="mb-3"><Origin v={ns.policy} /></div>
          <p className="mb-3 text-xs text-[var(--muted)]">
            You can only make these stricter than the platform baseline (regulation + governance, cited in Compliance).
            Baseline: window {base.contact_window[0]}–{base.contact_window[1]}, {base.max_contacts_7d} contacts/7 days, notice ≥ {base.predebit_notice_hours}h.
          </p>
          <SettingsForm namespace="policy" version={ns.policy.version} canEdit={s.can_edit}>
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="Contact window starts"><input name="window_start" type="time" defaultValue={pol.contact_window[0]} className={inputCls} /></Field>
              <Field label="Contact window ends"><input name="window_end" type="time" defaultValue={pol.contact_window[1]} className={inputCls} /></Field>
              <Field label="Max optional contacts per 7 days"><input name="max_contacts_7d" type="number" min={0} step={1} defaultValue={pol.max_contacts_7d} className={inputCls} /></Field>
              <Field label="Pre-debit notice (hours)"><input name="predebit_notice_hours" type="number" min={1} step={1} defaultValue={pol.predebit_notice_hours} className={inputCls} /></Field>
              <Field label="Refunds need approval above (₹)"><input name="refund_approval" type="number" min={0} step={1} defaultValue={pol.refund_approval_above_minor / 100} className={inputCls} /></Field>
              <Field label="Representments need approval above (₹)"><input name="representment_approval" type="number" min={0} step={1} defaultValue={pol.representment_approval_above_minor / 100} className={inputCls} /></Field>
              <Field label="Win-back discounts need approval above (₹)"><input name="discount_approval" type="number" min={0} step={1} defaultValue={pol.discount_approval_above_minor / 100} className={inputCls} /></Field>
            </div>
            <div className="space-y-1 text-sm">
              <label className="flex items-center gap-2"><input name="allow_debit_shift" type="checkbox" defaultChecked={pol.allow_debit_shift} /> Allow agents to propose debit-date shifts</label>
              <label className="flex items-center gap-2"><input name="voice_registered" type="checkbox" defaultChecked={pol.voice_registered} /> TRAI voice header registered (required for calls)</label>
              <label className="flex items-center gap-2"><input name="lending_collections_enabled" type="checkbox" defaultChecked={pol.lending_collections_enabled} /> Lending collections enabled (launch gate)</label>
            </div>
          </SettingsForm>
        </Card>
      </div>
    </>
  );
}
