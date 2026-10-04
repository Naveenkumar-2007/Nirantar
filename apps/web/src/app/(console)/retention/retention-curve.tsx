"use client";

import { useState } from "react";

export type CurvePoint = { t: number; survival: number; fitted: number; at_risk: number };

const W = 560, H = 220, PAD = { l: 40, r: 70, t: 12, b: 28 };

/** Observed (Kaplan–Meier) vs sBG-fitted share of subscribers still paying, by billing period.
 * One axis, two series (solid = observed, dashed = fitted), legend + direct end labels, hover crosshair. */
export function RetentionCurve({ points }: { points: CurvePoint[] }) {
  const [hover, setHover] = useState<number | null>(null);
  if (points.length < 2) return <p className="text-sm text-muted-foreground">Not enough periods to draw a curve.</p>;
  const maxT = points[points.length - 1].t;
  const x = (t: number) => PAD.l + ((t - 1) / Math.max(1, maxT - 1)) * (W - PAD.l - PAD.r);
  const y = (v: number) => PAD.t + (1 - v) * (H - PAD.t - PAD.b);
  const path = (k: "survival" | "fitted") => points.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p[k]).toFixed(1)}`).join("");
  const last = points[points.length - 1];
  const h = hover === null ? null : points[hover];
  return (
    <div>
      <div className="mb-2 flex gap-4 text-xs text-muted-foreground" aria-hidden="true">
        <span className="flex items-center gap-1"><svg width="18" height="6"><line x1="0" y1="3" x2="18" y2="3" stroke="var(--series-1)" strokeWidth="2" /></svg>Observed (Kaplan–Meier)</span>
        <span className="flex items-center gap-1"><svg width="18" height="6"><line x1="0" y1="3" x2="18" y2="3" stroke="var(--series-2)" strokeWidth="2" strokeDasharray="4 3" /></svg>Fitted (sBG)</span>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img"
        aria-label={`Share of subscribers still paying by billing period, observed vs fitted, ${points.length} periods`}
        onMouseLeave={() => setHover(null)}
        onMouseMove={(e) => {
          const r = (e.currentTarget as SVGSVGElement).getBoundingClientRect();
          const px = ((e.clientX - r.left) / r.width) * W;
          let best = 0;
          points.forEach((p, i) => { if (Math.abs(x(p.t) - px) < Math.abs(x(points[best].t) - px)) best = i; });
          setHover(best);
        }}>
        {[0, 0.25, 0.5, 0.75, 1].map((v) => (
          <g key={v}>
            <line x1={PAD.l} x2={W - PAD.r} y1={y(v)} y2={y(v)} stroke="var(--grid)" strokeWidth="1" />
            <text x={PAD.l - 6} y={y(v) + 4} textAnchor="end" fontSize="10" fill="var(--muted-foreground)">{Math.round(v * 100)}%</text>
          </g>
        ))}
        {points.filter((p) => p.t === 1 || p.t % Math.max(1, Math.ceil(maxT / 6)) === 0).map((p) => (
          <text key={p.t} x={x(p.t)} y={H - 8} textAnchor="middle" fontSize="10" fill="var(--muted-foreground)">{p.t}</text>
        ))}
        <text x={(W - PAD.r + PAD.l) / 2} y={H} textAnchor="middle" fontSize="10" fill="var(--muted-foreground)">billing period</text>
        <path d={path("survival")} fill="none" stroke="var(--series-1)" strokeWidth="2" strokeLinejoin="round" />
        <path d={path("fitted")} fill="none" stroke="var(--series-2)" strokeWidth="2" strokeDasharray="5 4" strokeLinejoin="round" />
        <text x={x(last.t) + 6} y={y(last.survival) + 4} fontSize="10" fill="var(--foreground)">Observed {Math.round(last.survival * 100)}%</text>
        <text x={x(last.t) + 6} y={y(last.fitted) + (last.fitted < last.survival ? 14 : -6)} fontSize="10" fill="var(--muted-foreground)">Fitted {Math.round(last.fitted * 100)}%</text>
        {h && (
          <g>
            <line x1={x(h.t)} x2={x(h.t)} y1={PAD.t} y2={H - PAD.b} stroke="var(--muted-foreground)" strokeWidth="1" strokeDasharray="2 2" />
            <circle cx={x(h.t)} cy={y(h.survival)} r="4" fill="var(--series-1)" stroke="var(--card)" strokeWidth="2" />
            <circle cx={x(h.t)} cy={y(h.fitted)} r="4" fill="var(--series-2)" stroke="var(--card)" strokeWidth="2" />
          </g>
        )}
      </svg>
      <p className="min-h-5 text-xs text-foreground" aria-live="polite">
        {h ? `Period ${h.t}: observed ${(h.survival * 100).toFixed(1)}%, fitted ${(h.fitted * 100).toFixed(1)}% · ${h.at_risk.toLocaleString("en-IN")} subscribers at risk`
          : "Hover the chart to read a period."}
      </p>
      <details className="mt-1 text-xs text-muted-foreground">
        <summary className="cursor-pointer">Table view</summary>
        <table className="mt-2 w-full text-left tabular-nums">
          <thead><tr><th className="py-1">Period</th><th>Observed</th><th>Fitted</th><th>At risk</th></tr></thead>
          <tbody>{points.map((p) => (
            <tr key={p.t}><td className="py-0.5">{p.t}</td><td>{(p.survival * 100).toFixed(1)}%</td><td>{(p.fitted * 100).toFixed(1)}%</td><td>{p.at_risk.toLocaleString("en-IN")}</td></tr>
          ))}</tbody>
        </table>
      </details>
    </div>
  );
}
