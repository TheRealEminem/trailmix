import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { api } from "../api";
import type { MyNote } from "../api";
import { formatDuration } from "../format";
import { CloseIcon, PencilIcon } from "./icons";

const URL = /(https?:\/\/[^\s<>()[\]]+[^\s<>()[\].,;:!?'"])/g;

/** Text with its links clickable. */
function Linked({ text }: { text: string }) {
  const parts: ReactNode[] = text.split(URL).map((part, i) =>
    i % 2 ? (
      <a key={i} href={part} target="_blank" rel="noreferrer" className="break-all text-forest underline decoration-forest/30 underline-offset-2">
        {part}
      </a>
    ) : (
      part
    ),
  );
  return <span className="whitespace-pre-wrap">{parts}</span>;
}

/**
 * Your own notes: type a line and press Enter. While recording each one is stamped with the moment; the notes
 * AI works them into the meeting's notes and keeps every link. Afterwards they can be edited or removed here,
 * and Regenerate rewrites the notes with them.
 */
export default function MyNotes({
  meetingId,
  notes,
  live,
  onChanged,
}: {
  meetingId: number;
  notes: MyNote[];
  /** Recording now: new notes get the current moment, and the box is ready to type in. */
  live?: boolean;
  onChanged?: () => void;
}) {
  const [list, setList] = useState(notes);
  const [draft, setDraft] = useState("");
  const [editing, setEditing] = useState<number | null>(null);
  const [editText, setEditText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => setList(notes), [JSON.stringify(notes)]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (live) box.current?.scrollTo({ top: box.current.scrollHeight, behavior: "smooth" });
  }, [list.length, live]);

  const run = (p: Promise<MyNote[]>) =>
    p
      .then((next) => {
        setList(next);
        setError(null);
        onChanged?.();
      })
      .catch((e) => setError((e as Error).message));
  const add = () => {
    const text = draft.trim();
    if (!text) return;
    setDraft("");
    void run(api.addMyNote(meetingId, text));
  };
  const save = (next: MyNote[]) => void run(api.setMyNotes(meetingId, next));

  return (
    <div data-private="your notes">
      {list.length > 0 && (
        <div ref={box} className={`space-y-2 ${live ? "fade-edges-y -mx-1 max-h-64 overflow-y-auto px-1 py-1" : ""}`}>
          {list.map((n, i) => (
            <div key={i} className="group flex animate-enter items-start gap-2.5 text-body leading-relaxed">
              <span className="mt-[3px] w-12 shrink-0 text-meta tabular-nums text-ink-soft">
                {n.t != null ? formatDuration(n.t) : "later"}
              </span>
              {editing === i ? (
                <div className="min-w-0 flex-1">
                  <textarea
                    className="field w-full resize-y text-label leading-relaxed"
                    rows={Math.min(6, editText.split("\n").length + 1)}
                    value={editText}
                    autoFocus
                    spellCheck
                    onChange={(e) => setEditText(e.target.value)}
                    aria-label="Edit note"
                  />
                  <div className="mt-1.5 flex gap-2">
                    <button
                      className="btn btn-sm btn-soft"
                      onClick={() => {
                        save(list.map((x, j) => (j === i ? { ...x, text: editText } : x)));
                        setEditing(null);
                      }}
                    >
                      Save
                    </button>
                    <button className="btn btn-sm btn-ghost" onClick={() => setEditing(null)}>
                      Cancel
                    </button>
                  </div>
                </div>
              ) : (
                <>
                  <div className="min-w-0 flex-1">
                    <Linked text={n.text} />
                  </div>
                  <span className="flex shrink-0 opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100">
                    <button
                      className="icon-btn h-7 w-7"
                      aria-label="Edit note"
                      onClick={() => {
                        setEditing(i);
                        setEditText(n.text);
                      }}
                    >
                      <PencilIcon size={14} />
                    </button>
                    <button
                      className="icon-btn h-7 w-7 hover:!text-trail-deep"
                      aria-label="Delete note"
                      onClick={() => save(list.filter((_, j) => j !== i))}
                    >
                      <CloseIcon size={14} />
                    </button>
                  </span>
                </>
              )}
            </div>
          ))}
        </div>
      )}
      <textarea
        className={`field w-full resize-none text-label leading-relaxed ${list.length ? "mt-3" : ""}`}
        rows={draft.includes("\n") ? 3 : 1}
        value={draft}
        spellCheck
        placeholder={
          live
            ? "Jot a note, paste a link… Enter adds it at this moment (Shift+Enter for a new line)"
            : "Add a note or a link… Enter adds it (Shift+Enter for a new line)"
        }
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            add();
          }
        }}
        aria-label="Add a note"
      />
      {error && <p className="mt-2 text-hint text-trail-deep">{error}</p>}
    </div>
  );
}
