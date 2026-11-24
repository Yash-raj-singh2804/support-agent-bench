"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import DiffView from "@/components/DiffView";
import Icon from "@/components/Icon";
import { LiveCard } from "@/components/LiveStatus";
import StatusBadge from "@/components/StatusBadge";
import TokenTrace from "@/components/TokenTrace";
import Transcript from "@/components/Transcript";
import { catColor, catLabel } from "@/components/Bars";
import { api, compact, num, shortModel, usd, type LiveInfo, type Run, type Trace } from "@/lib/api";

export default function RunPage() {
  const { id } = useParams<{ id: string }>();
  const [run, setRun] = useState<Run | null>(null);
  const [trace, setTrace] = useState<Trace | null>(null);
  const [liveInfo, setLiveInfo] = useState<LiveInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [analyzing, setAnalyzing] = useState(false);
  const [rerunning, setRerunning] = useState(false);
  const router = useRouter();
  const bottom = useRef<HTMLDivElement>(null);
  const msgCount = run?.messages?.length ?? 0;
  const isLive = run?.status === "queued" || run?.status === "running";
  useEffect(() => {
    if (isLive) bottom.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [msgCount, isLive]);

  useEffect(() => {
    let stop = false;
    let timer: ReturnType<typeof setTimeout>;
    const load = async () => {
      try {
        const [r, t] = await Promise.all([api<Run>(`/runs/${id}`), api<Trace>(`/runs/${id}/trace`).catch(() => null)]);
        if (stop) return;
        setRun(r);
        setTrace(t);
        if (r.status === "queued" || r.status === "running") {
          setLiveInfo(await api<LiveInfo>(`/runs/${id}/live`).catch(() => null));
          timer = setTimeout(load, 1000);
        } else setLiveInfo(null);
      } catch (e) {
        setError(String(e));
      }
    };
    load();
    return () => {
      stop = true;
      clearTimeout(timer);
    };
  }, [id]);

  async function reanalyze() {
    setAnalyzing(true);
    try {
      await api(`/runs/${id}/analyze`, { method: "POST" });
      setRun(await api<Run>(`/runs/${id}`));
      setTrace(await api<Trace>(`/runs/${id}/trace`));
    } catch (e) {
      setError(String(e));
    } finally {
      setAnalyzing(false);
    }
  }

  async function rerun() {
    if (!run?.task_id) return;
    setRerunning(true);
    try {
      const r = await api<{ run_ids: string[] }>("/batches", {
        method: "POST",
        body: JSON.stringify({ task_ids: [run.task_id], trials: 1, agent_model: run.agent_model, user_model: run.user_model, variant: run.variant ?? undefined }),
      });
      router.push(`/runs/${r.run_ids[0]}`);
    } catch (e) {
      setError(String(e));
      setRerunning(false);
    }
  }

  if (error) return <div className="banner bad">{error}</div>;
  if (!run) return <p className="muted">Loading…</p>;

  const live = run.status === "queued" || run.status === "running";
  const passed = run.reward === 1;

  return (
    <>
      <p className="small"><Link href="/tasks">← all tasks</Link>{run.batch_id && <> · <Link href={`/batches/${run.batch_id}`}>batch {run.batch_id}</Link></>}</p>
      <div className="page-head" style={{ marginBottom: 8 }}>
        <div className="row"><h1 style={{ margin: 0 }}>{run.task?.title ?? run.task_id}</h1><StatusBadge run={run} /></div>
        <button className="primary" disabled={rerunning || live} onClick={rerun}><Icon name="refresh" size={14} /> {rerunning ? "Starting…" : "Run again"}</button>
      </div>
      <p className="muted small" style={{ marginTop: 0 }}>
        run <code>{run.id}</code> · batch <code>{run.batch_id}</code> · trial {run.trial} · agent {shortModel(run.agent_model)} ·
        variant <b>{run.variant_label ?? "baseline"}</b> · customer {shortModel(run.user_model)} · agent {compact(run.input_tokens)} in / {compact(run.output_tokens)} out ({run.llm_calls ?? 0} calls) ·
        total {compact(trace ? trace.total.input + trace.total.output : null)} tokens · {usd(run.cost_usd)} · {num(run.duration_s, 1)}s
      </p>

      {live ? (
        liveInfo && liveInfo.phase !== "done" ? <LiveCard live={liveInfo} /> : (
          <div className="banner run"><span className="dot live" /> Starting…</div>
        )
      ) : run.status === "error" ? (
        <div className="banner bad">Episode errored<pre style={{ fontWeight: 400, marginTop: 6 }}>{run.error}</pre></div>
      ) : (
        <div className={`banner ${passed ? "ok" : "bad"}`}>
          <Icon name={passed ? "check" : "x"} size={16} /> {passed ? "PASS" : "FAIL"} · reward {run.reward} · DB state {run.db_match ? "matches gold" : "differs from gold"} · required outputs{" "}
          {run.outputs_ok ? "ok" : `missing: ${(run.missing_outputs ?? []).join(", ")}`}
          {run.process_ok === false ? ` · process: ${(run.signals?.violations ?? []).join("; ")}` : run.process_ok ? " · process ok" : ""}
          {run.grounded_ok === false ? ` · hallucinated: ${(run.hallucinations ?? []).join("; ")}` : run.grounded_ok ? " · grounded" : ""}
          {run.grounding_revisions ? ` · ${run.grounding_revisions} draft(s) revised` : ""}
          {" "}· ended by {run.end_reason}
        </div>
      )}

      <div className="grid2">
        <div className="panel">
          <h2>Transcript</h2>
          <Transcript messages={run.messages ?? []} highlightIdx={run.analysis?.critical_message_idx} pending={run.status === "running"} />
          <div ref={bottom} />
        </div>
        <div>
          {!live && (run.analysis || (run.signals?.violations.length ?? 0) > 0 || run.reward !== 1) && (
            <div className="panel">
              <div className="row" style={{ justifyContent: "space-between" }}>
                <h2 style={{ margin: 0 }}>Failure analysis</h2>
                {run.reward !== 1 && <button disabled={analyzing} onClick={reanalyze}>{analyzing ? "Judging…" : "Re-run judge"}</button>}
              </div>
              {run.analysis ? (
                <div style={{ marginTop: 10 }}>
                  <p style={{ margin: "0 0 6px" }}>
                    <span className="badge" style={{ color: catColor(run.analysis.category) }}>{catLabel(run.analysis.category)}</span>{" "}
                    <span className="small muted">
                      fault: <b>{run.analysis.responsible}</b> · {run.analysis.method === "llm_judge" ? `LLM judge (${shortModel(run.analysis.judge_model)})` : "rule-based"}
                      {run.analysis.confidence && ` · ${run.analysis.confidence} confidence`}
                    </span>
                  </p>
                  <div className="callout small">{run.analysis.explanation}</div>
                  {run.analysis.critical_message_idx != null && <p className="small muted">The critical message is outlined in red in the transcript.</p>}
                  {run.analysis.rule_based_guess && run.analysis.rule_based_guess !== run.analysis.category && (
                    <p className="small muted">Rule-based guess was {catLabel(run.analysis.rule_based_guess)}.</p>
                  )}
                  {run.analysis.judge_error && <p className="small" style={{ color: "var(--bad)" }}>Judge error: {run.analysis.judge_error}</p>}
                </div>
              ) : run.reward !== 1 ? <p className="muted small">Not analyzed yet.</p> : null}
              {run.signals && (
                <>
                  <h2 style={{ marginTop: 14, fontSize: 14 }}>Transcript checks</h2>
                  <p className="small" style={{ margin: "0 0 6px" }}>
                    gold actions matched {run.signals.matched_gold_actions}/{run.signals.gold_actions} · tool calls {run.signals.n_tool_calls} ·
                    tool errors {run.signals.tool_errors} · guard blocks {run.signals.guard_blocks}
                    {run.signals.extra_writes.length > 0 && <> · extra writes: <code>{run.signals.extra_writes.join(", ")}</code></>}
                    {run.signals.missed_gold_tools.length > 0 && <> · missed: <code>{run.signals.missed_gold_tools.join(", ")}</code></>}
                  </p>
                  {run.signals.violations.length > 0 && (
                    <ul className="warn-list small">{run.signals.violations.map((v, i) => <li key={i}>{v}</li>)}</ul>
                  )}
                </>
              )}
            </div>
          )}
          {run.task && (
            <div className="panel">
              <h2>Task</h2>
              <p className="small muted" style={{ marginTop: 0 }}>Instruction given to the simulated customer</p>
              <p>{run.task.user_scenario}</p>
              <h2 style={{ marginTop: 16 }}>Gold actions</h2>
              {run.task.gold_actions.length === 0 ? (
                <p className="muted">None: the correct outcome is an unchanged database.</p>
              ) : (
                run.task.gold_actions.map((a, i) => (
                  <pre key={i} className="tool" style={{ width: "100%", marginBottom: 6 }}>
                    {a.name}({JSON.stringify(a.arguments, null, 1)})
                  </pre>
                ))
              )}
              {run.task.required_outputs.length > 0 && (
                <p className="small">Required in agent replies: {run.task.required_outputs.map((o) => <code key={o}>&quot;{o}&quot; </code>)}</p>
              )}
            </div>
          )}
          {!live && run.status === "done" && (
            <div className="panel">
              <h2>Final DB state vs gold</h2>
              <p className="small muted">
                agent <code>{run.agent_hash?.slice(0, 12)}</code> · gold <code>{run.gold_hash?.slice(0, 12)}</code>
              </p>
              <DiffView diff={run.diff ?? []} empty="Identical: the agent produced exactly the expected database state." />
            </div>
          )}
        </div>
      </div>
      <div className="panel">
        <div className="panel-head">
          <h2>Token trace</h2>
          <span className="small faint">{live ? "updating live" : "agent, simulated customer and judge calls"}</span>
        </div>
        <TokenTrace trace={trace} />
      </div>
    </>
  );
}
