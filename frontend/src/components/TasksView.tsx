import { useEffect, useState } from "react";
import { api } from "../api";
import type { TaskWithMeeting, WorkspaceFilter } from "../api";
import { formatLongDate } from "../format";
import { CheckSquareIcon } from "./icons";
import { ContourBadge } from "./illustrations";
import TaskItem from "./TaskItem";
import { PageHeader, Skeleton, Switch } from "./ui";
import { inWorkspace } from "./Workspaces";

interface Props {
  onOpenMeeting: (id: number) => void;
  onChanged: () => void;
  onError: (msg: string) => void;
  /** Only tasks from meetings in this workspace. */
  workspace: WorkspaceFilter;
}

/** Every action item from every meeting (in the workspace you're in), with the open ones first. */
export default function TasksView({ onOpenMeeting, onChanged, onError, workspace }: Props) {
  const [tasks, setTasks] = useState<TaskWithMeeting[] | null>(null);
  const [showDone, setShowDone] = useState(false);

  const load = () =>
    api
      .tasks()
      .then(setTasks)
      .catch((e: Error) => onError(e.message));

  useEffect(() => {
    void load();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const toggle = async (t: TaskWithMeeting, done: boolean) => {
    setTasks((xs) => xs?.map((x) => (x.id === t.id ? { ...x, done } : x)) ?? null);
    try {
      await api.setTaskDone(t.id, done);
      onChanged();
    } catch (e) {
      onError((e as Error).message);
      void load();
    }
  };

  const here = (tasks ?? []).filter((t) => inWorkspace(workspace, t.workspace_id));
  const visible = here.filter((t) => showDone || !t.done);
  const groups: { id: number; title: string; created: string; items: TaskWithMeeting[] }[] = [];
  for (const t of visible) {
    const g = groups.find((x) => x.id === t.meeting_id);
    if (g) g.items.push(t);
    else groups.push({ id: t.meeting_id, title: t.meeting_title, created: t.meeting_created_at, items: [t] });
  }
  groups.sort((a, b) => b.created.localeCompare(a.created) || b.id - a.id); // newest meeting first
  const open = here.filter((t) => !t.done).length;

  return (
    <div>
      <PageHeader
        title="Tasks"
        subtitle={tasks === null ? "Gathering action items…" : `${open} open action item${open === 1 ? "" : "s"} from your meetings.`}
        actions={
          <label className="flex cursor-pointer items-center gap-2.5 text-ui font-medium text-ink-soft">
            Show done
            <Switch checked={showDone} onChange={setShowDone} label="Show done" />
          </label>
        }
      />

      {tasks === null && (
        <div className="panel space-y-5 p-6" aria-label="Loading tasks">
          {[0, 1].map((i) => (
            <div key={i} className="space-y-3">
              <Skeleton className="h-4 w-48" />
              <Skeleton className="h-3.5 w-3/4" />
              <Skeleton className="h-3.5 w-2/3" />
            </div>
          ))}
        </div>
      )}

      {tasks !== null && groups.length === 0 && (
        <div className="panel animate-enter px-6 py-14 text-center">
          <ContourBadge>
            <CheckSquareIcon size={18} />
          </ContourBadge>
          <p className="mt-3 font-medium">{tasks.length ? "All caught up" : "No action items yet"}</p>
          <p className="mt-1 text-label text-ink-soft">
            {tasks.length ? "Everything's ticked off. Enjoy the view." : "They're collected here from each meeting's summary."}
          </p>
        </div>
      )}

      {groups.length > 0 && (
        <div data-private="tasks" className="panel animate-enter divide-y divide-line" style={{ animationDelay: "60ms" }}>
          {groups.map((g) => (
            <section key={g.id} className="px-4 py-5 sm:px-6">
              <button onClick={() => onOpenMeeting(g.id)} className="group mb-2 rounded-sm px-2 text-left">
                <div className="text-heading font-semibold transition-colors group-hover:text-forest">{g.title}</div>
                <div className="text-hint text-ink-soft">{formatLongDate(g.created)}</div>
              </button>
              <div className="space-y-px">
                {g.items.map((t) => (
                  <TaskItem key={t.id} text={t.text} done={t.done} onToggle={(d) => void toggle(t, d)} />
                ))}
              </div>
            </section>
          ))}
        </div>
      )}
    </div>
  );
}
