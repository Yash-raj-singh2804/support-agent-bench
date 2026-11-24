import type { DiffEntry } from "@/lib/api";

function Row({ row }: { row: Record<string, unknown> | null }) {
  if (!row) return <>∅</>;
  return (
    <div style={{ display: "grid", gap: 1 }}>
      {Object.entries(row).filter(([, v]) => v !== null && v !== undefined).map(([k, v]) => (
        <code key={k} style={{ fontSize: 11.5 }}><span className="faint">{k}:</span> {typeof v === "object" ? JSON.stringify(v) : String(v)}</code>
      ))}
    </div>
  );
}

const fmt = (v: unknown) => (v === null || v === undefined ? "∅" : typeof v === "object" ? JSON.stringify(v) : String(v));

/** labels: what the "expected" and "actual" sides mean in this context */
export default function DiffView({
  diff,
  labels = ["expected (gold)", "actual (agent)"],
  empty = "No differences.",
}: {
  diff: DiffEntry[];
  labels?: [string, string];
  empty?: string;
}) {
  if (!diff.length) return <p className="muted">{empty}</p>;
  return (
    <table className="diff">
      <thead>
        <tr>
          <th>table / row</th>
          <th>field</th>
          <th>{labels[0]}</th>
          <th>{labels[1]}</th>
        </tr>
      </thead>
      <tbody>
        {diff.flatMap((d, i) => {
          const key = `${d.table}:${d.pk}`;
          if (d.op === "changed")
            return Object.entries(d.fields).map(([f, v], j) => (
              <tr key={`${i}-${j}`}>
                <td>{j === 0 && <code>{key}</code>}</td>
                <td><code>{f}</code></td>
                <td className="exp"><code>{fmt(v.expected)}</code></td>
                <td className="act"><code>{fmt(v.actual)}</code></td>
              </tr>
            ));
          return [
            <tr key={i}>
              <td><code>{key}</code></td>
              <td><span className="badge">{d.op === "missing" ? `only in ${labels[0]}` : `new in ${labels[1]}`}</span></td>
              <td colSpan={2} className={d.op === "missing" ? "exp" : "act"}><Row row={d.op === "missing" ? d.expected : d.actual} /></td>
            </tr>,
          ];
        })}
      </tbody>
    </table>
  );
}
