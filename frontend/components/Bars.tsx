export const CATEGORY_COLORS: Record<string, string> = {
  wrong_arguments: "#e8833a",
  missed_action: "#d9534f",
  unauthorized_action: "#a23b72",
  extra_action: "#c9a227",
  no_confirmation: "#7b61ff",
  authentication: "#3b8ea5",
  wrong_info: "#5b8c5a",
  unnecessary_transfer: "#8d6e63",
  user_simulator_error: "#9aa0a6",
  infra_error: "#555",
  other: "#b0b0b0",
  unanalyzed: "#d0d0d0",
};

export const catColor = (c: string) => CATEGORY_COLORS[c] ?? "#999";
export const catLabel = (c: string) => c.replace(/_/g, " ");

/** Horizontal bar list for counts or rates. */
export function Bars({
  data,
  max,
  format = (v) => String(v),
  color,
  titles,
}: {
  data: Record<string, number>;
  max?: number;
  format?: (v: number) => string;
  color?: (k: string) => string;
  titles?: Record<string, string>;
}) {
  const entries = Object.entries(data);
  if (!entries.length) return <p className="muted small">No data yet.</p>;
  const m = max ?? Math.max(...entries.map(([, v]) => v), 1);
  return (
    <div className="bars">
      {entries.map(([k, v]) => (
        <div key={k} className="bar-row" title={titles?.[k]}>
          <span className="bar-label">{catLabel(k)}</span>
          <span className="bar-track">
            <span className="bar-fill" style={{ width: `${(v / m) * 100}%`, background: color ? color(k) : "var(--accent)" }} />
          </span>
          <span className="bar-value">{format(v)}</span>
        </div>
      ))}
    </div>
  );
}

/** One stacked bar with a legend: share of failures per category. */
export function Stacked({ data }: { data: Record<string, number> }) {
  const total = Object.values(data).reduce((a, b) => a + b, 0);
  if (!total) return <span className="muted small">no failures</span>;
  return (
    <div>
      <div className="stack">
        {Object.entries(data).map(([k, v]) => (
          <span key={k} title={`${catLabel(k)}: ${v}`} style={{ width: `${(v / total) * 100}%`, background: catColor(k) }} />
        ))}
      </div>
      <div className="legend small">
        {Object.entries(data).map(([k, v]) => (
          <span key={k}><i style={{ background: catColor(k) }} />{catLabel(k)} {v}</span>
        ))}
      </div>
    </div>
  );
}
