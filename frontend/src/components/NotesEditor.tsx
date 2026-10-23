import { useEffect, useRef, useState } from "react";
import { EditorContent, useEditor } from "@tiptap/react";
import { Extension } from "@tiptap/core";
import type { Editor } from "@tiptap/core";
import type { Transaction } from "@tiptap/pm/state";
import StarterKit from "@tiptap/starter-kit";
import { Placeholder } from "@tiptap/extensions";
import { api } from "../api";
import type { MyNote } from "../api";
import { docToNotes, notesToHtml } from "../notesMarkdown";

const SAVE_MS = 700;

/** Each paragraph, heading and list item remembers the moment in the recording it was started (`t`, seconds). A new
 * block made by pressing Enter starts without one, so it gets its own moment. */
const Stamped = Extension.create({
  name: "stamped",
  addGlobalAttributes() {
    return [
      {
        types: ["paragraph", "heading", "listItem"],
        attributes: {
          t: {
            default: null,
            keepOnSplit: false,
            parseHTML: (el) => {
              const v = el.getAttribute("data-t");
              return v == null || v === "" || Number.isNaN(Number(v)) ? null : Number(v);
            },
            renderHTML: (attrs) => (attrs.t == null ? {} : { "data-t": attrs.t }),
          },
        },
      },
    ];
  },
});

/** Stamps the blocks this change touched with the moment, while recording, if they have none yet (a note typed after
 * an earlier meeting keeps having none). A paragraph inside a list item is stamped through its item. */
function stampNew(editor: Editor, change: Transaction, now: number) {
  const tr = editor.state.tr;
  const { doc } = editor.state;
  change.mapping.maps.forEach((map, i) => {
    const after = change.mapping.slice(i + 1);
    map.forEach((_oldFrom, _oldTo, from, to) => {
      const [a, b] = [after.map(from, -1), after.map(to, 1)];
      doc.nodesBetween(Math.max(0, a), Math.min(doc.content.size, b), (node, pos, parent) => {
        const block =
          node.type.name === "listItem" ||
          ((node.type.name === "paragraph" || node.type.name === "heading") && parent?.type.name !== "listItem");
        if (block && node.attrs.t == null && node.textContent.trim()) tr.setNodeAttribute(pos, "t", Math.round(now * 10) / 10);
      });
    });
  });
  if (tr.docChanged) editor.view.dispatch(tr.setMeta("addToHistory", false));
}

/**
 * Your notes as a page: type freely, "# " makes a heading, "- " or "1. " a list, ⌘B bold, ⌘I italic, and pasted links
 * stay links. Saved as you type. While recording each block is stamped with the moment you started it, and after
 * the meeting the notes AI builds the meeting's notes around them.
 *
 * The page is loaded once: what arrives later for the same meeting (the live poll) is ignored, so typing is never
 * overwritten. Give it a `key` per meeting.
 */
export default function NotesEditor({
  meetingId,
  notes,
  now,
  autoFocus,
  placeholder = "Add notes or links…",
  className = "",
  onSaved,
}: {
  meetingId: number;
  notes: MyNote[];
  /** Recording now: the seconds into it, for stamping new blocks. */
  now?: () => number;
  autoFocus?: boolean;
  placeholder?: string;
  className?: string;
  onSaved?: (notes: MyNote[]) => void;
}) {
  const [state, setState] = useState<"saved" | "saving" | "error">("saved");
  const [error, setError] = useState<string | null>(null);
  const saved = useRef(JSON.stringify(notes));
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const nowRef = useRef(now);
  nowRef.current = now;
  const onSavedRef = useRef(onSaved);
  onSavedRef.current = onSaved;

  const save = (editor: Editor) => {
    timer.current = null;
    const next = docToNotes(editor.getJSON());
    const body = JSON.stringify(next);
    if (body === saved.current) {
      setState("saved");
      return;
    }
    saved.current = body;
    setState("saving");
    api
      .setMyNotes(meetingId, next)
      .then((stored) => {
        setError(null);
        setState((s) => (s === "saving" ? "saved" : s));
        onSavedRef.current?.(stored);
      })
      .catch((e) => {
        saved.current = "";  // try again with the next keystroke
        setError((e as Error).message);
        setState("error");
      });
  };

  const editor = useEditor({
    extensions: [
      StarterKit.configure({
        heading: { levels: [1, 2, 3] },
        blockquote: false,
        codeBlock: false,
        horizontalRule: false,
        underline: false,
        link: { openOnClick: true, autolink: true, linkOnPaste: true, HTMLAttributes: { target: "_blank", rel: "noreferrer" } },
      }),
      Placeholder.configure({ placeholder }),
      Stamped,
    ],
    content: notesToHtml(notes),
    autofocus: autoFocus ? "end" : false,
    editorProps: {
      attributes: { class: "notes-editor prose-summary outline-none", spellcheck: "true", "aria-label": "Your notes" },
    },
    onUpdate: ({ editor, transaction }) => {
      if (!transaction.docChanged) return;
      const at = nowRef.current?.();
      if (at != null) stampNew(editor, transaction, at);
      if (timer.current) clearTimeout(timer.current);
      setState("saving");
      timer.current = setTimeout(() => save(editor), SAVE_MS);
    },
  });

  // Leaving the page (or the recording ending) saves what's still waiting.
  useEffect(
    () => () => {
      if (timer.current && editor && !editor.isDestroyed) {
        clearTimeout(timer.current);
        save(editor);
      }
    },
    [editor], // eslint-disable-line react-hooks/exhaustive-deps
  );

  return (
    <div className={className} data-private="your notes">
      <EditorContent editor={editor} />
      <p className="mt-2 h-4 text-meta text-ink-soft" aria-live="polite">
        {state === "error" ? <span className="text-trail-deep">Not saved: {error}</span> : state === "saving" ? "Saving…" : ""}
      </p>
    </div>
  );
}
