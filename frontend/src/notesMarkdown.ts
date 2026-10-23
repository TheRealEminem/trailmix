import { marked } from "marked";
import type { JSONContent } from "@tiptap/core";
import type { MyNote } from "./api";

/*
 * Your notes are kept as they always were, [{t, text}] (backend my_notes_json), one entry per paragraph, heading or
 * list item, with its text as a line of markdown ("# Plan", "- ask Sam", "  - nested", "1. first", "**bold**",
 * "[a link](https://…)"). So the notes AI, Catch me up and the history read them as lines, and the editor turns
 * them back into a page. `t` is the moment in the recording each block was started (null when typed afterwards).
 */

interface Item {
  depth: number;
  ordered: boolean;
  html: string;
  t: number | null;
}

const LIST = /^( *)([-*+]|\d+[.)]) +([\s\S]*)$/;
const HEADING = /^(#{1,3}) +([\s\S]*)$/;

/** Markdown inline text as HTML. Angle brackets stay text: an old note saying "<div>" must not vanish. */
const inline = (text: string) =>
  marked.parseInline(text.replace(/</g, "&lt;").replace(/>/g, "&gt;"), { gfm: true, breaks: true, async: false }) as string;

const tAttr = (t: number | null) => (t != null ? ` data-t="${t}"` : "");

function listHtml(items: Item[], start: number, depth: number): [string, number] {
  const ordered = items[start].ordered;
  let out = ordered ? "<ol>" : "<ul>";
  let i = start;
  while (i < items.length && items[i].depth === depth && items[i].ordered === ordered) {
    const it = items[i++];
    let inner = `<p>${it.html}</p>`;
    while (i < items.length && items[i].depth > depth) {
      const [nested, next] = listHtml(items, i, depth + 1);
      inner += nested;
      i = next;
    }
    out += `<li${tAttr(it.t)}>${inner}</li>`;
  }
  return [out + (ordered ? "</ol>" : "</ul>"), i];
}

/** Your notes as HTML for the editor; each block carries its moment as data-t. */
export function notesToHtml(notes: MyNote[]): string {
  const out: string[] = [];
  let list: Item[] = [];
  const flush = () => {
    for (let i = 0; i < list.length; ) {
      const [html, next] = listHtml(list, i, list[i].depth);
      out.push(html);
      i = next;
    }
    list = [];
  };
  for (const n of notes) {
    const text = n.text ?? "";
    const t = n.t ?? null;
    const li = LIST.exec(text);
    if (li) {
      // Two spaces a level; a list can't start deeper than one level below the item before it.
      const prev = list.length ? list[list.length - 1].depth : -1;
      list.push({ depth: Math.min(Math.floor(li[1].length / 2), prev + 1), ordered: /\d/.test(li[2]), html: inline(li[3]), t });
      continue;
    }
    flush();
    const h = HEADING.exec(text);
    out.push(h ? `<h${h[1].length}${tAttr(t)}>${inline(h[2])}</h${h[1].length}>` : `<p${tAttr(t)}>${inline(text)}</p>`);
  }
  flush();
  return out.join("");
}

// ── Back to lines ───────────────────────────────────────────────────────

const MARKS = ["link", "bold", "italic", "strike", "code"] as const;
type MarkName = (typeof MARKS)[number];

function open(mark: MarkName) {
  return mark === "link" ? "[" : mark === "bold" ? "**" : mark === "italic" ? "*" : mark === "strike" ? "~~" : "`";
}

function close(mark: MarkName, href: string) {
  if (mark === "link") return `](${/[\s()]/.test(href) ? `<${href}>` : href})`;
  return open(mark);
}

/** Characters that would read as formatting, escaped so what you typed comes back as you typed it. */
const escape = (text: string) => text.replace(/[\\*`~[\]]/g, (c) => `\\${c}`);

/** A paragraph's text and formatting as one line of markdown. Spaces at the edge of bold or italic move outside it,
 * where markdown expects them. */
function inlineMd(nodes: JSONContent[] = []): string {
  let out = "";
  let stack: { name: MarkName; href: string }[] = [];
  let pendingSpace = "";
  const closeTo = (keep: number) => {
    while (stack.length > keep) {
      const m = stack.pop()!;
      out += close(m.name, m.href);
    }
  };
  for (const node of nodes) {
    if (node.type === "hardBreak") {
      closeTo(0);
      out += pendingSpace + "\n";
      pendingSpace = "";
      continue;
    }
    const text = node.text ?? "";
    const core = text.trim();
    if (!core) {
      pendingSpace += text;
      continue;
    }
    const marks = MARKS.flatMap((name) => {
      const m = node.marks?.find((x) => x.type === name);
      const href = name === "link" ? String(m?.attrs?.href ?? "") : "";
      // A link that is just its address stays a bare address (it becomes a link again when loaded).
      return m && !(name === "link" && href === core) ? [{ name, href }] : [];
    });
    let same = 0;
    while (same < stack.length && same < marks.length && stack[same].name === marks[same].name && stack[same].href === marks[same].href)
      same++;
    closeTo(same);
    const lead = text.slice(0, text.length - text.trimStart().length);
    out += pendingSpace + lead;
    for (const m of marks.slice(same)) {
      out += open(m.name);
      stack.push(m);
    }
    const isCode = marks.some((m) => m.name === "code");
    out += isCode ? core.replace(/`/g, "'") : escape(core);
    pendingSpace = text.slice(text.trimEnd().length);
  }
  closeTo(0);
  return out + pendingSpace;
}

/** Plain text that starts like a heading or list item is escaped, so it stays a paragraph. */
const paragraphMd = (md: string) => md.replace(/^(#{1,6} )/, "\\$1").replace(/^([-+] )/, "\\$1").replace(/^(\d+)([.)] )/, "$1\\$2");

const stamp = (node: JSONContent): number | null => {
  const t = node.attrs?.t;
  return typeof t === "number" ? t : null;
};

function listMd(list: JSONContent, depth: number, out: MyNote[]) {
  const ordered = list.type === "orderedList";
  let n = typeof list.attrs?.start === "number" ? list.attrs.start : 1;
  for (const item of list.content ?? []) {
    const texts: string[] = [];
    const nested: JSONContent[] = [];
    for (const child of item.content ?? []) {
      if (child.type === "bulletList" || child.type === "orderedList") nested.push(child);
      else texts.push(inlineMd(child.content));
    }
    const text = texts.filter((x) => x.trim()).join("\n");
    if (text.trim()) out.push({ t: stamp(item), text: `${"  ".repeat(depth)}${ordered ? `${n}.` : "-"} ${text}` });
    n++;
    for (const sub of nested) listMd(sub, depth + 1, out);
  }
}

/** The editor's page as your notes, one entry per block. Empty blocks are left out. */
export function docToNotes(doc: JSONContent): MyNote[] {
  const out: MyNote[] = [];
  for (const block of doc.content ?? []) {
    if (block.type === "bulletList" || block.type === "orderedList") {
      listMd(block, 0, out);
      continue;
    }
    const text = inlineMd(block.content);
    if (!text.trim()) continue;
    if (block.type === "heading") {
      const level = Math.min(3, Number(block.attrs?.level ?? 1));
      out.push({ t: stamp(block), text: `${"#".repeat(level)} ${text}` });
    } else out.push({ t: stamp(block), text: paragraphMd(text) });
  }
  return out;
}
