"use client";

import { useEffect, useState } from "react";
import Icon from "@/components/Icon";
import { LiveRow } from "@/components/LiveStatus";
import { clock, dur, type BatchProgress } from "@/lib/api";

/** Overall progress for a batch or an experiment, with ETA, end time and the active passes. */
export default function ProgressCard({
  title,
  p,
  maxRows = 10,
  children,
}: {
  title: string;
  p: BatchProgress;
  maxRows?: number;
  children?: React.ReactNode;
}) {
  const [base, setBase] = useState(() => Date.now());
  const [, tick] = useState(0);
  useEffect(() => setBase(Date.now()), [p]);
  useEffect(() => {
    const h = setInterval(() => tick((t) => t + 1), 1000);
    return () => clearInterval(h);
  }, []);
  const dt = (Date.now() - base) / 1000;
  const eta = Math.max(0, p.eta_s - dt);

  return (
    <div className="live-card">
      <div className="live-head">
        <span className="live-orb"><Icon name="play" size={16} /></span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="live-title">{title}: {p.done} of {p.total} episodes finished</div>
          <div className="live-detail">
            {p.running} running · {p.queued} queued · {p.parallelism} at a time
            {p.elapsed_s != null && ` · ${dur(p.elapsed_s + dt)} elapsed`}
          </div>
        </div>
        <div className="live-eta">
          <div className="v">~{dur(eta)}</div>
          <div className="l">left{p.ends_at ? ` · ends ~${clock(p.ends_at)}` : ""}</div>
        </div>
      </div>
      <div className="live-bar" style={{ margin: "14px 0" }}><span style={{ width: `${Math.max(2, p.progress * 100)}%` }} /></div>
      {children}
      {p.live.length > 0 && (
        <div className="live-list">
          {p.live.slice(0, maxRows).map((l) => <LiveRow key={l.id} live={l} />)}
          {p.live.length > maxRows && <div className="small faint" style={{ padding: "4px 10px" }}>+{p.live.length - maxRows} more queued</div>}
        </div>
      )}
    </div>
  );
}
