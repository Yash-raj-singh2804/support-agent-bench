"use client";

import { useEffect, useMemo, useState } from "react";
import Markdown from "@/components/Markdown";
import { api } from "@/lib/api";

type ToolSpec = { name: string; description: string; parameters: { properties: Record<string, unknown> } };
const WRITES = new Set(["cancel_pending_order", "modify_pending_order_address", "modify_pending_order_items", "return_delivered_order_items",
  "exchange_delivered_order_items", "modify_pending_order_payment", "upgrade_shipping", "report_damaged_items", "file_lost_package_claim",
  "issue_price_adjustment", "issue_store_credit", "modify_user_address", "transfer_to_human_agents"]);

export default function PolicyPage() {
  const [md, setMd] = useState("");
  const [short, setShort] = useState(false);
  const [tools, setTools] = useState<ToolSpec[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api<{ markdown: string }>(`/policy?short=${short}`).then((p) => setMd(p.markdown)).catch((e) => setError(String(e)));
  }, [short]);
  useEffect(() => {
    api<ToolSpec[]>("/tools").then(setTools).catch(() => {});
  }, []);

  const toc = useMemo(() => md.split("\n").filter((l) => l.startsWith("## ")).map((l) => l.slice(3)), [md]);
  const words = md.split(/\s+/).filter(Boolean).length;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Policy &amp; tools</h1>
          <p className="muted" style={{ margin: 0 }}>The policy is the agent&apos;s system prompt. Tools enforce hard limits; the agent has to enforce the rest.</p>
        </div>
        <div className="seg">
          <button className={!short ? "on" : ""} onClick={() => setShort(false)}>Full policy</button>
          <button className={short ? "on" : ""} onClick={() => setShort(true)}>Condensed (short_policy variant)</button>
        </div>
      </div>
      {error && <div className="banner bad">{error}</div>}
      <div className="grid2" style={{ gridTemplateColumns: "minmax(0, 3fr) minmax(0, 1.3fr)" }}>
        <div className="panel policy-doc"><Markdown source={md} /></div>
        <div>
          <div className="panel" style={{ position: "sticky", top: 16 }}>
            <div className="panel-head"><h2>Contents</h2><span className="small faint">{words.toLocaleString()} words</span></div>
            <ol className="small" style={{ paddingLeft: 18, margin: 0, lineHeight: 1.9 }}>
              {toc.map((t) => <li key={t}><a href={`#${t.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`}>{t.replace(/^\d+\.\s*/, "")}</a></li>)}
            </ol>
            <div className="panel-head" style={{ marginTop: 18 }}><h2>Tools</h2><span className="small faint">{tools.length} ({tools.filter((t) => WRITES.has(t.name)).length} write)</span></div>
            {tools.map((t) => (
              <div key={t.name} style={{ marginBottom: 10 }}>
                <code style={{ color: WRITES.has(t.name) ? "var(--accent)" : undefined }}><b>{t.name}</b></code>
                <span className="faint small"> ({Object.keys(t.parameters.properties).join(", ")})</span>
                <div className="small muted">{t.description}</div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </>
  );
}
