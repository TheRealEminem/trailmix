import { useEffect, useState } from "react";
import { api } from "../api";
import type { Task } from "../api";
import { ArrowUpIcon, CheckIcon, SendIcon, TrashIcon } from "./icons";
import { Spinner } from "./ui";

/** "Dana: fix the login bug" -> owner "Dana", rest "fix the login bug". */
function splitOwner(text: string): [string | null, string] {
  const m = text.match(/^([^:]{1,32}):\s*(.+)$/);
  return m && m[1].split(/\s+/).length <= 4 ? [m[1], m[2]] : [null, text];
}

/** The task tracker picked in Settings → Tasks (its name), or null when tasks go nowhere. */
export function useTracker(): string | null {
  const [name, setName] = useState<string | null>(null);
  useEffect(() => {
    api
      .trackers()
      .then((t) => setName(t.trackers.find((x) => x.id === t.active)?.name ?? null))
      .catch(() => setName(null));
  }, []);
  return name;
}

export default function TaskItem({
  task,
  onToggle,
  onDelete,
  onSend,
  tracker,
}: {
  task: Task;
  onToggle: (done: boolean) => void;
  onDelete?: () => void;
  /** Makes it a ticket in `tracker`. */
  onSend?: () => Promise<void>;
  tracker?: string | null;
}) {
  const { text, done, ticket } = task;
  const [owner, rest] = splitOwner(text);
  const [sending, setSending] = useState(false);
  const send = async () => {
    setSending(true);
    try {
      await onSend?.();
    } finally {
      setSending(false);
    }
  };
  return (
    <div className="group flex items-start gap-3 rounded-md px-2 py-2 transition-colors duration-150 hover:bg-forest/[.04]">
      <label className="flex min-w-0 flex-1 cursor-pointer items-start gap-3">
        <button
          type="button"
          role="checkbox"
          aria-checked={done}
          onClick={() => onToggle(!done)}
          className={`mt-[3px] flex h-[18px] w-[18px] shrink-0 items-center justify-center rounded-xs border-[1.5px] transition-[background-color,border-color] duration-200 ${
            done ? "border-forest bg-forest text-on-accent" : "border-line-strong bg-surface group-hover:border-forest/60"
          }`}
        >
          {done && <CheckIcon size={12} strokeWidth={3} className="animate-check-pop" />}
        </button>
        <span className={`text-body leading-relaxed transition-colors duration-200 ${done ? "text-ink-soft line-through decoration-ink-faint/70" : "text-ink"}`}>
          {owner && <span className="mr-1.5 font-semibold">{owner}</span>}
          {rest}
        </span>
      </label>
      {ticket && (
        <a
          href={ticket.url}
          target="_blank"
          rel="noreferrer"
          className="chip mt-[3px] bg-sky-soft text-sky-deep hover:underline"
          data-tip={`Open it in ${ticket.name}`}
        >
          <ArrowUpIcon size={12} className="rotate-45" />
          In {ticket.name}
        </a>
      )}
      <div className="-my-1.5 flex shrink-0 items-center opacity-0 transition-opacity duration-150 focus-within:opacity-100 group-hover:opacity-100">
        {onSend && tracker && !ticket && (
          <button
            className="icon-btn h-8 w-8"
            onClick={() => void send()}
            disabled={sending}
            aria-label={`Send to ${tracker}`}
            data-tip={`Send to ${tracker}`}
          >
            {sending ? <Spinner /> : <SendIcon size={16} />}
          </button>
        )}
        {onDelete && (
          <button
            className="icon-btn h-8 w-8 hover:!bg-trail-soft hover:!text-trail-deep"
            onClick={onDelete}
            aria-label="Delete task"
            data-tip="Delete task (not done, just gone)"
          >
            <TrashIcon size={16} />
          </button>
        )}
      </div>
    </div>
  );
}
