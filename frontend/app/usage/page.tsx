"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { Bars } from "@/components/Bars";
import { api, compact, num, priceLabel, ROLE_COLORS, shortModel, usd, type Usage } from "@/lib/api";

const RANGES = [1, 7, 30, 90];

export default function UsagePage() {
  const [days, setDays] = useState(30);
  const [u, setU] = useState<Usage | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const load = () => api<Usage>(`/usage?days=${days}`).then(setU).catch((e) => setError(String(e)));
    load();
    const h = setInterval(load, 8000);
    return () => clearInterval(h);
  }, [days]);

  const t = u?.total;
  const maxDay = Math.max(1, ...(u?.by_day ?? []).map((d) => d.agent + d.customer + d.judge));
  const tokensByRole = Object.fromEntries(Object.entries(u?.by_role ?? {}).map(([k, v]) => [k, v.tokens]));

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Token usage</h1>
          <p className="muted" style={{ margin: 0 }}>Every LLM call from the agent, the simulated customer and the failure judge, with an estimated cost.</p>
        </div>
        <div className="seg">
          {RANGES.map((d) => <button key={d} className={d === days ? "on" : ""} onClick={() => setDays(d)}>{d === 1 ? "24h" : `${d}d`}</button>)}
        </div>
      </div>
      {error && <div className="banner bad">{error}</div>}

      <div className="stats" style={{ marginBottom: 18 }}>
        <div className="stat accent"><div className="l">Estimated spend</div><div className="v">{usd(t?.cost_usd)}</div><div className="sub">last {days === 1 ? "24 hours" : `${days} days`}</div></div>
        <div className="stat"><div className="l">Total tokens</div><div className="v">{compact(t ? t.input + t.output : null)}</div><div className="sub">{num(t?.calls)} LLM calls</div></div>
        <div className="stat"><div className="l">Input / output</div><div className="v" style={{ fontSize: 20, paddingTop: 4 }}>{compact(t?.input)} / {compact(t?.output)}</div><div className="sub">{t && t.input + t.output ? `${((t.input / (t.input + t.output)) * 100).toFixed(0)}% of tokens are prompt` : ""}</div></div>
        <div className="stat"><div className="l">Cached input</div><div className="v">{compact(t?.cached)}</div><div className="sub">{t?.input ? `${((t.cached / t.input) * 100).toFixed(0)}% of input` : ""}</div></div>
        <div className="stat"><div className="l">Reasoning tokens</div><div className="v">{compact(t?.reasoning)}</div><div className="sub">thinking models only</div></div>
      </div>

      {t && t.unpriced_models.length > 0 && (
        <div className="banner run small">No price for {t.unpriced_models.map(shortModel).join(", ")}: tokens are counted but cost isn&apos;t. Add prices to <code>backend/app/data/pricing.json</code>.</div>
      )}

      <div className="grid2">
        <div className="panel">
          <div className="panel-head">
            <h2>Tokens per day</h2>
            <span className="legend small" style={{ margin: 0 }}>
              {(["agent", "customer", "judge"] as const).map((r) => <span key={r}><i style={{ background: ROLE_COLORS[r] }} />{r}</span>)}
            </span>
          </div>
          {!u?.by_day.length ? <div className="empty"><b>No usage yet</b>Run a task or chat in the playground.</div> : (
            <div className="cols">
              {u.by_day.map((d) => {
                const tot = d.agent + d.customer + d.judge;
                return (
                  <div key={d.day} className="col" title={`${d.day}: ${num(tot)} tokens · ${usd(d.cost_usd)}`}>
                    <span className="small faint">{compact(tot)}</span>
                    <span className="stackcol" style={{ height: `${(tot / maxDay) * 120}px` }}>
                      {(["agent", "customer", "judge"] as const).map((r) => <span key={r} style={{ height: `${(d[r] / Math.max(tot, 1)) * 100}%`, background: ROLE_COLORS[r] }} />)}
                    </span>
                    <span className="spark-lbl">{d.day.slice(5)}</span>
                  </div>
                );
              })}
            </div>
          )}
        </div>
        <div className="panel">
          <div className="panel-head"><h2>By role</h2></div>
          <Bars data={tokensByRole} color={(k) => ROLE_COLORS[k] ?? "#888"} format={compact} />
          <table style={{ marginTop: 14 }}>
            <tbody>
              {Object.entries(u?.by_role ?? {}).map(([r, v]) => (
                <tr key={r}><td>{r}</td><td className="num small">{num(v.calls)} calls</td><td className="num small">{usd(v.cost_usd)}</td></tr>
              ))}
              {Object.entries(u?.by_kind ?? {}).map(([k, v]) => (
                <tr key={k}><td className="faint">{k === "eval" ? "benchmark episodes" : "playground sessions"}</td><td className="num small">{num(v.runs)} runs</td><td className="num small">{usd(v.cost_usd)}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="panel">
        <div className="panel-head"><h2>By model</h2><span className="small faint">list prices per 1M tokens (input / output); costs shown in USD</span></div>
        {!u?.by_model.length ? <p className="muted small">No calls yet.</p> : (
          <div className="scroll">
            <table>
              <thead>
                <tr><th>model</th><th>price</th><th className="num">calls</th><th className="num">input</th><th className="num">output</th><th className="num">cached</th><th className="num">reasoning</th><th>split by role</th><th className="num">cost</th></tr>
              </thead>
              <tbody>
                {u.by_model.map((m) => {
                  const tot = m.input + m.output;
                  return (
                    <tr key={m.model}>
                      <td><b>{shortModel(m.model)}</b><div className="small faint">{m.model.split(":")[0]}</div></td>
                      <td className="small">{m.price ? priceLabel(m.price) : <span className="faint">unknown</span>}</td>
                      <td className="num">{num(m.calls)}</td>
                      <td className="num">{compact(m.input)}</td>
                      <td className="num">{compact(m.output)}</td>
                      <td className="num faint">{compact(m.cached)}</td>
                      <td className="num faint">{compact(m.reasoning)}</td>
                      <td style={{ minWidth: 180 }}>
                        <div className="stack">
                          {Object.entries(m.by_role).map(([r, v]) => <span key={r} title={`${r}: ${num(v.tokens)} tokens, ${num(v.avg_latency_ms)}ms avg`} style={{ width: `${(v.tokens / Math.max(tot, 1)) * 100}%`, background: ROLE_COLORS[r] }} />)}
                        </div>
                      </td>
                      <td className="num"><b>{usd(m.cost_usd)}</b></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="panel">
        <div className="panel-head"><h2>Heaviest runs</h2><span className="small faint">by agent tokens</span></div>
        {!u?.top_runs.length ? <p className="muted small">No runs yet.</p> : (
          <table>
            <thead><tr><th>run</th><th>model / variant</th><th className="num">agent</th><th className="num">customer</th><th className="num">judge</th><th className="num">cost</th><th>result</th></tr></thead>
            <tbody>
              {u.top_runs.map((r) => (
                <tr key={r.id}>
                  <td><Link href={r.kind === "eval" ? `/runs/${r.id}` : "/playground"}>{r.task_id ?? "playground session"}</Link><div className="small faint"><code>{r.id.slice(0, 8)}</code></div></td>
                  <td className="small">{shortModel(r.agent_model)}<div className="faint">{r.variant_label}</div></td>
                  <td className="num">{compact((r.input_tokens ?? 0) + (r.output_tokens ?? 0))}</td>
                  <td className="num faint">{compact(r.customer_tokens)}</td>
                  <td className="num faint">{compact(r.judge_tokens)}</td>
                  <td className="num">{usd(r.cost_usd)}</td>
                  <td>{r.kind !== "eval" ? <span className="badge">chat</span> : r.reward === 1 ? <span className="badge ok">pass</span> : <span className="badge bad">fail</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
