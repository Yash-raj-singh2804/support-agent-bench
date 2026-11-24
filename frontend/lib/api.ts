export type ToolCall = { id: string; name: string; arguments: Record<string, unknown> };

export type Message = {
  id: number;
  idx: number;
  role: "user" | "assistant" | "tool";
  content: string | null;
  tool_calls: ToolCall[] | null;
  tool_call_id: string | null;
  tool_name: string | null;
};

export type Task = {
  id: string;
  category: string;
  title: string;
  user_scenario: string;
  gold_actions: { name: string; arguments: Record<string, unknown> }[];
  required_outputs: string[];
  runs?: number;
  successes?: number;
};

export type DiffEntry =
  | { table: string; pk: string; op: "missing" | "extra"; expected: Record<string, unknown> | null; actual: Record<string, unknown> | null }
  | { table: string; pk: string; op: "changed"; fields: Record<string, { expected: unknown; actual: unknown }> };

export type Run = {
  id: string;
  kind: "eval" | "playground";
  task_id: string | null;
  batch_id: string | null;
  trial: number;
  status: "queued" | "running" | "done" | "error";
  reward: number | null;
  db_match: boolean | null;
  outputs_ok: boolean | null;
  process_ok?: boolean | null;
  grounded_ok?: boolean | null;
  hallucinations?: string[] | null;
  grounding_revisions?: number | null;
  missing_outputs: string[] | null;
  agent_hash: string | null;
  gold_hash: string | null;
  diff?: DiffEntry[] | null;
  experiment_id: string | null;
  variant: Variant | null;
  variant_label: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  llm_calls: number | null;
  duration_s: number | null;
  customer_tokens: number | null;
  judge_tokens: number | null;
  agent_cost_usd: number | null;
  cost_usd: number | null;
  signals?: Signals | null;
  analysis: Analysis | null;
  schema_name: string | null;
  agent_model: string | null;
  user_model: string | null;
  end_reason: string | null;
  error: string | null;
  created_at: string;
  finished_at: string | null;
  messages?: Message[];
  task?: Task | null;
};

export type Variant = { think: boolean; checklist: boolean; guards: boolean; short_policy: boolean };

export type Analysis = {
  category: string;
  responsible: "agent" | "user_simulator" | "task" | "infra";
  explanation: string;
  critical_message_idx?: number | null;
  confidence?: string;
  method: "llm_judge" | "rules";
  judge_model?: string;
  rule_based_guess?: string;
  judge_error?: string;
};

export type Signals = {
  write_calls: { msg_idx: number; name: string; ok: boolean; confirmed: boolean; authenticated_as: string | null }[];
  n_tool_calls: number;
  tool_errors: number;
  guard_blocks: number;
  violations: string[];
  matched_gold_actions: number;
  gold_actions: number;
  missed_gold_tools: string[];
  extra_writes: string[];
  wrong_args_tools: string[];
  customer_turns: number;
};

export type Summary = {
  total: number;
  finished: number;
  tasks: number;
  avg_reward: number | null;
  pass_hat_k: Record<string, number | null>;
  adjusted_pass_1: number | null;
  by_category: Record<string, number>;
  failure_categories: Record<string, number>;
  failure_responsible: Record<string, number>;
  unanalyzed_failures: number;
  violating_passes: number;
  passes: number;
  avg_tokens: number | null;
  avg_total_tokens: number | null;
  total_tokens: number;
  avg_cost_usd: number | null;
  avg_agent_cost_usd: number | null;
  total_cost_usd: number | null;
  avg_duration_s: number | null;
  errors: number;
  rate_limited: number;
};

export type RoleTokens = { calls: number; input: number; output: number; cached: number; reasoning: number; cost_usd: number | null; wait_ms: number };
export type Episode = {
  id: string; task_id: string; title: string | null; trial: number; status: Run["status"]; reward: number | null;
  error: string | null; rate_limited: boolean; category: string | null; duration_s: number | null;
  tokens: Partial<Record<"agent" | "customer" | "judge", RoleTokens>>; input: number; output: number; cost_usd: number | null; wait_ms: number;
};
export type BatchDetail = Summary & {
  batch_id: string; created_at: string; agent_model: string; user_model: string; variant_label: string; experiment_id: string | null;
  avg_input: number; avg_output: number; total_input: number; total_output: number; total_wait_ms: number;
  progress: BatchProgress;
  role_totals: Record<"agent" | "customer" | "judge", { input: number; output: number; calls: number }>;
  episodes: Episode[];
};

export type Batch = Summary & {
  eta_s: number | null;
  batch_id: string;
  experiment_id: string | null;
  created_at: string;
  agent_model: string;
  user_model: string;
  variant_label: string;
};

export type ExperimentVariant = Summary & { variant: string; name: string; config: Variant; batch_id: string; delta_pass_1: number | null;
  progress: Omit<BatchProgress, "live"> | null };

export type Experiment = {
  experiment_id: string;
  created_at: string;
  agent_model: string;
  user_model: string;
  tasks: number;
  finished: number;
  total: number;
  variants: ExperimentVariant[];
  progress: BatchProgress | null;
  matrix?: Record<string, Record<string, number[]>>;
};

export type Price = { input: number; output: number; currency?: string; cached_input?: number; long_context?: { threshold: number; input: number; output: number } };
export type ModelPreset = { spec: string; label: string; open_weights: boolean; configured: boolean; price: Price | null };

export type TraceCall = {
  id: number;
  seq: number;
  turn: number;
  role: "agent" | "customer" | "judge";
  model: string;
  input_tokens: number;
  output_tokens: number;
  cached_tokens: number;
  reasoning_tokens: number;
  latency_ms: number;
  wait_ms: number | null;
  tool_calls: number;
  n_messages: number;
  with_tools: boolean;
  cost_usd: number | null;
  error: string | null;
};
export type RoleTotals = { calls: number; input: number; output: number; cached: number; reasoning: number; latency_ms: number; wait_ms: number; cost_usd: number | null };
export type Trace = {
  calls: TraceCall[];
  by_role: Partial<Record<"agent" | "customer" | "judge", RoleTotals>>;
  by_turn: { turn: number; agent_tokens: number; customer_tokens: number; calls: number; agent_calls: number; cost_usd: number }[];
  total: { calls: number; input: number; output: number; cost_usd: number | null; latency_ms: number; wait_ms: number };
};

export type Usage = {
  days: number;
  total: { calls: number; input: number; output: number; cached: number; reasoning: number; cost_usd: number | null; unpriced_models: string[] };
  by_model: { model: string; calls: number; input: number; output: number; cached: number; reasoning: number; cost_usd: number | null; price: Price | null;
    by_role: Record<string, { calls: number; tokens: number; cost_usd: number | null; avg_latency_ms: number }> }[];
  by_role: Record<string, { calls: number; tokens: number; cost_usd: number | null }>;
  by_kind: Record<string, { runs: number; tokens: number; cost_usd: number | null }>;
  by_day: { day: string; agent: number; customer: number; judge: number; cost_usd: number }[];
  top_runs: { id: string; kind: string; task_id: string | null; agent_model: string; variant_label: string | null; reward: number | null;
    input_tokens: number | null; output_tokens: number | null; customer_tokens: number | null; judge_tokens: number | null; cost_usd: number | null; created_at: string }[];
};

export type Config = {
  agent_model: string;
  user_model: string;
  judge_model: string;
  max_concurrency: number;
  auto_analyze: boolean;
  providers: Record<string, boolean>;
  api_key_configured: boolean;
  models: ModelPreset[];
  variants: Record<string, Variant>;
  taxonomy: Record<string, string>;
};

export type AnalysisOverview = {
  summary: Summary;
  failures: ({ id: string; task_id: string; batch_id: string; variant_label: string; agent_model: string; created_at: string } & Partial<Analysis>)[];
  violating_passes: { id: string; task_id: string; violations: string[] }[];
  by_task: Record<string, Record<string, number>>;
  taxonomy: Record<string, string>;
};

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
    ...init,
    headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
    cache: "no-store",
  });
  const body = await res.text();
  if (!res.ok) {
    let detail = body;
    try {
      detail = JSON.parse(body).detail ?? body;
    } catch {}
    throw new Error(`${res.status}: ${detail}`);
  }
  return (body ? JSON.parse(body) : null) as T;
}

export const pct = (x: number | null | undefined) => (x == null ? "–" : `${(x * 100).toFixed(1)}%`);

export const num = (x: number | null | undefined, d = 0) => (x == null ? "–" : x.toLocaleString(undefined, { maximumFractionDigits: d }));
export const signedPct = (x: number | null | undefined) => (x == null ? "" : `${x >= 0 ? "+" : ""}${(x * 100).toFixed(1)} pts`);
export const shortModel = (spec: string | null | undefined) => (spec ?? "").replace(/^(anthropic|openai|groq|gemini|sarvam):/, "");
export const usd = (x: number | null | undefined) =>
  x == null ? "–" : x === 0 ? "$0" : x < 0.01 ? `$${x.toFixed(4)}` : x < 10 ? `$${x.toFixed(3)}` : `$${x.toFixed(2)}`;
export const compact = (x: number | null | undefined) =>
  x == null ? "–" : x >= 1e6 ? `${(x / 1e6).toFixed(2)}M` : x >= 1e4 ? `${(x / 1e3).toFixed(1)}k` : Math.round(x).toLocaleString();
export const ROLE_COLORS: Record<string, string> = { agent: "#7c8cff", customer: "#38bdf8", judge: "#f59e0b" };
export const secs = (ms: number | null | undefined) => (ms == null ? "–" : ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`);

export type LiveInfo = {
  id: string; task_id?: string; title?: string | null; batch_id?: string | null; trial?: number; agent_model?: string;
  variant_label?: string | null; status: string; phase: string | null; phase_label?: string; detail?: string | null;
  turn?: number; expected_turns?: number; agent_steps?: number; tool_calls?: number; last_tool?: string | null;
  elapsed_s?: number | null; phase_elapsed_s?: number | null; est_total_s?: number; typical_s?: number; slower_than_usual?: boolean; eta_s?: number; progress?: number;
  wait_left_s?: number; based_on?: string; queue_position?: number | null; starts_in_s?: number | null;
};
export type BatchProgress = { done: number; total: number; running: number; queued: number; parallelism: number;
  eta_s: number; elapsed_s: number | null; progress: number; ends_at: string | null; live: LiveInfo[] };
export type Estimate = { episodes: number; eta_s: number; parallelism: number; avg_episode_s: number; ahead_in_queue: number; based_on: string };

export const dur = (s: number | null | undefined) => {
  if (s == null || !isFinite(s)) return "–";
  s = Math.max(0, Math.round(s));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
};

export const clock = (iso: string | null | undefined, offsetS = 0) => {
  if (!iso) return "";
  const d = new Date(new Date(iso).getTime() - offsetS * 1000);
  const sameDay = d.toDateString() === new Date().toDateString();
  return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) + (sameDay ? "" : ` ${d.toLocaleDateString([], { month: "short", day: "numeric" })}`);
};

export const priceLabel = (p: Price | null | undefined) => {
  if (!p) return "";
  const sym = p.currency === "INR" ? "₹" : "$";
  return `${sym}${p.input}/${sym}${p.output}`;
};
