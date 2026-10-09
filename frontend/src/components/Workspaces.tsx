import type { Workspace, WorkspaceColor, WorkspaceFilter } from "../api";
import { Select } from "./ui";

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
    ...(workspaces.length && unsorted ? [{ value: "none", label: "Not in a workspace", hint: count(unsorted) }] : []),
    { value: MANAGE, label: workspaces.length ? "Manage workspaces…" : "Add workspaces…", hint: "Cold Connect, CEPAC, Personal…" },
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
          { value: "", label: "No workspace", icon: <WorkspaceDot color={undefined} /> },
          ...workspaces.map((w) => ({ value: String(w.id), label: w.name, icon: <WorkspaceDot color={w.color} /> })),
        ]}
        className="h-8 w-auto max-w-[220px] text-ui"
      />
    </span>
  );
}
