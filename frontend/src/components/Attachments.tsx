import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { createPortal } from "react-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, inApp, revealInFinder } from "../api";
import type { Attachment, PendingAttachment } from "../api";
import { formatBytes } from "../format";
import {
  AlertIcon,
  FileIcon,
  FolderIcon,
  ImageIcon,
  LinkIcon,
  PaperclipIcon,
  PlusIcon,
  RefreshIcon,
  ShrinkIcon,
  SlidesIcon,
  TrashIcon,
} from "./icons";
import { Collapse, Spinner, useConfirm } from "./ui";

const KIND_ICON: Record<Attachment["kind"], ReactNode> = {
  image: <ImageIcon size={18} />,
  pdf: <FileIcon size={18} />,
  slides: <SlidesIcon size={18} />,
  document: <FileIcon size={18} />,
  other: <PaperclipIcon size={18} />,
  link: <LinkIcon size={18} />,
};
const KIND_LABEL: Record<Attachment["kind"], string> = {
  image: "Photo",
  pdf: "PDF",
  slides: "PowerPoint",
  document: "Document",
  other: "File",
  link: "Link",
};
const SEARCHABLE_TIP =
  "Its text and a short digest go in their own part under the summary, and into search and Ask. Never into tasks.";

/** Slides, PDFs, photos and links kept with a meeting's notes, in one list. */
export default function Attachments({
  meetingId,
  items,
  onChanged,
}: {
  meetingId: number;
  items: Attachment[];
  onChanged: () => void;
}) {
  const [over, setOver] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<PendingAttachment[]>([]);
  const [linking, setLinking] = useState(false);
  const [url, setUrl] = useState("");
  const picker = useRef<HTMLInputElement>(null);
  const [confirm, confirmDialog] = useConfirm();

  // While one is being read, keep the meeting fresh so its digest shows up when it's done.
  const reading = items.some((a) => a.status === "reading");
  useEffect(() => {
    if (!reading) return;
    const t = window.setInterval(onChanged, 2500);
    return () => window.clearInterval(t);
  }, [reading, onChanged]);

  // A file dropped anywhere else on the page must not replace the page with the file.
  useEffect(() => {
    const stop = (e: DragEvent) => e.preventDefault();
    window.addEventListener("dragover", stop);
    window.addEventListener("drop", stop);
    return () => {
      window.removeEventListener("dragover", stop);
      window.removeEventListener("drop", stop);
    };
  }, []);

  const run = async (label: string, fn: () => Promise<unknown>) => {
    setBusy(label);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
      onChanged();
    }
  };

  const upload = (files: File[]) =>
    run(files.length === 1 ? `Adding ${files[0].name}…` : `Adding ${files.length} files…`, async () => {
      const waiting: PendingAttachment[] = [];
      for (const [i, f] of files.entries()) {
        setBusy(files.length === 1 ? `Adding ${f.name}…` : `Adding ${f.name} (${i + 1} of ${files.length})…`);
        const r = await api.addAttachment(meetingId, f, false);
        if (r.pending) waiting.push(r.pending);
      }
      setPending((p) => [...p, ...waiting]);
      if (picker.current) picker.current.value = "";
    });

  const addLink = () =>
    run("Adding the link…", async () => {
      await api.addAttachmentLink(meetingId, url.trim(), false);
      setUrl("");
      setLinking(false);
    });

  const decide = (p: PendingAttachment, choice: "shrink" | "keep" | "cancel") => {
    setPending((list) => list.filter((x) => x.id !== p.id));
    void run(choice === "shrink" ? `Shrinking ${p.name}…` : `Adding ${p.name}…`, () =>
      choice === "cancel" ? api.cancelAttachment(meetingId, p.id) : api.finishAttachment(meetingId, p, choice === "shrink"),
    );
  };

  const remove = async (a: Attachment) => {
    const ok = await confirm({
      title: `Remove ${a.name}?`,
      body:
        a.kind === "link"
          ? "The link and anything read from it are removed from this meeting."
          : "Trailmix's copy of the file is deleted, along with anything read from it. Your own copy isn't touched.",
      confirmLabel: "Remove",
      tone: "danger",
    });
    if (ok) void run(`Removing ${a.name}…`, () => api.deleteAttachment(a.id));
  };

  const reveal = (a: Attachment) =>
    void api.attachmentPath(a.id).then(
      (r) => revealInFinder(r.path),
      (e: Error) => setError(e.message),
    );

  return (
    <section
      aria-labelledby="attachments-heading"
      className={`rounded-lg transition-[box-shadow] ${over ? "ring-2 ring-forest/40" : ""}`}
      onDragOver={(e) => {
        e.preventDefault();
        setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => {
        e.preventDefault();
        setOver(false);
        const files = Array.from(e.dataTransfer.files);
        if (files.length) void upload(files);
        else {
          const dropped = e.dataTransfer.getData("text/uri-list") || e.dataTransfer.getData("text/plain");
          if (/^https?:\/\//.test(dropped.trim())) {
            setUrl(dropped.trim());
            setLinking(true);
          }
        }
      }}
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h3 id="attachments-heading" className="flex items-center gap-2 text-label font-semibold">
            <PaperclipIcon size={16} />
            Attachments{items.length ? ` · ${items.length}` : ""}
          </h3>
          <p className="mt-0.5 text-hint leading-snug text-ink-soft">
            Slides, PDFs, photos or a link, kept with this meeting. Drop them here.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button className="btn btn-sm btn-soft" onClick={() => picker.current?.click()} disabled={!!busy}>
            <PlusIcon size={16} />
            Add files…
          </button>
          <button className="btn btn-sm btn-ghost" onClick={() => setLinking((v) => !v)} aria-expanded={linking}>
            <LinkIcon size={16} />
            Add link
          </button>
          <input
            ref={picker}
            type="file"
            multiple
            hidden
            onChange={(e) => void upload(Array.from(e.target.files ?? []))}
          />
        </div>
      </div>

      <Collapse open={linking}>
        <form
          className="mt-3 flex flex-wrap gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (url.trim()) void addLink();
          }}
        >
          <input
            type="url"
            className="field min-w-0 flex-1"
            placeholder="https://docs.google.com/presentation/d/…"
            aria-label="Link to attach"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
          />
          <button className="btn btn-md btn-primary" disabled={!url.trim() || !!busy}>
            Add
          </button>
        </form>
      </Collapse>

      {busy && (
        <p className="mt-3 flex items-center gap-2 truncate text-label text-ink-soft">
          <Spinner /> {busy}
        </p>
      )}
      {error && (
        <p className="mt-3 flex items-start gap-2 text-label text-trail-deep">
          <AlertIcon size={16} className="mt-0.5 shrink-0" />
          {error}
        </p>
      )}

      {items.length > 0 && (
        <ul className="mt-3 divide-y divide-line">
          {items.map((a) => (
            <AttachmentRow
              key={a.id}
              a={a}
              onSearchable={(on) =>
                void run(on ? `Adding ${a.name} to the searchable notes…` : "", () =>
                  api.updateAttachment(a.id, { searchable: on }),
                )
              }
              onShrink={() => void run(`Shrinking ${a.name}…`, () => api.shrinkAttachment(a.id))}
              onReread={() => void run(`Reading ${a.name} again…`, () => api.rereadAttachment(a.id))}
              onReveal={inApp && a.kind !== "link" ? () => reveal(a) : undefined}
              onRemove={() => void remove(a)}
            />
          ))}
        </ul>
      )}

      {pending[0] && <ShrinkDialog p={pending[0]} onDecide={(c) => decide(pending[0], c)} />}
      {confirmDialog}
    </section>
  );
}

/** The same list for a meeting that's being recorded (whose attachments aren't sent with the live recording):
 * a photo of the whiteboard, the deck being presented. */
export function LiveAttachments({ meetingId }: { meetingId: number }) {
  const [items, setItems] = useState<Attachment[]>([]);
  const load = useCallback(
    () =>
      void api.getMeeting(meetingId).then(
        (m) => setItems(m.attachments),
        () => undefined,
      ),
    [meetingId],
  );
  useEffect(load, [load]);
  return <Attachments meetingId={meetingId} items={items} onChanged={load} />;
}

function AttachmentRow({
  a,
  onSearchable,
  onShrink,
  onReread,
  onReveal,
  onRemove,
}: {
  a: Attachment;
  onSearchable: (on: boolean) => void;
  onShrink: () => void;
  onReread: () => void;
  onReveal?: () => void;
  onRemove: () => void;
}) {
  const href = a.kind === "link" ? a.url! : api.attachmentUrl(a.id);
  const detail = [
    KIND_LABEL[a.kind],
    a.kind !== "link" && formatBytes(a.size),
    a.original_size && `shrunk from ${formatBytes(a.original_size)}`,
  ].filter(Boolean);
  return (
    <li className="flex flex-wrap items-start gap-x-3.5 gap-y-2 py-3">
      <a href={href} target="_blank" rel="noreferrer" className="shrink-0" aria-hidden="true" tabIndex={-1}>
        {a.has_preview ? (
          <img
            src={api.attachmentPreviewUrl(a.id)}
            alt=""
            className="h-11 w-11 rounded-md border border-line object-cover"
            loading="lazy"
          />
        ) : (
          <span className="well h-11 w-11">{KIND_ICON[a.kind]}</span>
        )}
      </a>
      <div className="min-w-0 flex-1">
        <a href={href} target="_blank" rel="noreferrer" className="block truncate text-label font-medium hover:underline">
          {a.name}
        </a>
        <div className="mt-0.5 text-hint text-ink-soft">{detail.join(" · ")}</div>
        <label className="mt-1.5 inline-flex items-center gap-2 text-hint text-ink-soft" data-tip={SEARCHABLE_TIP}>
          <input
            type="checkbox"
            className="h-4 w-4 accent-[var(--forest)]"
            checked={a.searchable}
            onChange={(e) => onSearchable(e.target.checked)}
          />
          Add to searchable notes
          {a.status === "reading" && (
            <span className="inline-flex items-center gap-1.5">
              <Spinner /> Reading…
            </span>
          )}
        </label>
        {a.searchable && a.error && (
          <p className={`mt-1 flex items-start gap-1.5 text-hint ${a.status === "error" ? "text-trail-deep" : "text-ink-soft"}`}>
            {a.status === "error" && <AlertIcon size={14} className="mt-0.5 shrink-0" />}
            <span className="min-w-0 break-words">{a.error}</span>
            {a.status === "error" && (
              <button className="shrink-0 font-medium text-ink underline-offset-2 hover:underline" onClick={onReread}>
                <RefreshIcon size={12} className="mr-1 inline" />
                Try again
              </button>
            )}
          </p>
        )}
      </div>
      <div className="flex shrink-0 items-center gap-1">
        {a.can_shrink && a.size > 1024 * 1024 && (
          <button className="btn btn-sm btn-ghost" onClick={onShrink} data-tip="Make it smaller (lower picture quality)">
            <ShrinkIcon size={16} />
            Shrink
          </button>
        )}
        {onReveal && (
          <button className="icon-btn" onClick={onReveal} aria-label="Show in Finder" data-tip="Show in Finder">
            <FolderIcon size={16} />
          </button>
        )}
        <button className="icon-btn hover:!bg-trail-soft hover:!text-trail-deep" onClick={onRemove} aria-label={`Remove ${a.name}`} data-tip="Remove">
          <TrashIcon size={16} />
        </button>
      </div>
    </li>
  );
}

const SHRINK_HOW: Record<string, string> = {
  image:
    "The photo is resized to 2400 pixels on its longest side and saved as a JPEG: plenty to read a whiteboard or a slide.",
  pdf: "Each page becomes a picture. Its text is read first, so it can still go in the searchable notes, but you won't be able to select text in the PDF itself.",
  slides: "The pictures in the slides are resized and recompressed. Text, layout and notes stay as they are.",
};

/** "Deck.pptx is 48 MB. Shrink it to about 6 MB?" */
function ShrinkDialog({ p, onDecide }: { p: PendingAttachment; onDecide: (c: "shrink" | "keep" | "cancel") => void }) {
  const shrinkRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    shrinkRef.current?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onDecide("cancel");
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onDecide]);
  const saving = Math.max(0, Math.round((1 - p.estimate / p.size) * 100));
  return createPortal(
    <div className="fixed inset-0 z-50 flex animate-fade-in items-center justify-center bg-[var(--scrim)] p-5 backdrop-blur-[3px]">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="shrink-title"
        className="w-full max-w-[440px] animate-dialog-in rounded-xl border border-line bg-surface p-6 shadow-lg"
      >
        <div className="well well-forest mb-4 h-10 w-10 rounded-md">
          <ShrinkIcon size={20} />
        </div>
        <h2 id="shrink-title" className="break-words text-lead font-semibold tracking-[-0.01em]">
          {p.name} is {formatBytes(p.size)}
        </h2>
        <p className="mt-1.5 text-label leading-relaxed text-ink-soft">
          {p.kind === "pdf" ? "Flatten" : "Shrink"} it to about <b className="font-medium text-ink">{formatBytes(p.estimate)}</b>
          {saving > 0 && ` (${saving}% smaller)`}? {SHRINK_HOW[p.kind] ?? ""}
        </p>
        <p className="mt-2 text-hint text-ink-soft">To always shrink big files, see Settings → Attachments.</p>
        <div className="mt-6 flex flex-wrap justify-end gap-2">
          <button className="btn btn-md btn-ghost" onClick={() => onDecide("cancel")}>
            Cancel
          </button>
          <button className="btn btn-md btn-soft" onClick={() => onDecide("keep")}>
            Keep original
          </button>
          <button ref={shrinkRef} className="btn btn-md btn-primary" onClick={() => onDecide("shrink")}>
            {p.kind === "pdf" ? "Flatten" : "Shrink"}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}

/** The separate part under the summary: what the attachments in the searchable notes cover. Never tasks. */
export function AttachedMaterials({ items }: { items: Attachment[] }) {
  const shown = items.filter((a) => a.searchable);
  if (!shown.length) return null;
  return (
    <section className="prose-summary mt-8" aria-labelledby="materials-heading">
      <h2 id="materials-heading">
        <span className="well well-forest h-7 w-7">
          <PaperclipIcon size={16} />
        </span>
        Attached materials
      </h2>
      <p className="!mt-0 text-hint text-ink-soft">
        From the files and links added to the searchable notes. For reference only: they're not part of the tasks.
      </p>
      {shown.map((a) => (
        <div key={a.id}>
          <h3 className="flex items-center gap-2">
            {KIND_ICON[a.kind]}
            {a.name}
          </h3>
          {a.status === "reading" ? (
            <p className="flex items-center gap-2 text-ink-soft">
              <Spinner /> Reading…
            </p>
          ) : a.digest ? (
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{a.digest}</ReactMarkdown>
          ) : (
            <p className="text-ink-soft">{a.error ?? "No digest yet."}</p>
          )}
        </div>
      ))}
    </section>
  );
}
