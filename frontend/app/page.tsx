"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { Bars, catColor } from "@/components/Bars";
import Icon from "@/components/Icon";
import StatusBadge from "@/components/StatusBadge";
import { api, compact, num, pct, shortModel, usd, type Batch, type Config, type Run, type Task } from "@/lib/api";
import { ago } from "@/lib/time";

export default function Overview() {
  const [config, setConfig] = useState<Config | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [batches, setBatches] = useState<Batch[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const load = () =>
      Promise.all([api<Config>("/config"), api<Task[]>("/tasks"), api<Run[]>("/runs?kind=eval&limit=1000"), api<Batch[]>("/batches")])
        .then(([c, t, r, b]) => { setConfig(c); setTasks(t); setRuns(r); setBatches(b); setError(null); })
        .catch((e) => setError(String(e)));
    load();
    const h = setInterval(load, 5000);
    return () => clearInterval(h);
  }, []);

  const done = runs.filter((r) => r.status === "done" || r.status === "error");
  const passRate = done.length ? done.filter((r) => r.reward === 1).length / done.length : null;
  const live = runs.filter((r) => r.status === "running" || r.status === "queued");

  const byModel = useMemo(() => {
    const m: Record<string, { n: number; ok: number }> = {};
    for (const r of done) {
      const k = shortModel(r.agent_model);
      m[k] ??= { n: 0, ok: 0 };
      m[k].n++;
      if (r.reward === 1) m[k].ok++;
    }
    return Object.fromEntries(Object.entries(m).sort((a, b) => b[1].ok / b[1].n - a[1].ok / a[1].n).map(([k, v]) => [k, v.ok / v.n]));
  }, [done]);

  const causes = useMemo(() => {
    const c: Record<string, number> = {};
    for (const r of done) if (r.analysis?.category) c[r.analysis.category] = (c[r.analysis.category] ?? 0) + 1;
    return Object.fromEntries(Object.entries(c).sort((a, b) => b[1] - a[1]).slice(0, 6));
  }, [done]);

  const recentBatches = batches.filter((b) => b.finished > 0).slice(0, 12).reverse();
  const bestModel = Object.entries(byModel)[0];
  const topCause = Object.entries(causes)[0];
  const hasKey = config && Object.values(config.providers).some(Boolean);

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Overview</h1>
          <p className="muted" style={{ margin: 0 }}>A support agent for refunds, order changes and policy questions, tested τ-bench style against a live store database.</p>
        </div>
        <div className="row">
          <Link className="btn" href="/playground"><Icon name="chat" size={14} /> Chat with the agent</Link>
          <Link className="btn primary" href="/tasks"><Icon name="play" size={14} /> Run a task</Link>
        </div>
      </div>

      {error && <div className="banner bad">Can&apos;t reach the backend: {error}</div>}

      {config && (!hasKey || done.length === 0) && (
        <div className="panel">
          <h2>Get started</h2>
          <div className="grid3">
            <Step n={1} done={!!hasKey} title="Add an API key" text={<>Put <code>ANTHROPIC_API_KEY</code> or <code>GROQ_API_KEY</code> in <code>.env</code>, then restart the backend.</>} />
            <Step n={2} done={false} title="Chat with the agent" text={<>Open the <Link href="/playground">playground</Link>, pick a test customer, and try a refund.</>} />
            <Step n={3} done={done.length > 0} title="Run a benchmark task" text={<>On <Link href="/tasks">Run tasks</Link>, press Run and watch the AI customer and the agent live.</>} />
          </div>
        </div>
      )}

      <div className="stats" style={{ marginBottom: 18 }}>
        <div className="stat accent"><div className="l">Pass rate (all episodes)</div><div className="v">{pct(passRate)}</div><div className="sub">{done.length} episodes · {tasks.length} tasks</div></div>
        <div className="stat"><div className="l">Latest batch pass^1</div><div className="v">{pct(batches[0]?.pass_hat_k["1"])}</div><div className="sub">{batches[0] ? `${shortModel(batches[0].agent_model)} · ${batches[0].variant_label}` : "no batches yet"}</div></div>
        <div className="stat"><div className="l">Best model</div><div className="v" style={{ fontSize: 18, paddingTop: 4 }}>{bestModel?.[0] ?? "–"}</div><div className="sub">{bestModel ? `${pct(bestModel[1])} pass rate` : ""}</div></div>
        <div className="stat"><div className="l">Top failure cause</div><div className="v" style={{ fontSize: 18, paddingTop: 4, color: topCause ? catColor(topCause[0]) : undefined }}>{topCause?.[0].replace(/_/g, " ") ?? "–"}</div><div className="sub">{topCause ? `${topCause[1]} episodes` : ""}</div></div>
        <div className="stat"><div className="l">Spend (all episodes)</div><div className="v">{usd(sumOrNull(done.map((r) => r.cost_usd)))}</div><div className="sub"><Link href="/usage">{compact(done.reduce((a, r) => a + (r.input_tokens ?? 0) + (r.output_tokens ?? 0) + (r.customer_tokens ?? 0) + (r.judge_tokens ?? 0), 0))} tokens →</Link></div></div>
        <div className="stat"><div className="l">Running now</div><div className="v">{live.length}</div><div className="sub">{live.length ? <><span className="dot live" /> live</> : "idle"}</div></div>
      </div>

      <div className="grid2">
        <div className="panel">
          <div className="panel-head"><h2>pass^1 by batch</h2><Link className="small" href="/tasks">all batches →</Link></div>
          {recentBatches.length === 0 ? <div className="empty"><b>No results yet</b>Run some tasks to see the trend.</div> : (
            <div className="spark">
              {recentBatches.map((b) => (
                <Link key={b.batch_id} href={`/analysis?batch_id=${b.batch_id}`} className="spark-col" title={`${b.batch_id} · ${shortModel(b.agent_model)} · ${b.variant_label}: ${pct(b.pass_hat_k["1"])}`}>
                  <span className="small" style={{ color: "var(--text)" }}>{pct(b.pass_hat_k["1"]).replace(".0%", "%")}</span>
                  <span className="spark-bar" style={{ height: `${Math.max(3, (b.pass_hat_k["1"] ?? 0) * 80)}px` }} />
                  <span className="spark-lbl">{shortModel(b.agent_model).split("/").pop()}</span>
                </Link>
              ))}
            </div>
          )}
        </div>
        <div className="panel">
          <div className="panel-head"><h2>Pass rate by model</h2></div>
          <Bars data={byModel} max={1} format={pct} />
        </div>
      </div>

      <div className="grid2">
        <div className="panel">
          <div className="panel-head"><h2>Recent episodes</h2><Link className="small" href="/analysis">failure analysis →</Link></div>
          {runs.length === 0 ? <div className="empty"><b>Nothing yet</b>Your episodes will show up here.</div> : (
            <table>
              <tbody>
                {runs.slice(0, 10).map((r) => (
                  <tr key={r.id}>
                    <td><Link href={`/runs/${r.id}`}>{tasks.find((t) => t.id === r.task_id)?.title ?? r.task_id}</Link>
                      <div className="small faint">{shortModel(r.agent_model)} · {r.variant_label ?? "baseline"}</div></td>
                    <td className="small muted">{r.analysis?.category ? <span style={{ color: catColor(r.analysis.category) }}>{r.analysis.category.replace(/_/g, " ")}</span> : ""}</td>
                    <td className="num"><StatusBadge run={r} /><div className="small faint">{ago(r.created_at)}</div></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
        <div className="panel">
          <div className="panel-head"><h2>Why episodes fail</h2></div>
          <Bars data={causes} color={catColor} titles={config?.taxonomy} />
          <div className="callout small" style={{ marginTop: 16 }}>
            Average episode: <b>{num(avg(done.map((r) => (r.input_tokens ?? 0) + (r.output_tokens ?? 0))))}</b> agent tokens,{" "}
            <b>{usd(avg(done.filter((r) => r.cost_usd != null).map((r) => r.cost_usd!)))}</b>,{" "}
            <b>{num(avg(done.map((r) => r.duration_s ?? 0)), 1)}s</b>, <b>{num(avg(done.map((r) => r.llm_calls ?? 0)), 1)}</b> LLM calls.
          </div>
        </div>
      </div>
    </>
  );
}

const sumOrNull = (xs: (number | null)[]) => (xs.some((x) => x != null) ? xs.reduce<number>((a, b) => a + (b ?? 0), 0) : null);
const avg = (xs: number[]) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null);

function Step({ n, done, title, text }: { n: number; done: boolean; title: string; text: React.ReactNode }) {
  return (
    <div className="task-card">
      <div className="row">
        <span className={`badge ${done ? "ok" : "accent"}`}>{done ? <Icon name="check" size={12} /> : n}</span>
        <b>{title}</b>
      </div>
      <div className="small muted">{text}</div>
    </div>
  );
}
