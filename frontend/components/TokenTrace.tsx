"use client";

import { Fragment, useState } from "react";
import { compact, num, ROLE_COLORS, secs, shortModel, usd, type Trace } from "@/lib/api";

const ROLE_LABEL: Record<string, string> = { agent: "Agent", customer: "Customer sim", judge: "Judge" };

/** Per-episode token trace: totals by role, tokens per turn, and every LLM call. */
export default function TokenTrace({ trace, compactMode = false }: { trace: Trace | null; compactMode?: boolean }) {
  const [showAll, setShowAll] = useState(false);
  if (!trace || trace.calls.length === 0)
    return <p className="small muted" style={{ margin: 0 }}>No LLM calls recorded yet.</p>;

  const roles = (["agent", "customer", "judge"] as const).filter((r) => trace.by_role[r]);
  const maxTurn = Math.max(1, ...trace.by_turn.map((t) => t.agent_tokens + t.customer_tokens));
  const maxIn = Math.max(1, ...trace.calls.map((c) => c.input_tokens));
  const calls = showAll || compactMode ? trace.calls : trace.calls.slice(0, 40);

  return (
    <div>
      <div className="stats" style={{ gridTemplateColumns: `repeat(${compactMode ? 2 : roles.length + 1}, minmax(0, 1fr))`, marginBottom: 14 }}>
        <div className="stat accent">
          <div className="l">Total</div>
          <div className="v" style={{ fontSize: 20 }}>{compact(trace.total.input + trace.total.output)}</div>
          <div className="sub">{compact(trace.total.input)} in · {compact(trace.total.output)} out · {trace.total.calls} calls · {usd(trace.total.cost_usd)}
            {trace.total.wait_ms > 0 && <> · <span style={{ color: "var(--warn)" }}>{secs(trace.total.wait_ms)} rate-limited</span></>}</div>
        </div>
        {roles.map((r) => {
          const t = trace.by_role[r]!;
          return (
            <div key={r} className="stat">
              <div className="l"><i className="role-dot" style={{ background: ROLE_COLORS[r] }} />{ROLE_LABEL[r]}</div>
              <div className="v" style={{ fontSize: 20 }}>{compact(t.input + t.output)}</div>
              <div className="sub">
                {compact(t.input)} in · {compact(t.output)} out{t.reasoning ? ` (${compact(t.reasoning)} reasoning)` : ""}
                {t.cached ? ` · ${compact(t.cached)} cached` : ""} · {usd(t.cost_usd)}
              </div>
            </div>
          );
        })}
      </div>

      {trace.by_turn.length > 0 && (
        <>
          <div className="small faint" style={{ marginBottom: 6 }}>Tokens per customer turn</div>
          <div className="turn-bars">
            {trace.by_turn.map((t) => (
              <div key={t.turn} className="turn-bar-row" title={`turn ${t.turn}: agent ${t.agent_tokens} (${t.agent_calls} calls), customer ${t.customer_tokens}, ${usd(t.cost_usd)}`}>
                <span className="faint">T{t.turn}</span>
                <span className="turn-track">
                  <span style={{ width: `${(t.agent_tokens / maxTurn) * 100}%`, background: ROLE_COLORS.agent }} />
                  <span style={{ width: `${(t.customer_tokens / maxTurn) * 100}%`, background: ROLE_COLORS.customer }} />
                </span>
                <span className="num">{compact(t.agent_tokens + t.customer_tokens)}</span>
                {!compactMode && <span className="num faint">{t.agent_calls} agent call{t.agent_calls === 1 ? "" : "s"}</span>}
              </div>
            ))}
          </div>
        </>
      )}

      {!compactMode && (
        <>
          <div className="small faint" style={{ margin: "16px 0 6px" }}>Every LLM call. The bar shows prompt size, which grows as the conversation gets longer.</div>
          <div className="scroll">
            <table className="trace-table">
              <thead>
                <tr>
                  <th>#</th><th>turn</th><th>role</th><th>model</th><th style={{ width: "26%" }}>prompt</th>
                  <th className="num">in</th><th className="num">out</th><th className="num">cached</th><th className="num">reasoning</th>
                  <th className="num">tools</th><th className="num">latency</th><th className="num">waited</th><th className="num">cost</th>
                </tr>
              </thead>
              <tbody>
                {calls.map((c) => (
                  <Fragment key={c.id}>
                    <tr>
                      <td className="faint">{c.seq}</td>
                      <td className="faint">{c.role === "judge" ? "–" : c.turn}</td>
                      <td><span className="badge" style={{ color: ROLE_COLORS[c.role], borderColor: "transparent", background: "var(--panel-2)" }}>{c.role}</span></td>
                      <td className="small">{shortModel(c.model)}</td>
                      <td>
                        <span className="ctx-track"><span style={{ width: `${(c.input_tokens / maxIn) * 100}%`, background: ROLE_COLORS[c.role] }} /></span>
                        <span className="small faint">{c.n_messages} msgs</span>
                      </td>
                      <td className="num">{num(c.input_tokens)}</td>
                      <td className="num">{num(c.output_tokens)}</td>
                      <td className="num faint">{c.cached_tokens ? num(c.cached_tokens) : "–"}</td>
                      <td className="num faint">{c.reasoning_tokens ? num(c.reasoning_tokens) : "–"}</td>
                      <td className="num faint">{c.tool_calls || "–"}</td>
                      <td className="num">{secs(c.latency_ms)}</td>
                      <td className="num" style={{ color: c.wait_ms ? "var(--warn)" : undefined }}>{c.wait_ms ? secs(c.wait_ms) : "–"}</td>
                      <td className="num">{usd(c.cost_usd)}</td>
                    </tr>
                    {c.error && <tr><td /><td colSpan={12} className="small" style={{ color: "var(--bad)" }}>{c.error}</td></tr>}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
          {trace.calls.length > calls.length && (
            <button className="sm" style={{ marginTop: 8 }} onClick={() => setShowAll(true)}>Show all {trace.calls.length} calls</button>
          )}
        </>
      )}
    </div>
  );
}
