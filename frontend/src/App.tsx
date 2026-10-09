import { useCallback, useEffect, useRef, useState } from "react";
import { AUTH_EVENT, api, inApp, quitApp } from "./api";
import type {
  Bookmark,
  DraftLine,
  Health,
  LiveRecording,
  Meeting,
  MeetingListItem,
  NativeRecorder,
  Provider,
  Task,
  Workspace,
  WorkspaceFilter,
  TaskWithMeeting,
} from "./api";
import { startRecording } from "./capture";
import type { ActiveRecording } from "./capture";
import { formatDuration, tildePath } from "./format";
import AskPanel from "./components/AskPanel";
import { MenuIcon, MicIcon } from "./components/icons";
import { Ambient, Logo, TrailScene } from "./components/illustrations";
import Login from "./components/Login";
import MeetingView from "./components/MeetingView";
import type { Tab } from "./components/MeetingView";
import { savedMicId } from "./components/MicPicker";
import Recorder from "./components/Recorder";
import Settings from "./components/Settings";
import ImportPanel from "./components/ImportPanel";
import ModelsBanner from "./components/ModelsBanner";
import UpdateBanner from "./components/UpdateBanner";
import SetupCard from "./components/SetupCard";
import Sidebar from "./components/Sidebar";
import type { View } from "./components/Sidebar";
import TasksView from "./components/TasksView";
import { inWorkspace } from "./components/Workspaces";
import {
  PageHeader,
  Pulse,
  Skeleton,
  Toast,
  TooltipLayer,
  useConfirm,
} from "./components/ui";
import type { ToastState } from "./components/ui";
import { useTheme } from "./theme";

const POLL_MS = 2000;
const WORKSPACE_KEY = "trailmix.workspace";
const LIVE_POLL_MS = 2000;
const HEALTH_RETRY_MS = 5000;
// Statuses where the backend is working (or about to); the rest are resting states that need no polling.
const REST = ["done", "error", "ready_transcribe", "ready_summarize"]; // waiting_confirm continues by itself
const isActive = (status: string) => !REST.includes(status);

/** A boolean UI preference remembered in this browser. */
function usePref(
  key: string,
  initial: boolean,
): [boolean, (v: boolean) => void] {
  const [value, setValue] = useState(() => {
    try {
      const v = localStorage.getItem(key);
      return v === null ? initial : v === "1";
    } catch {
      return initial;
    }
  });
  const set = useCallback(
    (v: boolean) => {
      setValue(v);
      try {
        localStorage.setItem(key, v ? "1" : "0");
      } catch {
        /* private mode etc. */
      }
    },
    [key],
  );
  return [value, set];
}

/** True when the sidebar sits beside the content rather than in a drawer. */
function useWide() {
  const query = "(min-width: 1024px)";
  const [wide, setWide] = useState(
    () => window.matchMedia?.(query).matches ?? true,
  );
  useEffect(() => {
    const mq = window.matchMedia?.(query);
    if (!mq) return;
    const on = () => setWide(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  return wide;
}

export default function App() {
  const theme = useTheme();
  const [authState, setAuthState] = useState<"checking" | "ok" | "needed">(
    "checking",
  );
  const [quit, setQuit] = useState(false);
  const [meetings, setMeetings] = useState<MeetingListItem[]>([]);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [workspace, setWorkspaceState] = useState<WorkspaceFilter>(() => {
    try {
      const saved = localStorage.getItem(WORKSPACE_KEY);
      return saved === "none" ? "none" : saved && /^\d+$/.test(saved) ? Number(saved) : "all";
    } catch {
      return "all";
    }
  });
  const [allTasks, setAllTasks] = useState<TaskWithMeeting[]>([]);
  const [listLoaded, setListLoaded] = useState(false);
  const wide = useWide();
  const [drawer, setDrawer] = useState(false);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [meeting, setMeeting] = useState<Meeting | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [backendDown, setBackendDown] = useState(false);
  const [tab, setTab] = useState<Tab>("summary");
  const [view, setView] = useState<View>("main");
  const [toast, setToast] = useState<ToastState | null>(null);
  const [confirm, confirmDialog] = useConfirm();

  const [micDeviceId, setMicDeviceId] = useState(savedMicId);
  const [includeMeetingAudio, setIncludeMeetingAudio] = usePref(
    "trailmix.meetingAudio",
    true,
  );
  const [liveDraft, setLiveDraft] = usePref("trailmix.liveDraft", true);
  const [rec, setRec] = useState<ActiveRecording | null>(null);
  const [starting, setStarting] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<DraftLine[]>([]);
  const [marks, setMarks] = useState<Bookmark[]>([]);
  const recRef = useRef<ActiveRecording | null>(null);
  // A recording another app is capturing (normally the menu bar helper). Followed by polling /api/live.
  const [remote, setRemote] = useState<LiveRecording | null>(null);
  // The menu bar recorder, if it's running: then Record records through it (both sides of any call, no picker).
  const [native, setNative] = useState<NativeRecorder | null>(null);
  const [remoteStopping, setRemoteStopping] = useState(false);
  const remoteRef = useRef<LiveRecording | null>(null);
  const remoteAt = useRef(0); // when `remote` was fetched, so its timer can tick between polls
  const onRecorderScreenRef = useRef(true);

  const fail = useCallback(
    (text: string) => setToast({ tone: "error", text }),
    [],
  );
  const closeToast = useCallback(() => setToast(null), []);

  // Hosted installs need a sign-in; local ones answer "ok" straight away.
  useEffect(() => {
    const check = () =>
      api
        .auth()
        .then((a) => setAuthState(a.ok ? "ok" : "needed"))
        .catch(() => setAuthState("ok")); // backend down: let the normal "can't reach" UI explain
    void check();
    const onAuth = () => setAuthState("needed");
    window.addEventListener(AUTH_EVENT, onAuth);
    return () => window.removeEventListener(AUTH_EVENT, onAuth);
  }, []);

  // Which workspace you're in: remembered here, and told to the server so recordings started from the menu bar
  // or the hotkey go into it too.
  const setWorkspace = useCallback((w: WorkspaceFilter) => {
    setWorkspaceState(w);
    try {
      localStorage.setItem(WORKSPACE_KEY, String(w));
    } catch {
      /* private window */
    }
    void api.putSettings({ current_workspace: typeof w === "number" ? w : 0 }).catch(() => undefined);
  }, []);

  const refreshWorkspaces = useCallback(
    () =>
      api
        .workspaces()
        .then((ws) => {
          setWorkspaces(ws);
          // A workspace that was deleted: back to everything.
          setWorkspaceState((w) => (typeof w === "number" && !ws.some((x) => x.id === w) ? "all" : w));
        })
        .catch(() => undefined),
    [],
  );

  const refreshList = useCallback(async () => {
    try {
      void refreshWorkspaces(); // their meeting counts change with the list
      setMeetings(await api.listMeetings());
      setListLoaded(true);
      setBackendDown(false);
    } catch {
      setBackendDown(true);
    }
  }, []);

  const refreshSelected = useCallback(
    async (id: number) => {
      try {
        setMeeting(await api.getMeeting(id));
      } catch (e) {
        fail((e as Error).message);
      }
    },
    [fail],
  );

  const refreshHealth = useCallback(
    () =>
      api
        .health()
        .then(setHealth)
        .catch(() => undefined),
    [],
  );
  const refreshTasks = useCallback(
    () =>
      api
        .tasks()
        .then(setAllTasks)
        .catch(() => undefined),
    [],
  );

  // Health doubles as the "is the backend up?" check; retry quietly until it answers.
  useEffect(() => {
    if (authState !== "ok") return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    const check = async () => {
      try {
        const h = await api.health();
        if (!alive) return;
        setHealth(h);
        setBackendDown(false);
        void refreshList();
      } catch {
        if (!alive) return;
        setBackendDown(true);
        timer = setTimeout(check, HEALTH_RETRY_MS);
      }
    };
    void check();
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [refreshList, backendDown, authState]);

  useEffect(() => {
    if (authState === "ok") void refreshTasks();
  }, [meetings, authState, refreshTasks]);

  useEffect(() => {
    if (selectedId === null) {
      setMeeting(null);
      return;
    }
    void refreshSelected(selectedId);
  }, [selectedId, refreshSelected]);

  // Poll while anything is recording / queued / processing.
  const anyActive = meetings.some((m) => isActive(m.status));
  useEffect(() => {
    if (!anyActive) return;
    const t = setInterval(() => {
      void refreshList();
      if (selectedId !== null) void refreshSelected(selectedId);
    }, POLL_MS);
    return () => clearInterval(t);
  }, [anyActive, selectedId, refreshList, refreshSelected]);

  useEffect(() => {
    if (rec) {
      const t = setInterval(
        () => setElapsed((Date.now() - rec.startedAt) / 1000),
        250,
      );
      return () => clearInterval(t);
    }
    if (remote) {
      const tick = () =>
        setElapsed(remote.duration + (Date.now() - remoteAt.current) / 1000);
      tick();
      const t = setInterval(tick, 250);
      return () => clearInterval(t);
    }
  }, [rec, remote]);

  const openMeeting = useCallback((id: number) => {
    setSelectedId(id);
    setTab("summary");
    setView("main");
    setDrawer(false);
  }, []);

  // Recordings can also be started from the menu bar helper. Follow whatever is live; when one ends while
  // you're watching it, open the meeting, just like after a recording made here.
  useEffect(() => {
    if (authState !== "ok") return;
    let alive = true;
    let lastId: number | null = null;
    const tick = async () => {
      api
        .nativeRecorder()
        .then((n) => alive && setNative(n.available ? n : null))
        .catch(() => alive && setNative(null));
      try {
        const all = await api.live();
        if (!alive) return;
        const other =
          all.find((l) => l.id !== recRef.current?.meetingId) ?? null;
        remoteAt.current = Date.now();
        remoteRef.current = other;
        setRemote(other);
        const id = other?.id ?? null;
        if (id !== lastId) {
          const ended = lastId;
          lastId = id;
          void refreshList();
          if (ended !== null) {
            setRemoteStopping(false);
            if (id === null && onRecorderScreenRef.current) openMeeting(ended);
          }
        }
      } catch {
        /* backend down: the health check explains that */
      }
    };
    void tick();
    const t = setInterval(tick, LIVE_POLL_MS);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [authState, refreshList, openMeeting]);

  // Links like /#meeting-12 (the menu bar helper uses them) open that meeting; #import, #settings, #tasks
  // and #ask open those pages.
  useEffect(() => {
    const follow = () => {
      const m = location.hash.match(/^#meeting-(\d+)$/);
      const page = location.hash.slice(1);
      if (m) openMeeting(Number(m[1]));
      else if (["import", "settings", "tasks", "ask"].includes(page))
        setView(page as View);
      else if (page === "updates") {
        // Help → Check for Updates…: Settings, scrolled to the Updates section
        setView("settings");
        setTimeout(
          () =>
            document
              .getElementById("updates")
              ?.scrollIntoView({ behavior: "smooth", block: "start" }),
          150,
        );
      } else return;
      history.replaceState(null, "", location.pathname + location.search);
    };
    follow();
    window.addEventListener("hashchange", follow);
    return () => window.removeEventListener("hashchange", follow);
  }, [openMeeting]);

  // The drawer only exists on narrow windows; close it on Escape or when the window widens.
  useEffect(() => {
    if (wide) setDrawer(false);
  }, [wide]);
  useEffect(() => {
    if (!drawer) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setDrawer(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [drawer]);

  const finishRecording = useCallback(
    async (meetingId: number) => {
      recRef.current = null;
      setRec(null);
      setStopping(false);
      await refreshList();
      openMeeting(meetingId);
    },
    [refreshList, openMeeting],
  );

  const handleStart = async () => {
    setToast(null);
    setNotice(null);
    setDrafts([]);
    setMarks([]);
    setElapsed(0);
    setStarting(true);
    // In the app window, always record through the menu bar recorder (the window can't capture other apps'
    // audio itself). It may not have checked in yet, e.g. just after Trailmix started or updated: keep asking.
    if (native || inApp) {
      // The menu bar recorder picks this up within a second; the live poll then shows the recording here.
      try {
        for (let tries = 0; ; tries++) {
          try {
            await api.startNativeRecorder();
            break;
          } catch (e) {
            if (!inApp || tries >= 10) throw e;
            await new Promise((r) => setTimeout(r, 1000));
          }
        }
        const deadline = Date.now() + 15000;
        while (Date.now() < deadline) {
          const live = (await api.live())[0];
          if (live) {
            remoteAt.current = Date.now();
            remoteRef.current = live;
            setRemote(live);
            void refreshList();
            return;
          }
          await new Promise((r) => setTimeout(r, 400));
        }
        fail(
          "The menu bar recorder didn't start. Open its menu (the Trailmix icon at the top of the screen) to see why.",
        );
      } catch (e) {
        fail(`Couldn't start recording: ${(e as Error).message}`);
      } finally {
        setStarting(false);
      }
      return;
    }
    try {
      const r = await startRecording({
        micDeviceId,
        includeMeetingAudio,
        liveDraft,
        onDraft: (line) => setDrafts((d) => [...d, line]),
        onNotice: setNotice,
        onMarked: (b) => setMarks((m) => [...m, b]),
        onDropped: () => {
          const current = recRef.current;
          if (!current) return;
          fail(
            "Lost the connection to the backend. The audio recorded so far is being processed.",
          );
          void finishRecording(current.meetingId);
        },
        onStoppedElsewhere: () => {
          const current = recRef.current;
          if (!current) return;
          setToast({
            tone: "success",
            text: "Recording stopped from the menu bar helper.",
          });
          void current.stop().then(() => finishRecording(current.meetingId));
        },
      });
      recRef.current = r;
      setRec(r);
      if (r.warning) setNotice(r.warning);
      void refreshList();
    } catch (e) {
      const err = e as DOMException;
      fail(
        err.name === "NotAllowedError"
          ? "Permission denied. Allow microphone (and screen sharing) access and try again."
          : `Couldn't start recording: ${err.message}`,
      );
    } finally {
      setStarting(false);
    }
  };

  const handleStop = async () => {
    const r = recRef.current;
    if (r) {
      setStopping(true);
      await r.stop();
      await finishRecording(r.meetingId);
      return;
    }
    const other = remoteRef.current;
    if (!other) return;
    setRemoteStopping(true); // the /api/live poll notices the end and opens the meeting
    try {
      await api.stopLive(other.id);
    } catch (e) {
      setRemoteStopping(false);
      fail((e as Error).message);
    }
  };

  const handleMark = useCallback(() => {
    if (recRef.current) return recRef.current.mark();
    const other = remoteRef.current;
    if (!other) return;
    api
      .markLive(other.id)
      .then((b) =>
        setRemote((cur) =>
          cur && cur.id === other.id
            ? { ...cur, bookmarks: [...cur.bookmarks, b] }
            : cur,
        ),
      )
      .catch((e: Error) => fail(e.message));
  }, [fail]);

  const guarded = async (fn: () => Promise<unknown>, id: number) => {
    try {
      await fn();
    } catch (e) {
      fail((e as Error).message);
    }
    await Promise.all([refreshList(), refreshSelected(id)]);
  };

  const handleRename = async (title: string) => {
    if (!meeting || !title.trim() || title === meeting.title) return;
    await guarded(() => api.renameMeeting(meeting.id, title), meeting.id);
  };

  const handleDelete = async (id: number, title: string) => {
    const ok = await confirm({
      title: "Delete this meeting?",
      body: `“${title}”, its transcript, summary and audio will be removed for good. Exported files are left alone.`,
      confirmLabel: "Delete meeting",
      tone: "danger",
    });
    if (!ok) return;
    try {
      await api.deleteMeeting(id);
      if (selectedId === id) setSelectedId(null);
      setToast({ tone: "success", text: "Meeting deleted." });
    } catch (e) {
      fail((e as Error).message);
    }
    await refreshList();
  };

  const handleDeleteAudio = async () => {
    if (!meeting) return;
    const ok = await confirm({
      title: "Delete the audio?",
      body: "The transcript and summary stay, but you won't be able to replay or re-transcribe this meeting.",
      confirmLabel: "Delete audio",
      tone: "danger",
    });
    if (ok) await guarded(() => api.deleteAudio(meeting.id), meeting.id);
  };

  const handleRetranscribe = async () => {
    if (!meeting) return;
    const ok = await confirm({
      title: "Transcribe this meeting again?",
      body: "Trailmix transcribes the saved audio again with the current settings and writes a new summary. Speaker names, flagged moments and ticked-off tasks are kept.",
      confirmLabel: "Transcribe again",
    });
    if (ok) await guarded(() => api.retranscribe(meeting.id), meeting.id);
  };

  const handleExport = async () => {
    if (!meeting) return;
    await guarded(async () => {
      const { paths } = await api.exportNow(meeting.id);
      setToast({
        tone: "success",
        text: paths.length
          ? `Saved to ${paths.map(tildePath).join(" and ")}`
          : "Nothing to export with the current settings.",
      });
    }, meeting.id);
  };

  const handleToggleTask = async (t: Task, done: boolean) => {
    if (!meeting) return;
    setMeeting({
      ...meeting,
      tasks: meeting.tasks.map((x) => (x.id === t.id ? { ...x, done } : x)),
    }); // instant tick
    await guarded(() => api.setTaskDone(t.id, done), meeting.id);
    void refreshTasks();
  };

  const handleQuit = async () => {
    const ok = await confirm({
      title: "Quit Trailmix?",
      body: inApp
        ? "The window, the menu bar recorder and the background server all stop. Meetings that are mid-way through processing pick up where they left off next time."
        : "The background server stops. Meetings that are mid-way through processing pick up where they left off next time.",
      confirmLabel: "Quit",
      tone: "danger",
    });
    if (!ok) return;
    if (inApp) return quitApp(); // the app stops its server on the way out
    try {
      await api.shutdown();
      setQuit(true);
    } catch (e) {
      fail((e as Error).message);
    }
  };

  if (quit) {
    return (
      <div className="relative flex h-full flex-col items-center justify-center p-5 text-center">
        <Ambient />
        <div className="relative w-full max-w-[440px] animate-enter">
          <div className="art-frame h-44">
            <TrailScene dusk className="h-full w-full" />
          </div>
          <h1 className="page-title mt-6">See you on the trail</h1>
          <p className="mt-2 text-body leading-relaxed text-ink-soft">
            Trailmix has stopped. You can close this window; open Trailmix.app
            to start it again.
          </p>
        </div>
      </div>
    );
  }
  if (authState === "needed")
    return <Login dusk={theme.dusk} onDone={() => setAuthState("ok")} />;
  if (authState === "checking") {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="animate-soft-pulse">
          <Logo size={48} />
        </div>
      </div>
    );
  }

  const showRecorder = selectedId === null;
  onRecorderScreenRef.current = showRecorder && view === "main";
  const liveId = rec?.meetingId ?? remote?.id ?? null;
  const highlighted = view !== "main" ? null : (selectedId ?? liveId);
  const goRecorder = () => {
    setSelectedId(null);
    setView("main");
    setDrawer(false);
  };
  const goView = (v: View) => {
    setView(v);
    setDrawer(false);
  };

  return (
    <div className="relative flex h-full">
      <Ambient />

      {/* Beside the content on wide windows, a drawer on narrow ones. */}
      <div
        className={`fixed inset-y-0 left-0 z-40 transition-[transform,visibility] duration-300 ease-out lg:static lg:z-10 lg:translate-x-0 ${
          drawer
            ? "translate-x-0 shadow-lg"
            : "max-lg:invisible -translate-x-full"
        }`}
      >
        <Sidebar
          meetings={meetings}
          loaded={listLoaded}
          selectedId={highlighted}
          recording={liveId !== null}
          elapsed={elapsed}
          onSelect={(id) => {
            // The meeting being recorded lives on the recorder screen.
            if (id === liveId) goRecorder();
            else openMeeting(id);
          }}
          onNew={goRecorder}
          onDelete={(m) => void handleDelete(m.id, m.title)}
          view={view}
          onView={goView}
          openTasks={
            allTasks.filter(
              (t) => !t.done && inWorkspace(workspace, t.workspace_id),
            ).length
          }
          opaque={!wide}
          workspaces={workspaces}
          workspace={workspace}
          onWorkspace={setWorkspace}
        />
      </div>
      {drawer && !wide && (
        <div
          className="fixed inset-0 z-30 animate-fade-in bg-[var(--scrim)] backdrop-blur-[2px]"
          onClick={() => setDrawer(false)}
          aria-hidden="true"
        />
      )}

      <main className="relative z-10 min-w-0 flex-1 overflow-y-auto">
        {/* Narrow windows: a slim bar with the menu and a way back to recording. */}
        <div className="chrome sticky top-0 z-20 flex h-14 items-center gap-2 border-b border-line px-3 lg:hidden">
          <button
            className="icon-btn h-10 w-10"
            onClick={() => setDrawer(true)}
            aria-label="Open menu"
            aria-expanded={drawer}
          >
            <MenuIcon size={20} />
          </button>
          <div className="flex min-w-0 flex-1 items-center gap-2">
            <Logo size={26} />
            <span className="font-display text-lead font-semibold tracking-[-0.02em]">
              Trailmix
            </span>
          </div>
          {liveId !== null ? (
            <button
              onClick={goRecorder}
              className="btn btn-sm h-9 rounded-full bg-trail-soft px-3 text-trail-deep"
            >
              <Pulse />
              <span className="font-mono tabular-nums">
                {formatDuration(elapsed)}
              </span>
            </button>
          ) : (
            !showRecorder && (
              <button
                onClick={goRecorder}
                className="btn btn-sm btn-primary h-9"
                aria-label="New recording"
              >
                <MicIcon size={16} />
                Record
              </button>
            )
          )}
        </div>

        <div className="relative mx-auto w-full max-w-[872px] px-5 pb-24 pt-7 sm:px-8 lg:px-10 lg:pt-12">
          <UpdateBanner native={native} />
          <ModelsBanner />
          {view === "settings" ? (
            <Settings
              health={health}
              native={native}
              workspaces={workspaces}
              defaultCount={meetings.filter((x) => !x.workspace_id).length}
              onWorkspacesChanged={() => void refreshList()}
              theme={theme.pref}
              onTheme={theme.setPref}
              onChanged={() => void refreshHealth()}
              onQuit={() => void handleQuit()}
              onSignOut={() =>
                void api.logout().then(() => setAuthState("needed"))
              }
            />
          ) : view === "tasks" ? (
            <TasksView
              workspace={workspace}
              onOpenMeeting={openMeeting}
              onChanged={() => void refreshTasks()}
              onError={fail}
            />
          ) : view === "import" ? (
            <ImportPanel
              onImported={() => void refreshList()}
              onOpenMeeting={openMeeting}
              onError={fail}
            />
          ) : view === "ask" ? (
            <div>
              <PageHeader
                title="Ask your meetings"
                subtitle="Questions across everything you've recorded. Answers link back to the meetings they came from."
              />
              <div
                className="panel animate-enter p-5 sm:p-7"
                style={{ animationDelay: "60ms" }}
              >
                <AskPanel
                  health={health}
                  workspaceId={typeof workspace === "number" ? workspace : null}
                  onOpenMeeting={openMeeting}
                  onError={fail}
                />
              </div>
            </div>
          ) : showRecorder ? (
            <>
              {!rec && !remote && (
                <SetupCard
                  health={health}
                  hasMeetings={meetings.length > 0}
                  meetingCount={meetings.length}
                  workspaces={workspaces}
                  native={native}
                  onOpenSettings={(section) => {
                    goView("settings");
                    if (section)
                      setTimeout(
                        () =>
                          document
                            .getElementById(section)
                            ?.scrollIntoView({ behavior: "smooth", block: "start" }),
                        150,
                      );
                  }}
                  onChanged={() => {
                    void refreshHealth();
                    void refreshList();
                  }}
                />
              )}
              <Recorder
                rec={rec}
                remote={rec ? null : remote}
                native={native}
                elapsed={elapsed}
                starting={starting}
                stopping={stopping || remoteStopping}
                micDeviceId={micDeviceId}
                onMicChange={setMicDeviceId}
                includeMeetingAudio={includeMeetingAudio}
                onIncludeMeetingAudio={setIncludeMeetingAudio}
                liveDraft={liveDraft}
                onLiveDraft={setLiveDraft}
                notice={notice}
                drafts={drafts}
                health={health}
                backendDown={backendDown}
                bookmarks={marks}
                dusk={theme.dusk}
                onStart={handleStart}
                onStop={handleStop}
                onMark={handleMark}
              />
            </>
          ) : meeting && meeting.id === selectedId ? (
            <MeetingView
              meeting={meeting}
              health={health}
              tab={tab}
              onTab={setTab}
              onRename={(t) => void handleRename(t)}
              onDelete={() => void handleDelete(meeting.id, meeting.title)}
              onDeleteAudio={() => void handleDeleteAudio()}
              onRetranscribe={() => void handleRetranscribe()}
              onKeepForever={(keep) =>
                void guarded(
                  () => api.keepForever(meeting.id, keep),
                  meeting.id,
                )
              }
              onExport={() => void handleExport()}
              workspaces={workspaces}
              onRetitle={async () => {
                try {
                  await api.retitle(meeting.id);
                  await refreshSelected(meeting.id);
                  await refreshList();
                } catch (e) {
                  fail((e as Error).message);
                }
              }}
              onWorkspace={(id) =>
                void guarded(
                  () => api.setMeetingWorkspace(meeting.id, id),
                  meeting.id,
                ).then(() => refreshList())
              }
              onConfirm={() =>
                void guarded(() => api.confirm(meeting.id), meeting.id)
              }
              onRetry={() =>
                void guarded(() => api.retry(meeting.id), meeting.id)
              }
              onSummarize={(p: Provider, template?: string) =>
                void guarded(
                  () => api.summarize(meeting.id, p, template),
                  meeting.id,
                )
              }
              onBookmarks={(b) =>
                void guarded(() => api.setBookmarks(meeting.id, b), meeting.id)
              }
              onSpeakers={(n) =>
                void guarded(() => api.setSpeakers(meeting.id, n), meeting.id)
              }
              onToggleTask={(t, d) => void handleToggleTask(t, d)}
              onError={fail}
            />
          ) : (
            <div className="space-y-4 pt-2" aria-label="Loading meeting">
              <Skeleton className="h-3.5 w-56" />
              <Skeleton className="h-9 w-2/3" />
              <Skeleton className="mt-8 h-9 w-72" />
              <Skeleton className="h-64 w-full rounded-xl" />
            </div>
          )}
        </div>
      </main>

      <Toast toast={toast} onClose={closeToast} />
      <TooltipLayer />
      {confirmDialog}
    </div>
  );
}
