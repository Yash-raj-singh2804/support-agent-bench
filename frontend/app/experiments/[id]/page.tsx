"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { Fragment, useEffect, useState } from "react";
import { Bars, Stacked } from "@/components/Bars";
import ProgressCard from "@/components/ProgressCard";
import { api, dur, num, pct, shortModel, signedPct, usd, type Experiment } from "@/lib/api";

const heat = (v: number) => `color-mix(in srgb, var(--ok-bg) ${Math.round(v * 100)}%, var(--bad-bg))`;

export default function ExperimentPage() {
  const { id } = useParams<{ id: string }>();
  const [exp, setExp] = useState<Experiment | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let stop = false;
    let t: ReturnType<typeof setTimeout>;
    const load = async () => {
      try {
        const e = await api<Experiment>(`/experiments/${id}`);
        if (stop) return;
        setExp(e);
        if (e.finished < e.total) t = setTimeout(load, 2000);
      } catch (e) {
        setError(String(e));
      }
    };
    load();
    return () => { stop = true; clearTimeout(t); };
  }, [id]);

  if (error) return <div className="banner bad">{error}</div>;
  if (!exp) return <p className="muted">Loading…</p>;

  const vs = exp.variants;
  const maxK = Math.max(0, ...vs.map((v) => Object.keys(v.pass_hat_k).length));
  const ks = Array.from({ length: maxK }, (_, i) => String(i + 1));
  const cats = [...new Set(vs.flatMap((v) => Object.keys(v.by_category)))].sort();
  const best = [...vs].sort((a, b) => (b.pass_hat_k["1"] ?? 0) - (a.pass_hat_k["1"] ?? 0))[0];

  return (
    <>
      <p className="small"><Link href="/experiments">← experiments</Link></p>
      <h1>Experiment <code>{exp.experiment_id}</code></h1>
      <p className="muted small">
        agent {shortModel(exp.agent_model)} · customer {shortModel(exp.user_model)} · {exp.tasks} tasks · {exp.finished}/{exp.total} episodes finished
      </p>
      {exp.progress && (
        <ProgressCard title="Running experiment" p={exp.progress} maxRows={8}>
          <div className="variant-progress">
            {vs.map((v) => v.progress && (
              <div key={v.variant} className="vp-row">
                <span className="vp-name">{v.name}</span>
                <span className="live-row-bar"><span style={{ width: `${Math.max(2, v.progress.progress * 100)}%` }} /></span>
                <span className="small num">{v.progress.done}/{v.progress.total}</span>
                <span className="small num faint">{v.progress.done === v.progress.total ? "done" : `~${dur(v.progress.eta_s)}`}</span>
              </div>
            ))}
          </div>
        </ProgressCard>
      )}
      {exp.finished === exp.total && best && (
        <div className="callout" style={{ marginBottom: 18 }}>
          Best variant: <b>{best.name}</b> at pass^1 {pct(best.pass_hat_k["1"])}
          {best.delta_pass_1 != null && <> ({signedPct(best.delta_pass_1)} vs baseline)</>}
          {ks.length > 1 && <>, pass^{ks.length} {pct(best.pass_hat_k[ks[ks.length - 1]])}</>}.
        </div>
      )}

      <div className="panel">
        <h2>Results by variant</h2>
        <table>
          <thead>
            <tr>
              <th>variant</th>
              {ks.map((k) => <th key={k} className="num">pass^{k}</th>)}
              <th className="num">Δ pass^1</th>
              <th className="num">excl. sim errors</th>
              <th className="num">passes w/ violations</th>
              <th className="num">tokens/ep</th>
              <th className="num">$/ep</th>
              <th className="num">sec/ep</th>
              <th>failure causes</th>
            </tr>
          </thead>
          <tbody>
            {vs.map((v) => (
              <tr key={v.variant}>
                <td><b>{v.name}</b>{v.name !== v.variant && <div className="small muted">{v.variant}</div>}<div className="small"><Link href={`/analysis?batch_id=${v.batch_id}`}>analysis →</Link></div></td>
                {ks.map((k) => <td key={k} className="num">{pct(v.pass_hat_k[k])}</td>)}
                <td className="num">
                  {v.delta_pass_1 == null ? "–" : <span className={v.delta_pass_1 >= 0 ? "delta-pos" : "delta-neg"}>{signedPct(v.delta_pass_1)}</span>}
                </td>
                <td className="num">{pct(v.adjusted_pass_1)}</td>
                <td className="num">{v.violating_passes}/{v.passes}</td>
                <td className="num">{num(v.avg_tokens)}</td>
                <td className="num">{usd(v.avg_agent_cost_usd)}</td>
                <td className="num">{num(v.avg_duration_s, 1)}</td>
                <td style={{ minWidth: 220 }}><Stacked data={v.failure_categories} /></td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="small muted">
          pass^k is the chance that all k independent tries of a task succeed. &quot;Excl. sim errors&quot; drops runs the judge blamed on the
          simulated customer. &quot;Passes w/ violations&quot; counts runs that reached the right state but broke policy on the way (e.g. no
          explicit confirmation).
        </p>
      </div>

      <div className="grid2">
        <div className="panel">
          <h2>pass^1</h2>
          <Bars data={Object.fromEntries(vs.map((v) => [v.name, v.pass_hat_k["1"] ?? 0]))} max={1} format={pct} />
        </div>
        <div className="panel">
          <h2>Success by task category</h2>
          <div className="scroll">
          <table>
            <thead><tr><th>category</th>{vs.map((v) => <th key={v.variant} className="num">{v.name}</th>)}</tr></thead>
            <tbody>
              {cats.map((c) => (
                <tr key={c}>
                  <td>{c}</td>
                  {vs.map((v) => {
                    const x = v.by_category[c];
                    return <td key={v.variant} className="heat" style={{ background: x == null ? undefined : heat(x) }}>{pct(x)}</td>;
                  })}
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        </div>
      </div>

      {exp.matrix && (
        <div className="panel">
          <h2>Per task</h2>
          <table>
            <thead><tr><th>task</th>{vs.map((v) => <th key={v.variant} className="num">{v.name}</th>)}</tr></thead>
            <tbody>
              {Object.entries(exp.matrix).sort().map(([task, row]) => (
                <Fragment key={task}>
                  <tr>
                    <td><code>{task}</code></td>
                    {vs.map((v) => {
                      const rs = row[v.variant] ?? [];
                      const rate = rs.length ? rs.filter((r) => r >= 1).length / rs.length : null;
                      return (
                        <td key={v.variant} className="heat dots" style={{ background: rate == null ? undefined : heat(rate) }}>
                          {rs.map((r, i) => <span key={i} className={r >= 1 ? "ok" : "bad"}>{r >= 1 ? "●" : "✕"}</span>)}
                        </td>
                      );
                    })}
                  </tr>
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
