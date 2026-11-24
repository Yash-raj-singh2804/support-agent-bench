"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import Icon from "@/components/Icon";
import { api, type Config, type Run } from "@/lib/api";

const NAV = [
  { section: "Agent" },
  { href: "/", label: "Overview", icon: "home" },
  { href: "/playground", label: "Chat playground", icon: "chat" },
  { href: "/tasks", label: "Run tasks", icon: "play" },
  { section: "Evaluation" },
  { href: "/experiments", label: "Experiments", icon: "flask" },
  { href: "/analysis", label: "Failure analysis", icon: "bug" },
  { href: "/usage", label: "Token usage", icon: "coins" },
  { section: "Reference" },
  { href: "/policy", label: "Policy & tools", icon: "book" },
] as const;

export default function Sidebar() {
  const path = usePathname();
  const [config, setConfig] = useState<Config | null>(null);
  const [live, setLive] = useState(0);

  useEffect(() => {
    api<Config>("/config").then(setConfig).catch(() => {});
    const poll = () => api<Run[]>("/runs?kind=eval&limit=60")
      .then((rs) => setLive(rs.filter((r) => r.status === "running" || r.status === "queued").length))
      .catch(() => {});
    poll();
    const h = setInterval(poll, 4000);
    return () => clearInterval(h);
  }, []);

  const active = (href: string) => (href === "/" ? path === "/" : path.startsWith(href));

  return (
    <aside className="sidebar">
      <Link href="/" className="brand">
        <span className="logo"><Icon name="bolt" size={17} /></span>
        <span>Support Agent Bench<small>τ-bench style evals</small></span>
      </Link>
      {NAV.map((n, i) =>
        "section" in n ? (
          <div key={i} className="nav-section">{n.section}</div>
        ) : (
          <Link key={n.href} href={n.href} className={`nav-link ${active(n.href) ? "active" : ""}`}>
            <Icon name={n.icon} />
            {n.label}
            {n.href === "/tasks" && live > 0 && <span className="badge run" style={{ marginLeft: "auto" }}>{live}</span>}
          </Link>
        ),
      )}
      <div className="sidebar-foot">
        <div className="faint" style={{ marginBottom: 6, textTransform: "uppercase", letterSpacing: ".08em", fontSize: 10.5 }}>API keys</div>
        {config ? (
          Object.entries(config.providers).map(([p, ok]) => (
            <div key={p} className="key-row"><span>{p}</span><span className={`dot ${ok ? "on" : ""}`} title={ok ? "configured" : "missing"} /></div>
          ))
        ) : (
          <div className="faint">backend offline</div>
        )}
        {live > 0 && <div className="key-row" style={{ marginTop: 6 }}><span>{live} episode{live > 1 ? "s" : ""} running</span><span className="dot live" /></div>}
      </div>
    </aside>
  );
}
