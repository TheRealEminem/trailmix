import { describe, expect, test } from "vitest";
import type { JSONContent } from "@tiptap/core";
import { docToNotes, notesToHtml } from "./notesMarkdown";

describe("notesToHtml", () => {
  test("paragraphs and headings carry their moment, and inline markdown becomes HTML", () => {
    const html = notesToHtml([
      { t: 12.5, text: "# Plan" },
      { t: null, text: "Ask **Sam** about [the deck](https://example.com/deck)" },
    ]);
    expect(html).toBe(
      '<h1 data-t="12.5">Plan</h1><p>Ask <strong>Sam</strong> about <a href="https://example.com/deck">the deck</a></p>',
    );
  });

  test("angle brackets stay text", () => {
    expect(notesToHtml([{ t: null, text: "the <div> tag" }])).toBe("<p>the &lt;div&gt; tag</p>");
  });

  test("list items nest two spaces a level, and a list can't start deeper than one level down", () => {
    const html = notesToHtml([
      { t: 1, text: "- one" },
      { t: 2, text: "  - one point one" },
      { t: 3, text: "      - too deep: treated as the next level only" },
      { t: 4, text: "1. first" },
    ]);
    expect(html).toBe(
      '<ul><li data-t="1"><p>one</p><ul><li data-t="2"><p>one point one</p><ul><li data-t="3"><p>too deep: treated as the next level only</p></li></ul></li></ul></li></ul>' +
        '<ol><li data-t="4"><p>first</p></li></ol>',
    );
  });
});

describe("docToNotes", () => {
  const p = (text: string, marks?: JSONContent["marks"], t?: number): JSONContent => ({
    type: "paragraph",
    attrs: t === undefined ? undefined : { t },
    content: [{ type: "text", text, marks }],
  });

  test("blocks become lines of markdown with their moments; empty blocks are left out", () => {
    const notes = docToNotes({
      type: "doc",
      content: [
        { type: "heading", attrs: { level: 2, t: 3 }, content: [{ type: "text", text: "Decisions" }] },
        p("Ship it", [{ type: "bold" }], 9),
        { type: "paragraph" },
        p("https://x.test", [{ type: "link", attrs: { href: "https://x.test" } }]),
      ],
    });
    expect(notes).toEqual([
      { t: 3, text: "## Decisions" },
      { t: 9, text: "**Ship it**" },
      { t: null, text: "https://x.test" }, // a link that is just its address stays bare
    ]);
  });

  test("text that would read as formatting is escaped, so it comes back as typed", () => {
    const notes = docToNotes({ type: "doc", content: [p("# not a heading"), p("1. not a list"), p("a*b `c`")] });
    expect(notes.map((n) => n.text)).toEqual(["\\# not a heading", "1\\. not a list", "a\\*b \\`c\\`"]);
  });

  test("lists keep their nesting and numbering", () => {
    const item = (text: string, nested?: JSONContent): JSONContent => ({
      type: "listItem",
      content: [p(text), ...(nested ? [nested] : [])],
    });
    const notes = docToNotes({
      type: "doc",
      content: [
        { type: "bulletList", content: [item("one", { type: "orderedList", attrs: { start: 3 }, content: [item("three"), item("four")] }), item("two")] },
      ],
    });
    expect(notes.map((n) => n.text)).toEqual(["- one", "  3. three", "  4. four", "- two"]);
  });

  test("notes survive a round trip through the editor's HTML and back", () => {
    const original = [
      { t: 1, text: "# Plan" },
      { t: 2, text: "Ask **Sam** about *the deck*" },
      { t: 3, text: "- one" },
      { t: 4, text: "  - nested" },
    ];
    // The editor would parse notesToHtml(original) into a doc like this one.
    const doc: JSONContent = {
      type: "doc",
      content: [
        { type: "heading", attrs: { level: 1, t: 1 }, content: [{ type: "text", text: "Plan" }] },
        {
          type: "paragraph",
          attrs: { t: 2 },
          content: [
            { type: "text", text: "Ask " },
            { type: "text", text: "Sam", marks: [{ type: "bold" }] },
            { type: "text", text: " about " },
            { type: "text", text: "the deck", marks: [{ type: "italic" }] },
          ],
        },
        {
          type: "bulletList",
          content: [{ type: "listItem", attrs: { t: 3 }, content: [p("one"), { type: "bulletList", content: [{ type: "listItem", attrs: { t: 4 }, content: [p("nested")] }] }] }],
        },
      ],
    };
    expect(docToNotes(doc)).toEqual(original);
  });
});
