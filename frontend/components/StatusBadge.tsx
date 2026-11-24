import type { Run } from "@/lib/api";

export default function StatusBadge({ run }: { run: Pick<Run, "status" | "reward"> }) {
  if (run.status === "queued") return <span className="badge">queued</span>;
  if (run.status === "running") return <span className="badge run"><span className="dot live" /> running</span>;
  if (run.status === "error") return <span className="badge bad">error</span>;
  return run.reward === 1 ? <span className="badge ok">✓ pass</span> : <span className="badge bad">✕ fail</span>;
}
