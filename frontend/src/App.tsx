import { Suspense, lazy, useCallback, useEffect, useRef, useState } from "react";
import { AUTH_EVENT, api, inApp, quitApp } from "./api";
import type {
  Bookmark,
  DraftLine,
  Health,
  LiveRecording,
  Meeting,
  MeetingListItem,
  NativeRecorder,
  Task,
  Workspace,
  WorkspaceFilter,
  TaskWithMeeting,
} from "./api";
import { startRecording } from "./capture";
import type { ActiveRecording } from "./capture";
import { formatDuration, tildePath } from "./format";
import { platformWords, setRecorderPlatform } from "./platform";
import { PreflightSheet } from "./components/AIChoice";
import ErrorBoundary from "./components/ErrorBoundary";
import AskPanel from "./components/AskPanel";
import { MenuIcon, MicIcon } from "./components/icons";
import { Ambient, Logo, TrailScene } from "./components/illustrations";
import Login from "./components/Login";
import MeetingView from "./components/MeetingView";
import type { Tab } from "./components/MeetingView";
import { savedMicId } from "./components/MicPicker";
import Recorder from "./components/Recorder";
import ModelsBanner from "./components/ModelsBanner";
import UpdateBanner from "./components/UpdateBanner";
import StatsNotice from "./components/StatsNotice";
import WhatsNew from "./components/WhatsNew";
import { needsSetup } from "./setup";
import WorkspacesTip from "./components/WorkspacesTip";
import Sidebar from "./components/Sidebar";
import type { View } from "./components/Sidebar";
import TasksView from "./components/TasksView";
import ReportButton from "./components/ReportButton";
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
import { EventsContext, useEvents } from "./events";
import { useTheme } from "./theme";

// The big screens you may never open in a sitting come in their own files, fetched the first time they're shown.
const Settings = lazy(() => import("./components/Settings"));
const ImportPanel = lazy(() => import("./components/ImportPanel"));
const Welcome = lazy(() => import("./components/Welcome"));

const POLL_MS = 2000;
const DRAFT_POLL_MS = 700; // while the open meeting's notes are being written, so they come in smoothly
const WORKSPACE_KEY = "trailmix.workspace";
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

/** While a screen's code is on its way (a moment, once). */
function PageLoading() {
  return (
    <div className="space-y-4 pt-2" aria-label="Loading">
      <Skeleton className="h-9 w-2/3" />
      <Skeleton className="h-64 w-full rounded-xl" />
    </div>
  );
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
  const [questionCounts, setQuestionCounts] = useState<Record<string, number>>({});
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
  // The meetings whose AI you're choosing (the pre-flight sheet), and whether an import just finished: then the
  // sheet opens by itself for the imports waiting on that choice.
  const [preflightIds, setPreflightIds] = useState<number[] | null>(null);
  const [justImported, setJustImported] = useState(false);
  // First-run setup, full screen: decided once the meeting list is in (Settings → App can bring it back).
  const [welcome, setWelcome] = useState(false);
  const setupChecked = useRef(false);

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
  // Live captions being said right now, by channel (cleared when that stretch of speech is final).
  const [interims, setInterims] = useState<Record<number, DraftLine>>({});
  const [marks, setMarks] = useState<Bookmark[]>([]);
  const recRef = useRef<ActiveRecording | null>(null);
  // A recording another app is capturing (normally the menu bar helper). Followed by polling /api/live.
  const [remote, setRemote] = useState<LiveRecording | null>(null);
  // This window's own recording as the engine sees it (for the notes written during it).
  const [ownLive, setOwnLive] = useState<LiveRecording | null>(null);
  // The menu bar recorder, if it's running: then Record records through it (both sides of any call, no picker).
  const [native, setNative] = useState<NativeRecorder | null>(null);
  const [remoteStopping, setRemoteStopping] = useState(false);
  const remoteRef = useRef<LiveRecording | null>(null);
  const remoteAt = useRef(0); // when `remote` was last heard of, so its timer can tick between updates
  const lastRemoteId = useRef<number | null>(null);
  const onRecorderScreenRef = useRef(true);
  // The menu bar recorder, recordings in progress, memory and the speech models, pushed as they change.
  const events = useEvents(authState === "ok");

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

  useEffect(() => {
    if (authState !== "ok" || !listLoaded || setupChecked.current) return;
    setupChecked.current = true;
    api
      .getSettings()
      .then((s) => needsSetup(s, meetings.length) && setWelcome(true))
      .catch(() => undefined);
  }, [authState, listLoaded, meetings.length]);

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
      void api
        .questionCounts()
        .then(setQuestionCounts)
        .catch(() => undefined);
      setMeetings(await api.listMeetings());
      setListLoaded(true);
      setBackendDown(false);
    } catch {
      setBackendDown(true);
    }
  }, [refreshWorkspaces]);

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

  // Once an import's meetings are in the list, ask which AI should process the ones waiting on that choice.
  useEffect(() => {
    if (!justImported) return;
    setJustImported(false);
    const ids = meetings.filter((m) => m.ask_first).map((m) => m.id);
    if (ids.length) setPreflightIds(ids);
  }, [meetings]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (selectedId === null) {
      setMeeting(null);
      return;
    }
    void refreshSelected(selectedId);
  }, [selectedId, refreshSelected]);

  // Poll while anything is recording / queued / processing.
  const anyActive = meetings.some((m) => isActive(m.status));
  const writing = meeting?.status === "summarizing" && meeting.id === selectedId;
  useEffect(() => {
    if (!anyActive) return;
    let tick = 0;
    const t = setInterval(
      () => {
        if (!writing || tick++ % 3 === 0) void refreshList(); // the list needn't keep up with the notes
        if (selectedId !== null) void refreshSelected(selectedId);
      },
      writing ? DRAFT_POLL_MS : POLL_MS,
    );
    return () => clearInterval(t);
  }, [anyActive, writing, selectedId, refreshList, refreshSelected]);

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

  useEffect(() => {
    if (events.recorder?.available) setRecorderPlatform(events.recorder.platform);
    setNative(events.recorder?.available ? events.recorder : null);
  }, [events.recorder]);

  // Recordings can also be started from the menu bar helper. Follow whatever is live; when one ends while
  // you're watching it, open the meeting, just like after a recording made here.
  useEffect(() => {
    const all = events.live;
    const other = all.find((l) => l.id !== recRef.current?.meetingId) ?? null;
    setOwnLive(all.find((l) => l.id === recRef.current?.meetingId) ?? null);
    remoteAt.current = Date.now();
    remoteRef.current = other;
    setRemote(other);
    const id = other?.id ?? null;
    if (id !== lastRemoteId.current) {
      const ended = lastRemoteId.current;
      lastRemoteId.current = id;
      void refreshList();
      if (ended !== null) {
        setRemoteStopping(false);
        if (id === null && onRecorderScreenRef.current) openMeeting(ended);
      }
    }
  }, [events.live, refreshList, openMeeting]);

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

  // The window's title says which Trailmix this is when it isn't the stable release.
  const titleChannel = health?.build === "source" ? " Dev" : native?.update?.beta ? " Beta" : "";
  useEffect(() => {
    document.title = `Trailmix${titleChannel}`;
  }, [titleChannel]);

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
    setInterims({});
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
        const w = platformWords();
        fail(`The ${w.recorderName} didn't start. Open its menu (the Trailmix icon ${w.trayWhere}) to see why.`);
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
        onDraft: (line) => {
          setDrafts((d) => [...d, line]);
          if (line.channel !== undefined)
            setInterims((i) => {
              const { [line.channel!]: _gone, ...rest } = i;
              return rest;
            });
        },
        onInterim: (line) =>
          setInterims((i) => {
            if (line.text) return { ...i, [line.channel]: line };
            const { [line.channel]: _gone, ...rest } = i;
            return rest;
          }),
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
            text: `Recording stopped from the ${platformWords().helperName}.`,
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

  const handleRetranscribe = async (engine?: "gemini") => {
    if (!meeting) return;
    const ok = await confirm(
      engine === "gemini"
        ? {
            title: "Label who said what?",
            body: "The saved audio is sent to Google, which tells the speakers apart (Speaker 1, Speaker 2…) and transcribes it again, then Trailmix writes a new summary. Google removes the audio when it's done. Flagged moments and ticked-off tasks are kept; speaker names you set for other labels will need setting again.",
            confirmLabel: "Send to Google",
          }
        : {
            title: "Transcribe this meeting again?",
            body: "Trailmix transcribes the saved audio again with the current settings and writes a new summary. Speaker names, flagged moments and ticked-off tasks are kept.",
            confirmLabel: "Transcribe again",
          },
    );
    if (ok) await guarded(() => api.retranscribe(meeting.id, engine), meeting.id);
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

  const handleDeleteTask = async (t: Task) => {
    if (!meeting) return;
    const id = meeting.id;
    setMeeting({ ...meeting, tasks: meeting.tasks.filter((x) => x.id !== t.id), tasks_deleted: meeting.tasks_deleted + 1 });
    await guarded(async () => {
      await api.deleteTask(t.id);
      setToast({
        tone: "success",
        text: "Task deleted.",
        action: {
          label: "Undo",
          run: () => void guarded(() => api.restoreTask(t.id), id).then(() => void refreshTasks()),
        },
      });
    }, id);
    void refreshTasks();
  };

  const handleSendTask = async (t: Task) => {
    if (!meeting) return;
    const id = meeting.id;
    await guarded(async () => {
      const { ticket } = await api.sendTask(t.id);
      setToast({ tone: "success", text: `Sent to ${ticket.name}.` });
    }, id);
    void refreshTasks();
  };

  const handleQuit = async () => {
    const ok = await confirm({
      title: "Quit Trailmix?",
      body: inApp
        ? `The window, the ${platformWords().recorderName} and the background server all stop. Meetings that are mid-way through processing pick up where they left off next time.`
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
            Trailmix has stopped. You can close this window; open {platformWords().appName} to start it again.
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

  const waiting = meetings.filter((m) => m.ask_first).map((m) => m.id);
  const channel = health?.build === "source" ? "dev" : native?.update?.beta ? "beta" : null;
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
    <EventsContext.Provider value={events}>
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
          questions={questionCounts}
          health={health}
          onAIChanged={() => void refreshHealth()}
          channel={channel}
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
          {/* A page that fails to draw says so here; the sidebar stays usable, and opening another page starts afresh. */}
          <ErrorBoundary scope="page" key={`${view}-${selectedId}`}>
          {waiting.some((id) => id !== selectedId || view !== "main") && !preflightIds && (
            <div className="panel mb-6 flex animate-enter flex-wrap items-center justify-between gap-3 px-4 py-3 text-label">
              <span className="text-ink-soft">
                <span className="font-medium text-ink">
                  {waiting.length === 1 ? "An import is" : `${waiting.length} imports are`} waiting
                </span>{" "}
                for you to choose which AI processes {waiting.length === 1 ? "it" : "them"}.
              </span>
              <button className="btn btn-sm btn-primary" onClick={() => setPreflightIds(waiting)}>
                Choose AI…
              </button>
            </div>
          )}
          {view === "settings" ? (
            <Suspense fallback={<PageLoading />}>
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
              onRunSetup={() => setWelcome(true)}
              onSignOut={() =>
                void api.logout().then(() => setAuthState("needed"))
              }
            />
            </Suspense>
          ) : view === "tasks" ? (
            <TasksView
              workspace={workspace}
              onOpenMeeting={openMeeting}
              onChanged={() => void refreshTasks()}
              onError={fail}
              onToast={setToast}
            />
          ) : view === "import" ? (
            <Suspense fallback={<PageLoading />}>
            <ImportPanel
              onImported={() => {
                setJustImported(true);
                void refreshList();
              }}
              onOpenMeeting={openMeeting}
              onError={fail}
            />
            </Suspense>
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
              {/* Mounted after setup, which asks about stats itself, so they read what it saved. */}
              {!rec && !remote && !welcome && <StatsNotice />}
              {!rec && !remote && !welcome && <WhatsNew health={health} />}
              {!rec && !remote && (
                <WorkspacesTip
                  meetingCount={meetings.length}
                  workspaces={workspaces}
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
                  onChanged={() => void refreshList()}
                />
              )}
              <Recorder
                rec={rec}
                remote={rec ? null : remote}
                live={rec ? ownLive : remote}
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
                onEngine={(engine) => {
                  if (engine === "gemini" && !health?.transcription.gemini_ready) {
                    setToast({
                      tone: "error",
                      text: "Add a Google API key first (Settings → Transcription), and turn Lockdown mode off.",
                    });
                    return;
                  }
                  void api
                    .putSettings({ transcribe_engine: engine })
                    .then(() => refreshHealth())
                    .catch((e: Error) => setToast({ tone: "error", text: e.message }));
                }}
                notice={notice}
                drafts={drafts}
                interims={Object.values(interims)}
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
              onDate={async (iso) => {
                await api.setMeetingDate(meeting.id, iso);
                await Promise.all([refreshList(), refreshSelected(meeting.id)]);
              }}
              onDelete={() => void handleDelete(meeting.id, meeting.title)}
              onDeleteAudio={() => void handleDeleteAudio()}
              onRetranscribe={(engine) => void handleRetranscribe(engine)}
              onKeepForever={(keep) =>
                void guarded(
                  () => api.keepForever(meeting.id, keep),
                  meeting.id,
                )
              }
              onExport={() => void handleExport()}
              workspaces={workspaces}
              onChanged={() => {
                void refreshSelected(meeting.id);
                void refreshList();
              }}
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
              onChooseAI={() => setPreflightIds([meeting.id])}
              onOpenMeeting={(id) => {
                openMeeting(id);
                void refreshList();
              }}
              onBookmarks={(b) =>
                void guarded(() => api.setBookmarks(meeting.id, b), meeting.id)
              }
              onSpeakers={(n) =>
                void guarded(() => api.setSpeakers(meeting.id, n), meeting.id)
              }
              onToggleTask={(t, d) => void handleToggleTask(t, d)}
              onDeleteTask={(t) => void handleDeleteTask(t)}
              onSendTask={handleSendTask}
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
          </ErrorBoundary>
        </div>
      </main>

      <Toast toast={toast} onClose={closeToast} />
      {/* Not over the recording bar while you take notes. */}
      {!(showRecorder && view === "main" && (rec || remote)) && (
        <ReportButton
          page={
            view === "main"
              ? meeting && meeting.id === selectedId
                ? "Meeting"
                : "Home"
              : view.charAt(0).toUpperCase() + view.slice(1)
          }
          meetingId={view === "main" && meeting ? meeting.id : null}
        />
      )}
      <TooltipLayer />
      {confirmDialog}
      {/* Out of the way while a call is being recorded (say, started from the menu bar); back after. */}
      {welcome && !rec && !remote && (
        <Suspense fallback={null}>
          <Welcome
            health={health}
            native={native}
            dusk={theme.dusk}
            onChanged={() => void refreshHealth()}
            onDone={() => setWelcome(false)}
          />
        </Suspense>
      )}
      {preflightIds && (
        <PreflightSheet
          key={preflightIds.join(",")}
          ids={preflightIds}
          health={health}
          onClose={() => setPreflightIds(null)}
          onStarted={(n) => {
            setPreflightIds(null);
            setToast({ tone: "success", text: n === 1 ? "Started." : `Started ${n} meetings, one after another.` });
            void refreshList();
            if (selectedId !== null) void refreshSelected(selectedId);
          }}
        />
      )}
    </div>
    </EventsContext.Provider>
  );
}
