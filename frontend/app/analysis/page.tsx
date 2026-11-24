"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { Bars, catColor, catLabel } from "@/components/Bars";
import { api, pct, shortModel, type AnalysisOverview, type Batch, type Experiment } from "@/lib/api";

function AnalysisInner() {
  const params = useSearchParams();
  const router = useRouter();
  const batchId = params.get("batch_id") ?? "";
  const expId = params.get("experiment_id") ?? "";
  const [data, setData] = useState<AnalysisOverview | null>(null);
  const [batches, setBatches] = useState<Batch[]>([]);
  const [exps, setExps] = useState<Experiment[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showF, setShowF] = useState(25);
  const [showV, setShowV] = useState(10);

  const load = useCallback(async () => {
    const q = new URLSearchParams();
    if (batchId) q.set("batch_id", batchId);
    if (expId) q.set("experiment_id", expId);
    try {
      setData(await api<AnalysisOverview>(`/analysis?${q}`));
    } catch (e) {
      setError(String(e));
    }
  }, [batchId, expId]);

  useEffect(() => {
    load();
    api<Batch[]>("/batches").then(setBatches).catch(() => {});
    api<Experiment[]>("/experiments").then(setExps).catch(() => {});
    const h = setInterval(load, 5000);
    return () => clearInterval(h);
  }, [load]);

  const setFilter = (k: string, v: string) => {
    const q = new URLSearchParams();
    if (v) q.set(k, v);
    router.push(`/analysis${v ? `?${q}` : ""}`);
  };

  async function reanalyze() {
    if (!batchId) return;
    setBusy(true);
    try {
      await api(`/batches/${batchId}/analyze?only_missing=false`, { method: "POST" });
      setTimeout(load, 3000);
    } finally {
      setBusy(false);
    }
  }

  const s = data?.summary;
  const cats = useMemo(() => Object.keys(data?.taxonomy ?? {}), [data]);
  const taskRows = Object.entries(data?.by_task ?? {}).sort((a, b) => sum(b[1]) - sum(a[1]));

  return (
    <>
      <h1>Failure analysis</h1>
      <p className="muted">
        Every failed episode gets a cause. Deterministic checks of the transcript (writes, authentication, confirmation, order ownership,
        match against the gold actions) come first, then an LLM judge reads the transcript and DB diff and picks one category, plus who
        was at fault: the agent, the simulated customer, the task or infrastructure.
      </p>
      {error && <div className="banner bad">{error}</div>}

      <div className="panel row" style={{ alignItems: "flex-end" }}>
        <label className="field">
          <span className="muted small">batch</span>
          <select value={batchId} onChange={(e) => setFilter("batch_id", e.target.value)}>
            <option value="">all runs</option>
            {batches.map((b) => (
              <option key={b.batch_id} value={b.batch_id}>{b.batch_id} · {shortModel(b.agent_model)} · {b.variant_label}</option>
            ))}
          </select>
        </label>
        <label className="field">
          <span className="muted small">experiment</span>
          <select value={expId} onChange={(e) => setFilter("experiment_id", e.target.value)}>
            <option value="">–</option>
            {exps.map((x) => <option key={x.experiment_id} value={x.experiment_id}>{x.experiment_id} · {shortModel(x.agent_model)}</option>)}
          </select>
        </label>
        {batchId && <button disabled={busy} onClick={reanalyze}>Re-run judge on this batch</button>}
      </div>

      {s && (
        <div className="panel">
          <div className="stats">
            <div className="stat"><div className="v">{s.finished}</div><div className="l">episodes</div></div>
            <div className="stat"><div className="v">{pct(s.pass_hat_k["1"])}</div><div className="l">pass^1</div></div>
            <div className="stat"><div className="v">{pct(s.adjusted_pass_1)}</div><div className="l">pass^1 excluding simulator errors</div></div>
            <div className="stat"><div className="v">{s.finished - s.passes}</div><div className="l">failures ({s.unanalyzed_failures} not yet analyzed)</div></div>
            <div className="stat"><div className="v">{s.violating_passes}</div><div className="l">passes with policy violations</div></div>
          </div>
        </div>
      )}

      <div className="grid2">
        <div className="panel">
          <h2>Failure causes</h2>
          <Bars data={s?.failure_categories ?? {}} color={catColor} titles={data?.taxonomy} />
        </div>
        <div className="panel">
          <h2>Who was at fault</h2>
          <Bars data={s?.failure_responsible ?? {}} color={(k) => (k === "agent" ? "var(--bad)" : "#9aa0a6")} />
          <h2 style={{ marginTop: 18 }}>Taxonomy</h2>
          <ul className="small" style={{ paddingLeft: 16, margin: 0 }}>
            {Object.entries(data?.taxonomy ?? {}).map(([k, v]) => (
              <li key={k}><b style={{ color: catColor(k) }}>{catLabel(k)}</b>: <span className="muted">{v}</span></li>
            ))}
          </ul>
        </div>
      </div>

      {taskRows.length > 0 && (
        <div className="panel">
          <h2>Failures by task</h2>
          <table>
            <thead><tr><th>task</th>{cats.filter((c) => taskRows.some(([, r]) => r[c])).map((c) => <th key={c} className="num">{catLabel(c)}</th>)}</tr></thead>
            <tbody>
              {taskRows.map(([t, r]) => (
                <tr key={t}>
                  <td><code>{t}</code></td>
                  {cats.filter((c) => taskRows.some(([, rr]) => rr[c])).map((c) => (
                    <td key={c} className="num" style={{ color: r[c] ? catColor(c) : undefined, fontWeight: r[c] ? 600 : 400 }}>{r[c] ?? "·"}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="panel">
        <h2>Failed episodes</h2>
        {!data?.failures.length ? <p className="muted">No failures.</p> : (
          <table>
            <thead><tr><th>run</th><th>task</th><th>cause</th><th>fault</th><th>explanation</th></tr></thead>
            <tbody>
              {data.failures.slice(0, showF).map((f) => (
                <tr key={f.id}>
                  <td><Link href={`/runs/${f.id}`}><code>{f.id.slice(0, 8)}</code></Link><div className="small muted">{shortModel(f.agent_model)} · {f.variant_label}</div></td>
                  <td><code className="small">{f.task_id}</code></td>
                  <td>{f.category ? <span className="badge" style={{ color: catColor(f.category) }}>{catLabel(f.category)}</span> : <span className="muted small">pending</span>}</td>
                  <td className="small">{f.responsible}</td>
                  <td className="small">{f.explanation}{f.method === "rules" && <span className="muted"> (rule-based)</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {data && data.failures.length > showF && (
          <button style={{ marginTop: 10 }} onClick={() => setShowF((n) => n + 50)}>Show more ({data.failures.length - showF} left)</button>
        )}
      </div>

      {!!data?.violating_passes.length && (
        <div className="panel">
          <h2>Passing runs that broke policy</h2>
          <p className="small muted">The final state was correct, but the transcript shows a policy breach. The DB-state metric can&apos;t see these.</p>
          <table>
            <tbody>
              {data.violating_passes.slice(0, showV).map((v) => (
                <tr key={v.id}>
                  <td><Link href={`/runs/${v.id}`}><code>{v.id.slice(0, 8)}</code></Link></td>
                  <td><code className="small">{v.task_id}</code></td>
                  <td><ul className="warn-list small">{v.violations.map((x, i) => <li key={i}>{x}</li>)}</ul></td>
                </tr>
              ))}
            </tbody>
          </table>
          {data.violating_passes.length > showV && (
            <button style={{ marginTop: 10 }} onClick={() => setShowV((n) => n + 25)}>Show more ({data.violating_passes.length - showV} left)</button>
          )}
        </div>
      )}
    </>
  );
}

const sum = (r: Record<string, number>) => Object.values(r).reduce((a, b) => a + b, 0);

export default function AnalysisPage() {
  return (
    <Suspense fallback={<p className="muted">Loading…</p>}>
      <AnalysisInner />
    </Suspense>
  );
}
