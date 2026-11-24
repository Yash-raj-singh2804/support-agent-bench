import Icon from "@/components/Icon";
import type { Message } from "@/lib/api";

export const WRITE_TOOLS = new Set([
  "cancel_pending_order",
  "modify_pending_order_address",
  "modify_pending_order_items",
  "return_delivered_order_items",
  "exchange_delivered_order_items",
]);

function fmtArgs(args: Record<string, unknown>) {
  return Object.entries(args)
    .filter(([k]) => k !== "policy_checklist")
    .map(([k, v]) => `${k}=${JSON.stringify(v)}`)
    .join(", ");
}

function prettyResult(s: string) {
  try {
    return JSON.stringify(JSON.parse(s), null, 2);
  } catch {
    return s;
  }
}

export default function Transcript({
  messages,
  customerLabel = "Customer (simulated)",
  highlightIdx,
  pending = false,
}: {
  messages: Message[];
  customerLabel?: string;
  highlightIdx?: number | null;
  /** show a typing indicator for the agent */
  pending?: boolean;
}) {
  const results = new Map<string, Message>();
  for (const m of messages) if (m.role === "tool" && m.tool_call_id) results.set(m.tool_call_id, m);

  return (
    <div className="transcript">
      {messages.map((m) => {
        if (m.role === "tool") return null; // rendered with its call
        const crit = m.idx === highlightIdx ? "critical" : "";
        if (m.role === "user")
          return (
            <div key={m.id} className="turn user">
              <div className="avatar user"><Icon name="user" size={14} /></div>
              <div className={`msg user ${crit}`} title={`#${m.idx}`}>
                <div className="who">{customerLabel}</div>
                {m.content}
              </div>
            </div>
          );
        return (
          <div key={m.id} style={{ display: "contents" }}>
            {m.content && (
              <div className="turn">
                <div className="avatar agent"><Icon name="bolt" size={14} /></div>
                <div className={`msg assistant ${crit}`} title={`#${m.idx}`}>
                  <div className="who">Agent</div>
                  {m.content}
                </div>
              </div>
            )}
            {(m.tool_calls ?? []).map((tc) => {
              const res = results.get(tc.id);
              const isErr = res?.content?.startsWith("Error");
              const write = WRITE_TOOLS.has(tc.name);
              const checklist = tc.arguments.policy_checklist as Record<string, unknown> | undefined;
              return (
                <details key={tc.id} className={`tool ${crit}`}>
                  <summary>
                    <span className={`tool-ico ${write ? "write" : ""}`}><Icon name={write ? "db" : tc.name === "think" ? "spark" : "tool"} size={11} /></span>
                    <span className={`name ${write ? "write" : ""}`}>{tc.name}</span>
                    <span className="args">({fmtArgs(tc.arguments)})</span>
                    {res ? (
                      isErr ? <span className="err">✕ {res.content?.replace(/^Error:\s*/, "")}</span> : <span className="ok">✓</span>
                    ) : (
                      <span className="muted">running…</span>
                    )}
                  </summary>
                  {checklist && <pre>{"policy_checklist " + JSON.stringify(checklist, null, 2)}</pre>}
                  {res && !isErr && <pre>{prettyResult(res.content ?? "")}</pre>}
                </details>
              );
            })}
          </div>
        );
      })}
      {pending && (
        <div className="turn">
          <div className="avatar agent"><Icon name="bolt" size={14} /></div>
          <div className="msg assistant typing"><i /><i /><i /></div>
        </div>
      )}
    </div>
  );
}
