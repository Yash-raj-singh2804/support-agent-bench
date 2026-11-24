"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import Icon from "@/components/Icon";
import { dur, shortModel, type LiveInfo } from "@/lib/api";

const STEPS = [
  { key: "queued", label: "Queued", phases: ["queued"] },
  { key: "setup", label: "Setup", phases: ["starting"] },
  { key: "talk", label: "Conversation", phases: ["customer", "agent", "tool", "rate_limited", "retrying"] },
  { key: "score", label: "Scoring", phases: ["scoring"] },
  { key: "judge", label: "Judging", phases: ["judging"] },
];

const PHASE_ICON: Record<string, string> = {
  queued: "list", starting: "db", customer: "user", agent: "bolt", tool: "tool",
  rate_limited: "refresh", retrying: "refresh", scoring: "check", judging: "bug",
};

/** Ticks once per second so elapsed/ETA move smoothly between polls. */
function useTicker(live: LiveInfo | null) {
  const [base, setBase] = useState(() => Date.now());
  const [, setTick] = useState(0);
  useEffect(() => setBase(Date.now()), [live]);
  useEffect(() => {
    const h = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(h);
  }, []);
  return (Date.now() - base) / 1000;
}

export function LiveCard({ live }: { live: LiveInfo }) {
  const dt = useTicker(live);
  const phase = live.phase ?? "queued";
  const running = live.status === "running";
  const elapsed = (live.elapsed_s ?? 0) + (running ? dt : 0);
  const eta = Math.max(0, (live.eta_s ?? 0) - dt);
  const waitLeft = Math.max(0, (live.wait_left_s ?? 0) - dt);
  const stepIdx = STEPS.findIndex((s) => s.phases.includes(phase));
  const pctDone = Math.min(99, Math.round(((live.progress ?? 0) + (running && live.est_total_s ? dt / live.est_total_s : 0)) * 100));
  const throttled = phase === "rate_limited" || phase === "retrying";

  return (
    <div className={`live-card ${throttled ? "throttled" : ""}`}>
      <div className="live-head">
        <span className="live-orb"><Icon name={PHASE_ICON[phase] ?? "spark"} size={18} /></span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="live-title">
            {live.phase_label ?? phase}
            {throttled && waitLeft > 0 && <span className="badge run" style={{ marginLeft: 8 }}>resuming in {dur(waitLeft)}</span>}
          </div>
          <div className="live-detail">{live.detail ?? (phase === "queued" && live.queue_position ? `#${live.queue_position} in line` : " ")}</div>
        </div>
        <div className="live-eta">
          <div className="v">{phase === "queued" ? `~${dur(Math.max(0, (live.starts_in_s ?? 0) - dt))}` : `~${dur(eta)}`}</div>
          <div className="l">{phase === "queued" ? "until it starts" : "left"}</div>
        </div>
      </div>

      <div className="live-steps">
        {STEPS.map((s, i) => (
          <div key={s.key} className={`live-step ${i < stepIdx ? "done" : i === stepIdx ? "on" : ""}`}>
            <span className="pip">{i < stepIdx ? <Icon name="check" size={10} /> : i + 1}</span>
            {s.label}
            {s.key === "talk" && i === stepIdx && live.turn ? <span className="faint"> · turn {live.turn}/~{Math.max(live.turn, Math.round(live.expected_turns ?? 0))}</span> : null}
          </div>
        ))}
      </div>

      <div className="live-bar"><span style={{ width: `${phase === "queued" ? 0 : Math.max(3, pctDone)}%` }} /></div>
      <div className="live-meta">
        {live.trial != null && <span>Pass {live.trial + 1}</span>}
        {running && <span>{dur(elapsed)} elapsed</span>}
        {running && <span>{live.agent_steps ?? 0} agent steps · {live.tool_calls ?? 0} tool calls</span>}
        {live.last_tool && phase !== "tool" && <span className="mono">last tool: {live.last_tool.split("(")[0]}</span>}
        {live.slower_than_usual
          ? <span style={{ color: "var(--warn)" }}>slower than usual (typically {dur(live.typical_s)})</span>
          : live.est_total_s != null && <span className="faint">typically {dur(live.est_total_s)}, from {live.based_on}</span>}
      </div>
    </div>
  );
}

/** One compact row for lists of running/queued episodes. */
export function LiveRow({ live, showTask = true }: { live: LiveInfo; showTask?: boolean }) {
  const dt = useTicker(live);
  const phase = live.phase ?? "queued";
  const running = live.status === "running";
  const eta = Math.max(0, (live.eta_s ?? 0) - dt);
  const pctDone = Math.min(99, Math.round(((live.progress ?? 0) + (running && live.est_total_s ? dt / live.est_total_s : 0)) * 100));
  return (
    <Link href={`/runs/${live.id}`} className="live-row">
      <span className={`live-orb sm ${phase === "rate_limited" ? "warn" : ""}`}><Icon name={PHASE_ICON[phase] ?? "spark"} size={13} /></span>
      <span style={{ minWidth: 0 }}>
        {showTask && <span className="live-row-title">{live.title ?? live.task_id} <span className="faint">· pass {(live.trial ?? 0) + 1}</span></span>}
        <span className="live-row-phase">
          {live.phase_label}
          {phase === "queued" && live.queue_position ? ` (#${live.queue_position})` : ""}
          {live.detail && phase !== "queued" ? <span className="faint"> · {live.detail}</span> : null}
        </span>
      </span>
      <span className="live-row-bar"><span style={{ width: `${phase === "queued" ? 0 : Math.max(3, pctDone)}%` }} /></span>
      <span className="num small" style={{ whiteSpace: "nowrap" }}>{phase === "queued" ? `starts in ~${dur(Math.max(0, (live.starts_in_s ?? 0) - dt))}` : `~${dur(eta)} left`}</span>
      {live.agent_model && <span className="small faint" style={{ whiteSpace: "nowrap" }}>{shortModel(live.agent_model)}</span>}
    </Link>
  );
}
