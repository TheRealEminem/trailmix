/**
 * What's on screen, as text, for a problem report: the app's own labels, buttons and messages, with every part
 * marked data-private (titles, notes, transcripts, names, tasks, workspaces…) replaced by "[transcript hidden]"
 * and the like. Only what's in view counts, and the report panel itself is left out (data-report-ui).
 */
export function screenText(root: Element = document.body): string {
  const lines: string[] = [];
  const hidden = new Set<Element>();
  let line = "";
  let lastBlock: Element | null = null;
  const flush = () => {
    const t = line.replace(/\s+/g, " ").trim();
    if (t && t !== lines[lines.length - 1]) lines.push(t);
    line = "";
  };
  const inView = (el: Element) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth;
  };
  const blockOf = (el: Element): Element => {
    let e: Element | null = el;
    while (e && e !== root) {
      const d = getComputedStyle(e).display;
      if (d !== "inline" && d !== "inline-block" && d !== "contents") return e;
      e = e.parentElement;
    }
    return root;
  };

  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    const el = n.parentElement;
    if (!el || !n.textContent?.trim()) continue;
    if (el.closest("[data-report-ui], script, style, noscript, [aria-hidden='true']")) continue;
    if (!inView(el)) continue;
    const secret = el.closest("[data-private]");
    if (secret) {
      if (!hidden.has(secret)) {
        hidden.add(secret);
        flush();
        lines.push(`[${secret.getAttribute("data-private") || "personal content"} hidden]`);
        lastBlock = null;
      }
      continue;
    }
    const block = blockOf(el);
    if (block !== lastBlock) flush();
    lastBlock = block;
    line += " " + n.textContent;
  }
  flush();
  return lines.join("\n");
}
