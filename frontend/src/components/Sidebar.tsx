import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { api } from "../api";
import type { MeetingListItem, SearchHit } from "../api";
import { dayGroup, formatDuration, formatTime } from "../format";
import { ChatIcon, CheckSquareIcon, CloseIcon, DownloadIcon, MicIcon, SearchIcon, SlidersIcon, TrashIcon } from "./icons";
import { Logo } from "./illustrations";
import SystemStatus from "./SystemStatus";
import { Pulse, Skeleton, Spinner, StatusChip } from "./ui";

export type View = "main" | "settings" | "tasks" | "ask" | "import";

interface Props {
  meetings: MeetingListItem[];
  loaded: boolean;
  selectedId: number | null;
  recording: boolean;
  elapsed: number;
  onSelect: (id: number) => void;
  onNew: () => void;
  onDelete: (m: MeetingListItem) => void;
  view: View;
  onView: (v: View) => void;
  openTasks: number;
  /** In the narrow-window drawer, where glass over busy content reads as smudges. */
  opaque?: boolean;
}

/** Renders a search snippet, turning the … markers into highlights. */
function Snippet({ text }: { text: string }) {
  return (
    <>
      {text.split(/([^]*)/).map((part, i) =>
        part.startsWith("") ? (
          <mark key={i} className="rounded-xs bg-sun-soft px-0.5 text-sun-deep">
            {part.slice(1, -1)}
          </mark>
        ) : (
          <span key={i}>{part}</span>
        ),
      )}
    </>
  );
}

function NavItem({ active, onClick, icon, label, badge }: { active: boolean; onClick: () => void; icon: ReactNode; label: string; badge?: number }) {
  return (
    <button
      onClick={onClick}
      aria-current={active ? "page" : undefined}
      className={`flex h-9 w-full items-center gap-2.5 rounded-md px-2.5 text-ui font-medium transition-[background-color,color,transform] duration-150 ease-out hover:translate-x-px ${
        active ? "bg-forest/[.08] text-ink" : "text-ink-soft hover:bg-forest/[.05] hover:text-ink"
      }`}
    >
      <span className={`transition-colors ${active ? "text-forest" : ""}`}>{icon}</span>
      <span className="flex-1 text-left">{label}</span>
      {badge ? (
        <span className="min-w-[20px] rounded-full bg-forest/10 px-1.5 py-0.5 text-center text-caption font-semibold tabular-nums text-forest-deep">
          {badge}
        </span>
      ) : null}
    </button>
  );
}

function MeetingRow({ m, active, onSelect, onDelete }: { m: MeetingListItem; active: boolean; onSelect: () => void; onDelete: () => void }) {
  const busy = m.status === "recording";
  return (
    <div className="group relative transition-transform duration-150 ease-out hover:translate-x-px">
      <button
        onClick={onSelect}
        aria-current={active ? "page" : undefined}
        className={`block w-full rounded-md px-3 py-2.5 pr-9 text-left transition-[background-color,box-shadow] duration-150 ${
          active ? "bg-surface shadow-sm ring-1 ring-line" : "group-hover:bg-forest/[.05]"
        }`}
      >
        <div className={`truncate text-ui leading-5 ${active ? "font-semibold text-ink" : "font-medium text-ink"}`}>{m.title}</div>
        <div className="mt-0.5 flex items-center gap-1.5 text-meta leading-4 text-ink-soft">
          <span className="tabular-nums">{formatTime(m.created_at)}</span>
          <span className="text-ink-faint" aria-hidden="true">
            ·
          </span>
          <span className="tabular-nums">{formatDuration(m.duration_sec)}</span>
          <span className="ml-auto">
            <StatusChip status={m.status} />
          </span>
        </div>
      </button>
      {!busy && (
        <button
          onClick={onDelete}
          className="icon-btn absolute right-1.5 top-2 h-7 w-7 opacity-0 hover:!bg-trail-soft hover:!text-trail-deep focus-visible:opacity-100 group-hover:opacity-100"
          aria-label={`Delete ${m.title}`}
          data-tip="Delete meeting"
        >
          <TrashIcon size={16} />
        </button>
      )}
    </div>
  );
}

export default function Sidebar({ meetings, loaded, selectedId, recording, elapsed, onSelect, onNew, onDelete, view, onView, openTasks, opaque }: Props) {
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<SearchHit[] | null>(null);
  const [searching, setSearching] = useState(false);
  const searchBox = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const q = query.trim();
    if (!q) {
      setHits(null);
      return;
    }
    setSearching(true);
    const t = setTimeout(() => {
      api
        .search(q)
        .then(setHits)
        .catch(() => setHits([]))
        .finally(() => setSearching(false));
    }, 180);
    return () => clearTimeout(t);
  }, [query]);

  // ⌘K / Ctrl+K jumps to search.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        searchBox.current?.focus();
        searchBox.current?.select();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const groups: { label: string; items: MeetingListItem[] }[] = [];
  for (const m of meetings) {
    const label = dayGroup(m.created_at);
    const last = groups[groups.length - 1];
    if (last?.label === label) last.items.push(m);
    else groups.push({ label, items: [m] });
  }
  const mac = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform);

  return (
    <aside
      className={`chrome relative z-10 flex h-full w-[288px] shrink-0 flex-col border-r border-line shadow-chrome ${opaque ? "chrome-opaque" : ""}`}
    >
      {/* brand */}
      <div className="flex items-center gap-3 px-6 pb-5 pt-7">
        <Logo size={40} />
        <div className="min-w-0">
          <div className="font-display text-wordmark font-semibold leading-none tracking-[-0.02em] text-ink">Trailmix</div>
          <div className="mt-1.5 text-caption leading-none text-ink-faint">notes from along the way</div>
        </div>
      </div>

      <div className="px-4">
        {recording ? (
          <button
            onClick={onNew}
            className="btn h-11 w-full justify-between rounded-md border border-trail/20 bg-trail-soft px-4 text-label text-trail-deep hover:brightness-[1.02]"
          >
            <span className="flex items-center gap-2.5">
              <Pulse />
              Recording
            </span>
            <span className="font-mono text-ui tabular-nums">{formatDuration(elapsed)}</span>
          </button>
        ) : (
          <button onClick={onNew} className="group btn btn-primary h-11 w-full rounded-md text-label font-semibold">
            <MicIcon size={18} className="transition-transform duration-200 ease-spring group-hover:-rotate-6 group-hover:scale-110" />
            New recording
          </button>
        )}

        <div className="relative mt-4">
          <SearchIcon size={16} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-faint" />
          <input
            ref={searchBox}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Escape") {
                setQuery("");
                e.currentTarget.blur();
              }
            }}
            placeholder="Search"
            className="field h-9 w-full border-transparent bg-forest/[.045] py-0 pl-9 pr-12 shadow-none hover:border-line focus:bg-surface"
            aria-label="Search meetings"
          />
          <span className="absolute right-2 top-1/2 flex -translate-y-1/2 items-center">
            {query ? (
              <button onClick={() => setQuery("")} className="icon-btn h-6 w-6" aria-label="Clear search">
                {searching ? <Spinner /> : <CloseIcon size={16} />}
              </button>
            ) : (
              <kbd className="kbd pointer-events-none [@media(pointer:coarse)]:hidden">{mac ? "⌘K" : "Ctrl K"}</kbd>
            )}
          </span>
        </div>
      </div>

      <nav className="mt-3 space-y-0.5 px-3" aria-label="Views">
        <NavItem active={view === "tasks"} onClick={() => onView("tasks")} icon={<CheckSquareIcon size={16} />} label="Tasks" badge={openTasks} />
        <NavItem active={view === "ask"} onClick={() => onView("ask")} icon={<ChatIcon size={16} />} label="Ask your meetings" />
        <NavItem active={view === "import"} onClick={() => onView("import")} icon={<DownloadIcon size={16} />} label="Import" />
      </nav>

      <nav className="fade-edges-y mt-3 flex-1 overflow-y-auto px-3 pb-4 pt-1" aria-label="Meetings">
        {hits !== null && (
          <section className="animate-fade-in">
            <h3 className="eyebrow px-3 pb-1.5 pt-2">
              {hits.length ? `${hits.length} match${hits.length === 1 ? "" : "es"}` : "No matches"}
            </h3>
            {hits.map((h) => (
              <button
                key={h.id}
                onClick={() => onSelect(h.id)}
                className={`mb-0.5 block w-full rounded-md px-3 py-2.5 text-left transition-colors duration-150 ${
                  h.id === selectedId ? "bg-surface shadow-sm ring-1 ring-line" : "hover:bg-forest/[.05]"
                }`}
              >
                <div className="truncate text-ui font-medium">{h.title}</div>
                <div className="mt-0.5 line-clamp-2 text-meta leading-snug text-ink-soft">
                  <Snippet text={h.snippet} />
                </div>
                <div className="mt-1 text-meta text-ink-soft">
                  {dayGroup(h.created_at)} · {formatTime(h.created_at)}
                </div>
              </button>
            ))}
          </section>
        )}

        {hits === null && !loaded && (
          <div className="space-y-4 px-3 pt-3" aria-label="Loading meetings">
            {[0, 1, 2].map((i) => (
              <div key={i} className="space-y-2">
                <Skeleton className="h-3.5 w-3/4" />
                <Skeleton className="h-3 w-1/3" />
              </div>
            ))}
          </div>
        )}

        {hits === null && loaded && meetings.length === 0 && (
          <div className="mx-1 mt-3 rounded-lg px-4 py-6 text-center inset-surface">
            <p className="text-ui font-medium">No meetings yet</p>
            <p className="mt-1 text-hint leading-snug text-ink-soft">Your first recording will appear here.</p>
          </div>
        )}

        {hits === null &&
          groups.map((g) => (
            <section key={g.label} className="mb-2">
              <h3 className="eyebrow px-3 pb-1 pt-3">{g.label}</h3>
              <div className="space-y-px">
                {g.items.map((m) => (
                  <MeetingRow key={m.id} m={m} active={m.id === selectedId} onSelect={() => onSelect(m.id)} onDelete={() => onDelete(m)} />
                ))}
              </div>
            </section>
          ))}
      </nav>

      <div className="border-t border-line px-3 pb-2 pt-2">
        <NavItem active={view === "settings"} onClick={() => onView("settings")} icon={<SlidersIcon size={16} />} label="Settings" />
      </div>
      <SystemStatus />
    </aside>
  );
}
