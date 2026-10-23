import { useEffect, useRef, useState } from "react";
import type { Workspace } from "../api";
import { CloseIcon, FolderIcon } from "./icons";
import { OrganizeProgress, WorkspaceSuggestions, useOrganize } from "./Workspaces";

const SKIPPED_KEY = "trailmix.workspacesSkipped";

function skippedBefore(): boolean {
  try {
    return localStorage.getItem(SKIPPED_KEY) === "1";
  } catch {
    return false;
  }
}

/**
 * After setup, once there are a few meetings: an offer to organize them into workspaces (suggested from their
 * topics, or your own). Gone once workspaces exist and the meetings are sorted, or when you close it.
 */
export default function WorkspacesTip({
  meetingCount,
  workspaces,
  onOpenSettings,
  onChanged,
}: {
  meetingCount: number;
  workspaces: Workspace[];
  onOpenSettings: (section?: string) => void;
  onChanged: () => void;
}) {
  const [skipped, setSkipped] = useState(skippedBefore);
  const organize = useOrganize(onChanged);
  // Open while suggestions are waiting to be added, or meetings are being sorted into new workspaces.
  const [engaged, setEngaged] = useState(false);
  const sortedAfterSuggest = useRef(false);
  const ideas = organize.job?.kind === "suggest" && !organize.job.active ? (organize.job.suggestions ?? []) : [];
  const left = ideas.filter((i) => !workspaces.some((w) => w.name.toLowerCase() === i.name.toLowerCase()));
  // Once every suggested workspace is added, sort the meetings into them.
  useEffect(() => {
    if (engaged && ideas.length && !left.length && !sortedAfterSuggest.current) {
      sortedAfterSuggest.current = true;
      organize.start("sort");
    }
  }, [workspaces, organize.job]); // eslint-disable-line react-hooks/exhaustive-deps

  const busy = !!organize.job?.active;
  if (skipped || meetingCount < 3) return null;
  if (workspaces.length && !engaged) return null;
  if (engaged && workspaces.length && !left.length && !busy && sortedAfterSuggest.current) return null;

  const skip = () => {
    setSkipped(true);
    try {
      localStorage.setItem(SKIPPED_KEY, "1");
    } catch {
      /* private window */
    }
  };

  return (
    <section aria-label="Workspaces" className="glass mb-6 animate-enter px-5 py-4 sm:px-6">
      <div className="flex items-start gap-3.5">
        <span className="well well-forest mt-0.5">
          <FolderIcon size={16} />
        </span>
        <div className="min-w-0 flex-1">
          <h2 className="font-display text-lead font-semibold tracking-[-0.01em]">Sort your meetings into workspaces?</h2>
          <p className="mt-1 text-hint leading-relaxed text-ink-soft">
            Keep your job, a committee and personal apart. Trailmix can suggest some from what your meetings are about.
          </p>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <button
              className="btn btn-sm btn-primary"
              disabled={busy}
              onClick={() => {
                setEngaged(true);
                organize.start("suggest");
              }}
            >
              Suggest from my meetings
            </button>
            <button className="btn btn-sm btn-soft" onClick={() => onOpenSettings("workspaces")}>
              Add my own
            </button>
          </div>
          <div className="mt-2 text-hint">
            <OrganizeProgress job={organize.job} />
            {organize.error && <span className="text-trail-deep">{organize.error}</span>}
          </div>
          <WorkspaceSuggestions job={organize.job} existing={workspaces} onAdded={onChanged} />
        </div>
        <button onClick={skip} className="icon-btn h-8 w-8" aria-label="Not now" data-tip="Not now">
          <CloseIcon size={16} />
        </button>
      </div>
    </section>
  );
}
