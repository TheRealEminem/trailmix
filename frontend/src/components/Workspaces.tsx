import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { OrganizeJob, OrganizeKind, Workspace, WorkspaceColor, WorkspaceFilter } from "../api";
import { PlusIcon } from "./icons";
import { Select, Spinner } from "./ui";

const DOT: Record<WorkspaceColor, string> = {
  forest: "bg-forest",
  sky: "bg-sky",
  sun: "bg-sun",
  trail: "bg-trail",
  ink: "bg-ink-soft",
};
export const WORKSPACE_COLORS = Object.keys(DOT) as WorkspaceColor[];

export function WorkspaceDot({ color, className = "" }: { color: WorkspaceColor | undefined; className?: string }) {
  return (
    <span
      aria-hidden="true"
      className={`inline-block h-2 w-2 shrink-0 rounded-full ${color ? DOT[color] : "bg-transparent ring-1 ring-inset ring-ink-faint"} ${className}`}
    />
  );
}

/** Does a meeting in `workspaceId` show under this filter? */
export function inWorkspace(filter: WorkspaceFilter, workspaceId: number | null | undefined): boolean {
  if (filter === "all") return true;
  if (filter === "none") return !workspaceId;
  return workspaceId === filter;
}

const MANAGE = "manage";

/** The sidebar's "which part of my life am I looking at" menu. */
export function WorkspaceSwitcher({
  workspaces,
  value,
  onChange,
  total,
  unsorted,
  onManage,
}: {
  workspaces: Workspace[];
  value: WorkspaceFilter;
  onChange: (v: WorkspaceFilter) => void;
  total: number;
  unsorted: number;
  onManage: () => void;
}) {
  const count = (n: number) => `${n} meeting${n === 1 ? "" : "s"}`;
  const options = [
    { value: "all", label: "All meetings", hint: count(total), icon: <WorkspaceDot color={undefined} /> },
    ...workspaces.map((w, i) => ({
      value: String(w.id),
      label: w.name,
      hint: count(w.meetings) + (i < 9 ? ` · ⌃${i + 1}` : ""),
      icon: <WorkspaceDot color={w.color} />,
    })),
    ...(workspaces.length ? [{ value: "none", label: "Default", hint: count(unsorted) + " · not in another workspace", icon: <WorkspaceDot color={undefined} /> }] : []),
    { value: MANAGE, label: workspaces.length ? "Manage workspaces…" : "Add workspaces…", hint: "e.g. a job, a committee, personal" },
  ];
  return (
    <Select
      label="Workspace"
      size="sm"
      value={String(value)}
      options={options}
      onChange={(v) => (v === MANAGE ? onManage() : onChange(v === "all" || v === "none" ? v : Number(v)))}
      className="h-9 w-full border-transparent bg-forest/[.045] font-medium shadow-none hover:border-line"
    />
  );
}

/** A meeting's workspace, changeable. */
export function WorkspacePicker({
  workspaces,
  value,
  auto,
  onChange,
}: {
  workspaces: Workspace[];
  value: number | null;
  auto: boolean;
  onChange: (id: number | null) => void;
}) {
  if (!workspaces.length) return null;
  return (
    <span data-tip={value && auto ? "Trailmix put it here from the notes; change it any time" : undefined}>
      <Select
        label="Workspace"
        size="sm"
        value={value ? String(value) : ""}
        onChange={(v) => onChange(v ? Number(v) : null)}
        options={[
          { value: "", label: "Default", icon: <WorkspaceDot color={undefined} /> },
          ...workspaces.map((w) => ({ value: String(w.id), label: w.name, icon: <WorkspaceDot color={w.color} /> })),
        ]}
        className="h-8 w-auto max-w-[220px] text-ui"
      />
    </span>
  );
}

/** Follows an organizing job (tag, sort, re-sort, suggest): start one, and see its progress until it ends. */
export function useOrganize(onDone: () => void) {
  const [job, setJob] = useState<OrganizeJob | null>(null);
  const [error, setError] = useState<string | null>(null);
  const done = useRef(onDone);
  done.current = onDone;

  useEffect(() => {
    // A job may already be running (started elsewhere, or before this page opened).
    api
      .organizeStatus()
      .then((j) => j.kind && setJob(j))
      .catch(() => undefined);
  }, []);
  useEffect(() => {
    if (!job?.active) return;
    const t = setInterval(() => {
      api
        .organizeStatus()
        .then((j) => {
          setJob(j);
          done.current();
        })
        .catch(() => undefined);
    }, 1500);
    return () => clearInterval(t);
  }, [job?.active]);

  const start = (kind: OrganizeKind, onlyOlder = false) => {
    setError(null);
    api
      .organize(kind, onlyOlder)
      .then(setJob)
      .catch((e: Error) => setError(e.message));
  };
  return { job, error, start, setJob };
}

const DOING: Record<OrganizeKind, string> = {
  sort: "Sorting",
  resort: "Re-sorting",
  tag: "Tagging",
  suggest: "Reading your meetings' topics",
};

/** "Sorting 12 of 80 with qwen2.5:7b…" / what the last job did. */
export function OrganizeProgress({ job }: { job: OrganizeJob | null }) {
  if (!job?.kind) return null;
  if (job.active)
    return (
      <span className="inline-flex items-center gap-2 text-hint text-ink-soft">
        <Spinner />
        {DOING[job.kind]} {job.done ?? 0} of {job.total ?? 0}
        {job.model ? ` with ${job.model}` : ""}…
      </span>
    );
  if (job.error) return <span className="text-hint text-trail-deep">Stopped: {job.error}</span>;
  const n = job.total ?? 0;
  const text =
    job.kind === "tag"
      ? `Tagged ${n} meeting${n === 1 ? "" : "s"}.`
      : job.kind === "suggest"
        ? job.suggestions?.length
          ? ""
          : "No suggestions came back. Try again, or add workspaces yourself."
        : `Sorted ${job.sorted ?? 0} of ${n}; the rest stay in Default.`;
  return text ? <span className="text-hint text-ink-soft">{text}</span> : null;
}

/** Workspace ideas from your meetings' tags, each one click to add. */
export function WorkspaceSuggestions({
  job,
  existing,
  onAdded,
}: {
  job: OrganizeJob | null;
  existing: Workspace[];
  onAdded: () => void;
}) {
  const [error, setError] = useState<string | null>(null);
  const ideas = (job?.active ? [] : (job?.suggestions ?? [])).filter(
    (s) => !existing.some((w) => w.name.toLowerCase() === s.name.toLowerCase()),
  );
  if (!ideas.length) return null;
  const add = async (list: { name: string; about: string }[]) => {
    setError(null);
    try {
      for (const s of list) await api.createWorkspace({ name: s.name, about: s.about });
      onAdded();
    } catch (e) {
      setError((e as Error).message);
    }
  };
  return (
    <div className="space-y-2 py-3.5" data-private="workspace suggestions">
      <div className="flex items-center justify-between gap-3">
        <span className="text-label font-medium">Suggested from your meetings</span>
        {ideas.length > 1 && (
          <button className="btn btn-sm btn-ghost" onClick={() => void add(ideas)}>
            Add all
          </button>
        )}
      </div>
      {ideas.map((s) => (
        <div key={s.name} className="flex items-center justify-between gap-3 rounded-md px-3 py-2 inset-surface">
          <div className="min-w-0">
            <div className="text-ui font-medium">{s.name}</div>
            <div className="text-hint text-ink-soft">{s.about}</div>
          </div>
          <button className="btn btn-sm btn-soft shrink-0" onClick={() => void add([s])}>
            <PlusIcon size={16} /> Add
          </button>
        </div>
      ))}
      <p className="text-hint text-ink-soft">You can rename them and change what they're about afterwards.</p>
      {error && <p className="text-hint text-trail-deep">{error}</p>}
    </div>
  );
}
