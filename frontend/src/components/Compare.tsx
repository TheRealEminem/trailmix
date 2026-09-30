import { useMemo, useState } from "react";
import type { DraftLine, Segment } from "../api";
import { formatDuration, shortModel } from "../format";
import { SpeakerChip, Switch } from "./ui";

interface Props {
  draft: DraftLine[];
  segments: Segment[];
  names?: Record<string, string>;
  liveModel: string;
  finalModel: string;
}

const words = (t: string) => t.toLowerCase().match(/[a-z0-9']+/g) ?? [];

/** Renders text with words that don't appear in `other` highlighted (order-insensitive, per row). */
function Highlighted({ text, other, tone }: { text: string; other: string; tone: "add" | "drop" }) {
  const known = new Set(words(other));
  const cls =
    tone === "add" ? "bg-forest-soft text-forest-deep" : "bg-trail-soft text-trail-deep line-through decoration-trail/50";
  return (
    <>
      {text.split(/(\s+)/).map((tok, i) => {
        const w = words(tok)[0];
        return w && !known.has(w) ? (
          <mark key={i} className={`rounded-xs px-0.5 ${cls}`}>
            {tok}
          </mark>
        ) : (
          <span key={i}>{tok}</span>
        );
      })}
    </>
  );
}

export default function Compare({ draft, segments, names = {}, liveModel, finalModel }: Props) {
  const [highlight, setHighlight] = useState(true);

  const rows = useMemo(() => {
    const drafts = [...draft].sort((a, b) => a.start - b.start);
    const rows = drafts.map((d) => ({ draft: d, finals: [] as Segment[] }));
    // Each final segment goes to the latest draft chunk (same speaker when labeled) that began at or
    // before its midpoint, give or take a second: chunk and segment edges never line up exactly.
    for (const f of [...segments].sort((a, b) => a.start - b.start)) {
      const mid = (f.start + f.end) / 2;
      const same = rows.filter((r) => !f.speaker || !r.draft.speaker || r.draft.speaker === f.speaker);
      const pool = same.length > 0 ? same : rows;
      const before = pool.filter((r) => r.draft.start <= mid + 1);
      (before.length > 0 ? before[before.length - 1] : pool[0]).finals.push(f);
    }
    return rows;
  }, [draft, segments]);

  const count = (texts: string[]) => texts.reduce((n, t) => n + words(t).length, 0);
  const draftWords = count(draft.map((d) => d.text));
  const finalWords = count(segments.map((s) => s.text));

  return (
    <div>
      <div className="mb-6 flex flex-wrap items-center justify-between gap-3 text-hint text-ink-soft">
        <span className="tabular-nums">
          <span className="font-semibold text-ink">{draftWords}</span> words in the draft ·{" "}
          <span className="font-semibold text-ink">{finalWords}</span> in the final transcript
        </span>
        <label className="flex cursor-pointer items-center gap-2.5 font-medium">
          Highlight changes
          <Switch checked={highlight} onChange={setHighlight} label="Highlight changes" />
        </label>
      </div>

      <div className="-mx-1 overflow-x-auto px-1">
        <div className="grid min-w-[560px] grid-cols-[3rem_1fr_1fr] gap-x-5 text-label">
          <div />
          <div className="pb-3">
            <div className="text-ui font-semibold text-ink">Live draft</div>
            <div className="text-meta text-ink-soft">{shortModel(liveModel)} · during the meeting</div>
          </div>
          <div className="pb-3">
            <div className="text-ui font-semibold text-forest-deep">Final</div>
            <div className="text-meta text-ink-soft">{shortModel(finalModel)} · after you stopped</div>
          </div>

          {rows.map((r, i) => {
            const finalText = r.finals.map((f) => f.text).join(" ");
            return (
              <div key={i} className="contents">
                <div className="border-t border-line py-3 font-mono text-meta tabular-nums text-ink-soft">
                  {formatDuration(r.draft.start)}
                </div>
                <div className="border-t border-line py-3 pr-2 leading-relaxed text-ink-soft">
                  {r.draft.speaker && (
                    <span className="mr-1.5">
                      <SpeakerChip speaker={r.draft.speaker} name={names[r.draft.speaker]} />
                    </span>
                  )}
                  {highlight ? <Highlighted text={r.draft.text} other={finalText} tone="drop" /> : r.draft.text}
                </div>
                <div className="space-y-1 border-t border-line py-3 leading-relaxed">
                  {r.finals.length === 0 && <span className="text-ink-soft">—</span>}
                  {r.finals.map((f, j) => (
                    <p key={j}>
                      {f.speaker && j === 0 && (
                        <span className="mr-1.5">
                          <SpeakerChip speaker={f.speaker} name={names[f.speaker]} />
                        </span>
                      )}
                      {highlight ? <Highlighted text={f.text} other={r.draft.text} tone="add" /> : f.text}
                    </p>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
