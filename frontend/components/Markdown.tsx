import { Fragment, type ReactNode } from "react";

/** Tiny markdown renderer for the policy: headings, lists, tables, bold, inline code, rules. */
function inline(text: string, key = 0): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /(\*\*[^*]+\*\*|`[^`]+`)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let i = 0;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const t = m[0];
    out.push(t.startsWith("**") ? <b key={`${key}-${i++}`}>{t.slice(2, -2)}</b> : <code key={`${key}-${i++}`}>{t.slice(1, -1)}</code>);
    last = m.index + t.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

export default function Markdown({ source }: { source: string }) {
  const lines = source.split("\n");
  const blocks: ReactNode[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    const h = /^(#{1,4})\s+(.*)$/.exec(line);
    if (h) {
      const lvl = h[1].length;
      const id = h[2].toLowerCase().replace(/[^a-z0-9]+/g, "-");
      blocks.push(lvl === 1 ? <h1 key={i} id={id}>{inline(h[2], i)}</h1> : lvl === 2 ? <h2 key={i} id={id} className="md-h2">{inline(h[2], i)}</h2> : <h3 key={i} id={id}>{inline(h[2], i)}</h3>);
      i++;
      continue;
    }
    if (/^---+$/.test(line.trim())) { blocks.push(<hr key={i} />); i++; continue; }
    if (line.startsWith("|")) {
      const rows: string[][] = [];
      while (i < lines.length && lines[i].startsWith("|")) {
        if (!/^\|[\s|:-]+\|$/.test(lines[i].trim())) rows.push(lines[i].trim().slice(1, -1).split("|").map((c) => c.trim()));
        i++;
      }
      const [head, ...body] = rows;
      blocks.push(
        <table key={`t${i}`} className="md-table">
          <thead><tr>{head.map((c, j) => <th key={j}>{inline(c, j)}</th>)}</tr></thead>
          <tbody>{body.map((r, k) => <tr key={k}>{r.map((c, j) => <td key={j}>{inline(c, j)}</td>)}</tr>)}</tbody>
        </table>,
      );
      continue;
    }
    if (/^\s*([-*]|\d+\.)\s/.test(line)) {
      const ordered = /^\s*\d+\./.test(line);
      const items: { text: string; sub: string[] }[] = [];
      while (i < lines.length && (/^\s*([-*]|\d+\.)\s/.test(lines[i]) || (/^\s{2,}\S/.test(lines[i]) && items.length))) {
        const l = lines[i];
        if (/^\s{2,}([-*]|\d+\.)\s/.test(l)) items[items.length - 1].sub.push(l.replace(/^\s*([-*]|\d+\.)\s/, ""));
        else if (/^\s{2,}\S/.test(l) && !/^\s*([-*]|\d+\.)\s/.test(l)) items[items.length - 1].text += " " + l.trim();
        else items.push({ text: l.replace(/^\s*([-*]|\d+\.)\s/, ""), sub: [] });
        i++;
      }
      const Tag = ordered ? "ol" : "ul";
      blocks.push(
        <Tag key={`l${i}`}>
          {items.map((it, k) => (
            <li key={k}>{inline(it.text, k)}{it.sub.length > 0 && <ul>{it.sub.map((s, j) => <li key={j}>{inline(s, j)}</li>)}</ul>}</li>
          ))}
        </Tag>,
      );
      continue;
    }
    const para: string[] = [];
    while (i < lines.length && lines[i].trim() && !/^(#|\||\s*([-*]|\d+\.)\s|---)/.test(lines[i])) { para.push(lines[i].trim()); i++; }
    blocks.push(<p key={`p${i}`}>{inline(para.join(" "), i)}</p>);
  }
  return <div className="md">{blocks.map((b, k) => <Fragment key={k}>{b}</Fragment>)}</div>;
}
