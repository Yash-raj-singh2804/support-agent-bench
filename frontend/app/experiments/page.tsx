"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import ModelPicker from "@/components/ModelPicker";
import { api, clock, dur, pct, shortModel, signedPct, type Config, type Estimate, type Experiment } from "@/lib/api";

const DESCRIPTIONS: Record<string, string> = {
  baseline: "The policy as the system prompt, plus the store tools.",
  think: "Adds a private `think` tool for reasoning before acting.",
  checklist: "Each write tool takes a required policy checklist. A failed check blocks the call.",
  guards: "Hard runtime guards outside the model: authenticated, owns the order, the customer just said yes.",
  short_policy: "A condensed policy (~40% of the length).",
  all: "think + checklist + guards",
};

export default function Experiments() {
  const router = useRouter();
  const [config, setConfig] = useState<Config | null>(null);
  const [list, setList] = useState<Experiment[]>([]);
  const [agentModel, setAgentModel] = useState("");
  const [userModel, setUserModel] = useState("");
  const [trials, setTrials] = useState(5);
  const [variants, setVariants] = useState<Set<string>>(new Set(["baseline", "think", "checklist", "guards", "all"]));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [taskCount, setTaskCount] = useState(0);

  useEffect(() => {
    api<Config>("/config").then((c) => {
      setConfig(c);
      setAgentModel(c.agent_model);
      setUserModel(c.user_model);
    }).catch((e) => setError(String(e)));
    api<unknown[]>("/tasks").then((t) => setTaskCount(t.length)).catch(() => {});
    const load = () => api<Experiment[]>("/experiments").then(setList).catch(() => {});
    load();
    const h = setInterval(load, 3000);
    return () => clearInterval(h);
  }, []);

  const toggle = (v: string) => setVariants((s) => {
    const n = new Set(s);
    if (n.has(v)) n.delete(v); else n.add(v);
    return n;
  });

  async function launch() {
    setBusy(true);
    setError(null);
    try {
      const order = Object.keys(config?.variants ?? {});
      const r = await api<{ experiment_id: string }>("/experiments", {
        method: "POST",
        body: JSON.stringify({ agent_model: agentModel, user_model: userModel, trials, variants: order.filter((v) => variants.has(v)) }),
      });
      router.push(`/experiments/${r.experiment_id}`);
    } catch (e) {
      setError(String(e));
      setBusy(false);
    }
  }

  const episodes = taskCount * trials * variants.size;
  const [est, setEst] = useState<Estimate | null>(null);
  useEffect(() => {
    if (!agentModel || variants.size === 0) return setEst(null);
    const h = setTimeout(() => {
      const order = Object.keys(config?.variants ?? {});
      api<Estimate>("/experiments/estimate", {
        method: "POST",
        body: JSON.stringify({ agent_model: agentModel, user_model: userModel, trials, variants: order.filter((v) => variants.has(v)) }),
      }).then(setEst).catch(() => setEst(null));
    }, 300);
    return () => clearTimeout(h);
  }, [agentModel, userModel, trials, variants, config, list.length]);

  return (
    <>
      <h1>Experiments</h1>
      <p className="muted">
        Ablations: run the same tasks with different agent designs and measure how much each one changes pass^k. Every variant
        runs on the same tasks, models and simulated customer.
      </p>
      {error && <div className="banner bad">{error}</div>}

      <div className="panel">
        <h2>New experiment</h2>
        <div className="row" style={{ alignItems: "flex-end", marginBottom: 14 }}>
          <ModelPicker config={config} value={agentModel} onChange={setAgentModel} label="agent model" />
          <ModelPicker config={config} value={userModel} onChange={setUserModel} label="customer simulator" />
          <label className="field">
            <span className="muted small">trials per task</span>
            <input type="number" min={1} max={10} value={trials} onChange={(e) => setTrials(Math.max(1, Math.min(10, +e.target.value || 1)))} style={{ minWidth: 0, width: 80 }} />
          </label>
        </div>
        <table>
          <tbody>
            {Object.keys(config?.variants ?? {}).map((v) => (
              <tr key={v}>
                <td style={{ width: 160 }}>
                  <span className={`chip ${variants.has(v) ? "on" : ""}`} onClick={() => toggle(v)}>
                    <input type="checkbox" readOnly checked={variants.has(v)} /> {v}
                  </span>
                </td>
                <td className="small muted">{DESCRIPTIONS[v]}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="row" style={{ marginTop: 12 }}>
          <button className="primary" disabled={busy || variants.size === 0 || !agentModel} onClick={launch}>
            Launch {episodes} episodes
          </button>
          {est && (
            <span className="small">
              <b>Estimated time: ~{dur(est.eta_s)}</b>
              <span className="muted"> (ends ~{clock(new Date(Date.now() + est.eta_s * 1000).toISOString())}) · ~{dur(est.avg_episode_s)} per episode, {est.parallelism} at a time
                {est.ahead_in_queue > 0 ? ` · ${est.ahead_in_queue} episodes already ahead in the queue` : ""}</span>
              <div className="faint">based on {est.based_on}</div>
            </span>
          )}
        </div>
      </div>

      <div className="panel">
        <h2>Past experiments</h2>
        {list.length === 0 ? <p className="muted">None yet.</p> : (
          <table>
            <thead>
              <tr><th>experiment</th><th>agent</th><th style={{ width: 230 }}>progress</th><th>pass^1 by variant</th></tr>
            </thead>
            <tbody>
              {list.map((e) => (
                <tr key={e.experiment_id}>
                  <td><Link href={`/experiments/${e.experiment_id}`}><code>{e.experiment_id}</code></Link>
                    <div className="small muted">{new Date(e.created_at).toLocaleString()}</div></td>
                  <td className="small">{shortModel(e.agent_model)}</td>
                  <td>
                    {e.progress ? (
                      <>
                        <span className="live-row-bar" style={{ display: "block", marginBottom: 4 }}><span style={{ width: `${Math.max(2, e.progress.progress * 100)}%` }} /></span>
                        <span className="small"><span className="dot live" /> {e.finished}/{e.total} · <b>~{dur(e.progress.eta_s)} left</b></span>
                        <div className="small faint">ends ~{clock(e.progress.ends_at)}</div>
                      </>
                    ) : <span className="small">{e.finished}/{e.total} <span className="badge ok">done</span></span>}
                  </td>
                  <td className="small">
                    {e.variants.map((v) => (
                      <span key={v.variant} style={{ marginRight: 12 }}>
                        {v.name}: <b>{pct(v.pass_hat_k["1"])}</b>{" "}
                        {v.delta_pass_1 != null && <span className={v.delta_pass_1 >= 0 ? "delta-pos" : "delta-neg"}>{signedPct(v.delta_pass_1)}</span>}
                      </span>
                    ))}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
