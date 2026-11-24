"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { catColor, catLabel, Stacked } from "@/components/Bars";
import Icon from "@/components/Icon";
import ProgressCard from "@/components/ProgressCard";
import StatusBadge from "@/components/StatusBadge";
import { api, compact, num, pct, ROLE_COLORS, secs, shortModel, usd, type BatchDetail } from "@/lib/api";

const ROLES = ["agent", "customer", "judge"] as const;

export default function BatchPage() {
  const { id } = useParams<{ id: string }>();
  const [b, setB] = useState<BatchDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => api<BatchDetail>(`/batches/${id}`).then(setB).catch((e) => setError(String(e))), [id]);
  useEffect(() => {
    load();
    const h = setInterval(load, 1500);
    return () => clearInterval(h);
  }, [load]);

  async function retry() {
    setBusy(true);
    try {
      await api(`/batches/${id}/retry-errors`, { method: "POST" });
      await load();
    } finally {
      setBusy(false);
    }
  }

  if (error) return <div className="banner bad">{error}</div>;
  if (!b) return <p className="muted">Loading…</p>;

  const maxTok = Math.max(1, ...b.episodes.map((e) => e.input + e.output));
  const live = b.finished < b.total;

  return (
    <>
      <p className="small"><Link href="/tasks">← all batches</Link></p>
      <div className="page-head">
        <div>
          <h1>Batch <code>{b.batch_id}</code></h1>
          <p className="muted small" style={{ margin: 0 }}>
            agent {shortModel(b.agent_model)} · variant {b.variant_label} · customer {shortModel(b.user_model)} · {b.finished}/{b.total} episodes
          </p>
        </div>
        <div className="row">
          {b.errors > 0 && <button disabled={busy || live} onClick={retry}><Icon name="refresh" size={14} /> Retry {b.errors} errored</button>}
          <Link className="btn" href={`/analysis?batch_id=${b.batch_id}`}><Icon name="bug" size={14} /> Failure analysis</Link>
        </div>
      </div>

      {live && <ProgressCard title="Running batch" p={b.progress} maxRows={12} />}

      {b.rate_limited > 0 && (
        <div className="banner run">
          {b.rate_limited} episode{b.rate_limited > 1 ? "s" : ""} failed on rate limits (HTTP 429). Set the provider&apos;s RPM/TPM limits in <code>.env</code> so requests are paced, then press Retry.
        </div>
      )}

      <div className="stats" style={{ marginBottom: 18 }}>
        <div className="stat accent"><div className="l">pass^1</div><div className="v">{pct(b.pass_hat_k["1"])}</div>
          <div className="sub">{Object.entries(b.pass_hat_k).slice(1).map(([k, v]) => `pass^${k} ${pct(v)}`).join(" · ") || `${b.passes} of ${b.finished} passed`}</div></div>
        <div className="stat"><div className="l">Input tokens / episode</div><div className="v">{compact(b.avg_input)}</div><div className="sub">{compact(b.total_input)} total</div></div>
        <div className="stat"><div className="l">Output tokens / episode</div><div className="v">{compact(b.avg_output)}</div><div className="sub">{compact(b.total_output)} total</div></div>
        <div className="stat"><div className="l">Cost</div><div className="v">{usd(b.total_cost_usd)}</div><div className="sub">{usd(b.avg_cost_usd)} per episode</div></div>
        <div className="stat"><div className="l">Waiting on rate limits</div><div className="v" style={{ color: b.total_wait_ms ? "var(--warn)" : undefined }}>{secs(b.total_wait_ms)}</div><div className="sub">{b.errors} errors ({b.rate_limited} rate-limited)</div></div>
      </div>

      <div className="grid2">
        <div className="panel">
          <div className="panel-head"><h2>Input vs output by role</h2><span className="small faint">whole batch</span></div>
          <table>
            <thead><tr><th>role</th><th className="num">calls</th><th className="num">input</th><th className="num">output</th><th style={{ width: "40%" }}>in / out</th></tr></thead>
            <tbody>
              {ROLES.map((r) => {
                const t = b.role_totals[r];
                const tot = t.input + t.output;
                return (
                  <tr key={r}>
                    <td><i className="role-dot" style={{ background: ROLE_COLORS[r] }} />{r}</td>
                    <td className="num">{num(t.calls)}</td>
                    <td className="num">{compact(t.input)}</td>
                    <td className="num">{compact(t.output)}</td>
                    <td>
                      <div className="stack" title={`${num(t.input)} in / ${num(t.output)} out`}>
                        <span style={{ width: `${(t.input / Math.max(tot, 1)) * 100}%`, background: ROLE_COLORS[r] }} />
                        <span style={{ width: `${(t.output / Math.max(tot, 1)) * 100}%`, background: "#e7e9ee" }} />
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <p className="small faint" style={{ marginBottom: 0 }}>Colored part = input (prompt), white = output (generated, including reasoning).</p>
        </div>
        <div className="panel">
          <div className="panel-head"><h2>Failure causes</h2></div>
          <Stacked data={b.failure_categories} />
        </div>
      </div>

      <div className="panel">
        <div className="panel-head"><h2>Every episode</h2><span className="small faint">input / output tokens per role, per trial</span></div>
        <div className="scroll">
          <table className="trace-table">
            <thead>
              <tr>
                <th>task</th><th>trial</th><th>result</th>
                {ROLES.map((r) => <th key={r} className="num" style={{ color: ROLE_COLORS[r] }}>{r} in / out</th>)}
                <th style={{ width: 160 }}>total</th><th className="num">cost</th><th className="num">waited</th><th className="num">time</th>
              </tr>
            </thead>
            <tbody>
              {b.episodes.map((e) => (
                <tr key={e.id}>
                  <td><Link href={`/runs/${e.id}`}>{e.title ?? e.task_id}</Link>
                    {e.category && <div className="small" style={{ color: catColor(e.category) }}>{catLabel(e.category)}</div>}
                    {e.error && <div className="small" style={{ color: e.rate_limited ? "var(--warn)" : "var(--bad)", maxWidth: 360, whiteSpace: "normal" }}>{e.error.split("\n")[0]}</div>}
                  </td>
                  <td className="faint">{e.trial + 1}</td>
                  <td><StatusBadge run={e} /></td>
                  {ROLES.map((r) => {
                    const t = e.tokens[r];
                    return <td key={r} className="num">{t ? <>{compact(t.input)} <span className="faint">/</span> {compact(t.output)}</> : <span className="faint">–</span>}</td>;
                  })}
                  <td>
                    <span className="ctx-track" style={{ width: "100%" }} title={`${num(e.input)} in / ${num(e.output)} out`}>
                      <span style={{ width: `${((e.input + e.output) / maxTok) * 100}%`, display: "flex" }}>
                        <span style={{ width: `${(e.input / Math.max(e.input + e.output, 1)) * 100}%`, background: ROLE_COLORS.agent, borderRadius: 0 }} />
                        <span style={{ flex: 1, background: "#e7e9ee", borderRadius: 0 }} />
                      </span>
                    </span>
                    <div className="small faint">{compact(e.input)} in · {compact(e.output)} out</div>
                  </td>
                  <td className="num">{usd(e.cost_usd)}</td>
                  <td className="num" style={{ color: e.wait_ms ? "var(--warn)" : undefined }}>{e.wait_ms ? secs(e.wait_ms) : "–"}</td>
                  <td className="num faint">{e.duration_s != null ? `${num(e.duration_s, 1)}s` : "–"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}
