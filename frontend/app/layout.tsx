import type { Metadata } from "next";
import Sidebar from "@/components/Sidebar";
import "./globals.css";

export const metadata: Metadata = {
  title: "Support Agent Bench",
  description: "τ-bench style evaluation of a retail support agent",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <div className="shell">
          <Sidebar />
          <div className="main">
            <main className="container">{children}</main>
          </div>
        </div>
      </body>
    </html>
  );
}
