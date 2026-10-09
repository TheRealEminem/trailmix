import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { ProblemReport } from "../api";
import { screenText } from "../screenText";
import { CloseIcon, CopyIcon, ReportIcon } from "./icons";
import { Spinner, Switch } from "./ui";

const REPO = "TheRealEminem/trailmix";

function Toggle({ label, hint, checked, onChange }: { label: string; hint: string; checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <label className="flex items-start justify-between gap-4 py-2.5">
      <span className="min-w-0">
        <span className="block text-label font-medium">{label}</span>
        <span className="block text-hint leading-snug text-ink-soft">{hint}</span>
      </span>
      <Switch label={label} checked={checked} onChange={onChange} />
    </label>
  );
}

/**
 * "Report a problem", in the corner of every screen. It reads the window as text with your content already
 * hidden, the engine removes personal information from the rest (feedback.py), and you check the report
 * before it opens as a GitHub issue. Nothing is sent until you send it there.
 */
export default function ReportButton({ page, meetingId }: { page: string; meetingId: number | null }) {
  const [open, setOpen] = useState(false);
  const [screen, setScreen] = useState("");
  const [description, setDescription] = useState("");
  const [withScreen, setWithScreen] = useState(true);
  const [withDiagnostics, setWithDiagnostics] = useState(true);
  const [blocks, setBlocks] = useState(false);
  const [busy, setBusy] = useState(false);
  const [report, setReport] = useState<ProblemReport | null>(null);
  const [body, setBody] = useState("");
  const [title, setTitle] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const box = useRef<HTMLTextAreaElement>(null);

  const start = () => {
    setScreen(screenText()); // before the panel covers anything
    setReport(null);
    setError(null);
    setOpen(true);
    setTimeout(() => box.current?.focus(), 50);
  };
  const close = () => setOpen(false);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && close();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  const prepare = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await api.prepareReport({
        description,
        screen,
        page,
        include_screen: withScreen,
        include_diagnostics: withDiagnostics,
        blocks,
        meeting_id: meetingId,
      });
      setReport(r);
      setBody(r.body);
      setTitle(r.title);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const url = `https://github.com/${REPO}/issues/new?title=${encodeURIComponent(title)}&body=${encodeURIComponent(body.slice(0, 6000))}`;

  return (
    <>
      <button
        onClick={start}
        data-report-ui
        className="fixed bottom-4 right-4 z-40 flex h-10 w-10 items-center justify-center rounded-full border border-line bg-surface text-ink-soft shadow-md transition-[color,transform] duration-150 hover:-translate-y-px hover:text-ink"
        aria-label="Report a problem"
        data-tip="Report a problem"
      >
        <ReportIcon size={18} />
      </button>
      {open && (
        <div
          data-report-ui
          className="fixed inset-0 z-50 flex animate-fade-in items-center justify-center bg-[var(--scrim)] p-5 backdrop-blur-[3px]"
          onMouseDown={(e) => e.target === e.currentTarget && close()}
        >
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="report-title"
            className="max-h-[90vh] w-full max-w-[560px] animate-dialog-in overflow-y-auto rounded-xl border border-line bg-surface p-6 shadow-lg"
          >
            <div className="flex items-start justify-between gap-3">
              <div>
                <h2 id="report-title" className="text-lead font-semibold tracking-[-0.01em]">
                  {report ? "Check your report" : "Report a problem"}
                </h2>
                <p className="mt-1 text-hint leading-relaxed text-ink-soft">
                  {report
                    ? "This is everything that will be posted, publicly, on GitHub. Change anything you like."
                    : "Personal information is removed on your Mac before you see the report, and nothing is sent until you post it on GitHub yourself."}
                </p>
              </div>
              <button onClick={close} className="icon-btn h-8 w-8 shrink-0" aria-label="Close">
                <CloseIcon size={16} />
              </button>
            </div>

            {!report ? (
              <>
                <textarea
                  ref={box}
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  spellCheck
                  rows={4}
                  placeholder="What happened, and what did you expect? (e.g. I pressed Record during a Zoom call and nothing happened)"
                  className="field mt-4 w-full resize-y leading-relaxed"
                />
                <div className="mt-2 divide-y divide-line">
                  <Toggle
                    label="What's on screen"
                    hint="The window's text, with your meetings, notes, names and workspaces left out."
                    checked={withScreen}
                    onChange={setWithScreen}
                  />
                  <Toggle
                    label="Diagnostics"
                    hint="Trailmix version, your Mac and the models in use, this meeting's status, and recent errors."
                    checked={withDiagnostics}
                    onChange={setWithDiagnostics}
                  />
                  <Toggle
                    label="This stops me using Trailmix"
                    hint="Holds the beta back from everyone until it's fixed."
                    checked={blocks}
                    onChange={setBlocks}
                  />
                </div>
                {error && <p className="mt-3 text-hint text-trail-deep">{error}</p>}
                <div className="mt-5 flex justify-end gap-2">
                  <button className="btn btn-md btn-ghost" onClick={close}>
                    Cancel
                  </button>
                  <button className="btn btn-md btn-primary" onClick={() => void prepare()} disabled={busy || !description.trim()}>
                    {busy ? <Spinner /> : null}
                    {busy ? "Removing personal details…" : "Prepare report"}
                  </button>
                </div>
              </>
            ) : (
              <>
                <input
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                  aria-label="Report title"
                  spellCheck
                  className="field mt-4 w-full font-medium"
                />
                <textarea
                  value={body}
                  onChange={(e) => setBody(e.target.value)}
                  aria-label="Report text"
                  rows={14}
                  className="field mt-2 w-full resize-y font-mono text-hint leading-relaxed"
                />
                <p className="mt-2 text-hint text-ink-soft">
                  {report.model
                    ? `Checked by ${report.model} on your Mac, plus patterns and the names Trailmix knows.`
                    : "Checked with patterns and the names Trailmix knows (no local AI model was available), so read it carefully."}{" "}
                  Posting needs a free GitHub account.
                </p>
                <div className="mt-5 flex flex-wrap justify-end gap-2">
                  <button className="btn btn-md btn-ghost" onClick={() => setReport(null)}>
                    Back
                  </button>
                  <button
                    className="btn btn-md btn-soft"
                    onClick={() =>
                      void navigator.clipboard.writeText(`${title}\n\n${body}`).then(() => {
                        setCopied(true);
                        setTimeout(() => setCopied(false), 1500);
                      })
                    }
                  >
                    <CopyIcon size={16} /> {copied ? "Copied" : "Copy"}
                  </button>
                  <a className="btn btn-md btn-primary" href={url} target="_blank" rel="noreferrer" onClick={close}>
                    Post on GitHub
                  </a>
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </>
  );
}
