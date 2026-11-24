"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Stacked } from "@/components/Bars";
import Icon from "@/components/Icon";
import { LiveRow } from "@/components/LiveStatus";
import ModelPicker from "@/components/ModelPicker";
import { api, compact, dur, pct, shortModel, usd, type Batch, type Config, type Estimate, type LiveInfo, type Run, type Task } from "@/lib/api";
import { ago } from "@/lib/time";

export default function TasksPage() {
  const router = useRouter();
  const [config, setConfig] = useState<Config | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [batches, setBatches] = useState<Batch[]>([]);
  const [trials, setTrials] = useState(1);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [agentModel, setAgentModel] = useState("");
  const [userModel, setUserModel] = useState("");
  const [variant, setVariant] = useState("baseline");
  const [cat, setCat] = useState("all");
  const [active, setActive] = useState<LiveInfo[]>([]);
  const [est, setEst] = useState<Estimate | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [t, r, b, l] = await Promise.all([api<Task[]>("/tasks"), api<Run[]>("/runs?kind=eval&limit=500"), api<Batch[]>("/batches"),
        api<{ active: LiveInfo[] }>("/live")]);
      setActive(l.active);
      setTasks(t);
      setRuns(r);
      setBatches(b);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    api<Config>("/config")
      .then((c) => {
        setConfig(c);
        setAgentModel(c.agent_model);
        setUserModel(c.user_model);
      })
      .catch((e) => setError(String(e)));
    refresh();
    const h = setInterval(refresh, 2000);
    return () => clearInterval(h);
  }, [refresh]);

  const runsByTask = useMemo(() => {
    const m = new Map<string, Run[]>();
    for (const r of runs) if (r.task_id) m.set(r.task_id, [...(m.get(r.task_id) ?? []), r]);
    return m;
  }, [runs]);
  const cats = useMemo(() => ["all", ...new Set(tasks.map((t) => t.category))], [tasks]);
  const shown = tasks.filter((t) => cat === "all" || t.category === cat);
  const noKey = config && !Object.values(config.providers).some(Boolean);
  const shownIds = shown.map((t) => t.id).join(",");
  useEffect(() => {
    if (!agentModel || !shownIds) return;
    const h = setTimeout(() => {
      api<Estimate>("/batches/estimate", { method: "POST", body: JSON.stringify({ task_ids: shownIds.split(","), trials, agent_model: agentModel }) })
        .then(setEst).catch(() => setEst(null));
    }, 300);
    return () => clearTimeout(h);
  }, [agentModel, trials, shownIds, active.length]);

  async function launch(ids: string[] | null, key: string) {
    setBusy(key);
    setError(null);
    try {
      const r = await api<{ batch_id: string; run_ids: string[] }>("/batches", {
        method: "POST",
        body: JSON.stringify({ task_ids: ids, trials, agent_model: agentModel, user_model: userModel, variant }),
      });
      if (r.run_ids.length === 1) router.push(`/runs/${r.run_ids[0]}`); // single run → watch it live
      else await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(null);
    }
  }

  async function retryErrors(batchId: string) {
    setBusy(batchId);
    try {
      await api(`/batches/${batchId}/retry-errors`, { method: "POST" });
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Run tasks</h1>
          <p className="muted" style={{ margin: 0 }}>
            Press <b>Run</b> on a task to watch an AI customer and the agent talk live. A run passes only if the final database matches the expected state.
          </p>
        </div>
        <button className="primary" disabled={!!busy || !!noKey} onClick={() => launch(cat === "all" ? null : shown.map((t) => t.id), "all")}>
          <Icon name="play" size={14} /> Run {cat === "all" ? "all" : `all ${cat}`} ({shown.length * trials} episodes)
          {est && <span style={{ opacity: .8, fontWeight: 400 }}>· ~{dur(est.eta_s)}</span>}
        </button>
      </div>

      {error && <div className="banner bad">{error}</div>}
      {noKey && <div className="banner run">No API keys configured. Add ANTHROPIC_API_KEY or GROQ_API_KEY to <code>.env</code> and restart the backend.</div>}

      <div className="panel row" style={{ alignItems: "flex-end" }}>
        <ModelPicker config={config} value={agentModel} onChange={setAgentModel} label="Agent model" />
        <ModelPicker config={config} value={userModel} onChange={setUserModel} label="Customer simulator" />
        <label className="field">
          <span>Agent variant</span>
          <select value={variant} onChange={(e) => setVariant(e.target.value)}>
            {Object.keys(config?.variants ?? { baseline: {} }).map((v) => <option key={v}>{v}</option>)}
          </select>
        </label>
        <label className="field">
          <span>Trials per task</span>
          <input type="number" min={1} max={10} value={trials} style={{ minWidth: 0, width: 90 }}
            onChange={(e) => setTrials(Math.max(1, Math.min(10, +e.target.value || 1)))} />
        </label>
      </div>

      {active.length > 0 && (
        <div className="live-card">
          <div className="panel-head" style={{ marginBottom: 8 }}>
            <h2 style={{ display: "flex", alignItems: "center", gap: 8 }}><span className="dot live" /> Live now</h2>
            <span className="small faint">
              {active.filter((a) => a.status === "running").length} running · {active.filter((a) => a.status === "queued").length} queued
            </span>
          </div>
          <div className="live-list">
            {active.slice(0, 8).map((l) => <LiveRow key={l.id} live={l} />)}
            {active.length > 8 && <div className="small faint" style={{ padding: "4px 10px" }}>+{active.length - 8} more</div>}
          </div>
        </div>
      )}

      <div className="row" style={{ marginBottom: 14, justifyContent: "space-between" }}>
        <div className="seg">
          {cats.map((c) => <button key={c} className={c === cat ? "on" : ""} onClick={() => setCat(c)}>{c.replace("_", " ")}</button>)}
        </div>
        <span className="small faint">{trials > 1 ? `${trials} trials each; results appear in Batches below` : "1 trial: opens the live view"}</span>
      </div>

      <div className="grid3" style={{ marginBottom: 22 }}>
        {shown.map((t) => {
          const rs = (runsByTask.get(t.id) ?? []).slice(0, 14);
          const done = rs.filter((r) => r.status === "done");
          const rate = done.length ? done.filter((r) => r.reward === 1).length / done.length : null;
          return (
            <div key={t.id} className="task-card">
              <div className="row" style={{ justifyContent: "space-between" }}>
                <span className="badge accent">{t.category.replace("_", " ")}</span>
                <span className="small faint">{rate == null ? "not run" : `${pct(rate)} of ${done.length}`}</span>
              </div>
              <div className="title">{t.title}</div>
              <div className="small muted" style={{ display: "-webkit-box", WebkitLineClamp: 3, WebkitBoxOrient: "vertical", overflow: "hidden" }}>
                {t.user_scenario}
              </div>
              <div className="small faint">
                {t.gold_actions.length ? <>expects <code>{t.gold_actions.map((a) => a.name).join(", ")}</code></> : "expects no changes (refuse / answer)"}
              </div>
              <div className="foot">
                <span className="dots">
                  {rs.map((r) => (
                    <Link key={r.id} href={`/runs/${r.id}`} title={`${r.status} · ${shortModel(r.agent_model)} · ${ago(r.created_at)}`}>
                      {r.status === "done" ? <span className={r.reward === 1 ? "ok" : "bad"}>{r.reward === 1 ? "●" : "✕"}</span>
                        : r.status === "error" ? <span className="bad">!</span> : <span className="muted">○</span>}
                    </Link>
                  ))}
                </span>
                <button className="sm" disabled={!!busy || !!noKey} onClick={() => launch([t.id], t.id)}>
                  {busy === t.id ? "Starting…" : <><Icon name="play" size={12} /> Run</>}
                </button>
              </div>
            </div>
          );
        })}
      </div>

      <div className="panel">
        <div className="panel-head"><h2>Batches</h2><span className="small faint">{batches.length} total</span></div>
        {batches.length === 0 ? (
          <div className="empty"><b>No batches yet</b>Run a task or press Run all.</div>
        ) : (
          <table>
            <thead>
              <tr><th>batch</th><th>started</th><th>model / variant</th><th>progress</th><th>pass^k</th><th>failure causes</th><th className="num">tokens/ep</th><th className="num">cost/ep</th></tr>
            </thead>
            <tbody>
              {batches.slice(0, 30).map((b) => (
                <tr key={b.batch_id}>
                  <td><Link href={`/batches/${b.batch_id}`}><code>{b.batch_id}</code></Link>
                    {b.errors > 0 && <div><button className="sm ghost" style={{ padding: "2px 0", color: "var(--warn)" }} disabled={!!busy}
                      onClick={() => retryErrors(b.batch_id)}>↻ retry {b.errors} error{b.errors > 1 ? "s" : ""}{b.rate_limited ? ` (${b.rate_limited} rate-limited)` : ""}</button></div>}</td>
                  <td className="small muted">{ago(b.created_at)}</td>
                  <td className="small">{shortModel(b.agent_model)}<div className="faint">{b.variant_label}</div></td>
                  <td>{b.finished < b.total ? <><span className="badge run">{b.finished}/{b.total}</span>{b.eta_s != null && <div className="small faint">~{dur(b.eta_s)} left</div>}</> : <span className="small">{b.finished}/{b.total}</span>}</td>
                  <td className="small">
                    {Object.entries(b.pass_hat_k).map(([k, v]) => <span key={k} style={{ marginRight: 10 }}>k={k} <b>{pct(v)}</b></span>)}
                  </td>
                  <td style={{ minWidth: 200 }}><Stacked data={b.failure_categories} /></td>
                  <td className="num small">{compact(b.avg_total_tokens)}</td>
                  <td className="num small">{usd(b.avg_cost_usd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
