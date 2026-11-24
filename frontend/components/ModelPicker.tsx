"use client";

import { priceLabel, type Config } from "@/lib/api";

/** Select a model preset or type any "provider:model" spec. */
export default function ModelPicker({
  config,
  value,
  onChange,
  label,
}: {
  config: Config | null;
  value: string;
  onChange: (v: string) => void;
  label?: string;
}) {
  const presets = config?.models ?? [];
  const known = presets.some((m) => m.spec === value);
  return (
    <label className="field">
      {label && <span className="muted small">{label}</span>}
      <select value={known ? value : "__custom"} onChange={(e) => onChange(e.target.value === "__custom" ? "" : e.target.value)}>
        <optgroup label="Proprietary">
          {presets.filter((m) => !m.open_weights).map((m) => (
            <option key={m.spec} value={m.spec}>{m.label}{m.price ? ` · ${priceLabel(m.price)}` : ""}{m.configured ? "" : " (no key)"}</option>
          ))}
        </optgroup>
        <optgroup label="Open weights (Groq, Sarvam)">
          {presets.filter((m) => m.open_weights).map((m) => (
            <option key={m.spec} value={m.spec}>{m.label}{m.price ? ` · ${priceLabel(m.price)}` : ""}{m.configured ? "" : " (no key)"}</option>
          ))}
        </optgroup>
        <option value="__custom">Custom…</option>
      </select>
      {!known && (
        <input placeholder="provider:model, e.g. groq:qwen/qwen3.8-27b" value={value} onChange={(e) => onChange(e.target.value)} style={{ minWidth: 260 }} />
      )}
    </label>
  );
}
