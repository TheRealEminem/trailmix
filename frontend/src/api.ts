export type MeetingStatus =
  | "recording"
  | "queued"
  | "ready_transcribe"
  | "waiting_confirm"
  | "transcribing"
  | "ready_summarize"
  | "summarizing"
  | "done"
  | "error";

export interface MeetingListItem {
  id: number;
  title: string;
  created_at: string;
  duration_sec: number;
  status: MeetingStatus;
}

export interface SearchHit extends MeetingListItem {
  /** Contains  …  around matched words. */
  snippet: string;
}

export interface Segment {
  start: number;
  end: number;
  speaker: string | null;
  text: string;
}

export interface DraftLine {
  start: number;
  speaker: string | null;
  text: string;
}

export interface Bookmark {
  t: number;
  note: string;
}

export interface Task {
  id: number;
  text: string;
  done: boolean;
}

export interface TaskWithMeeting extends Task {
  meeting_id: number;
  meeting_title: string;
  meeting_created_at: string;
}

export interface QA {
  q: string;
  a: string;
  provider: string;
  at: string;
}

export type ProviderId = "ollama" | "anthropic" | "openai" | "gemini" | "deepseek" | "custom";

export interface Settings {
  auto_transcribe: boolean;
  auto_summarize: boolean;
  auto_title: boolean;
  your_name: string;
  summary_provider: ProviderId;
  summary_fallback: ProviderId | "none";
  summary_template: string;
  custom_template: string;
  ollama_url: string;
  ollama_model: string;
  anthropic_model: string;
  anthropic_api_key: string;
  openai_model: string;
  openai_api_key: string;
  gemini_model: string;
  gemini_api_key: string;
  deepseek_model: string;
  deepseek_api_key: string;
  custom_name: string;
  custom_base_url: string;
  custom_model: string;
  custom_api_key: string;
  transcribe_engine: "local" | "remote";
  transcribe_url: string;
  transcribe_api_key: string;
  transcribe_model: string;
  transcribe_live_model: string;
  auto_export: boolean;
  export_dir: string;
  export_formats: ExportFormat[];
  export_audio: boolean;
  export_summary: boolean;
  export_transcript: boolean;
  export_separate_files: boolean;
  /** Only in responses: "…1234" for a stored key, null for none. */
  secret_hints?: Record<string, string | null>;
  granola_api_key?: string;
}

export interface Meeting extends MeetingListItem {
  wait_reason: string | null;
  transcript: string | null;
  segments: Segment[] | null;
  has_system: boolean;
  summary: string | null;
  summary_provider: string | null;
  summary_error: string | null;
  error: string | null;
  audio_bytes: number;
  audio_deleted: boolean;
  audio_expires_at: string | null;
  keep_audio: boolean;
  export_folder: string | null;
  draft: DraftLine[] | null;
  exported_paths: string[];
  export_error: string | null;
  title_auto: boolean;
  bookmarks: Bookmark[];
  speaker_names: Record<string, string>;
  qa: QA[];
  tasks: Task[];
}

export interface ProviderInfo {
  id: ProviderId;
  label: string;
  model: string;
  ready: boolean;
  reason: string;
  local: boolean;
}

export interface Health {
  summary: ProviderInfo;
  fallback: ProviderInfo | null;
  transcription: {
    engine: "local" | "remote";
    model: string;
    live_model: string;
    url: string | null;
    local_available: boolean;
  };
  templates: { id: string; name: string }[];
  providers: { id: ProviderId; label: string; kind: "local" | "cloud" | "custom" }[];
  audio_retention_days: number;
  auth: boolean;
  recording: boolean;
}

/** A recording in progress, whichever app is capturing it. */
export interface LiveRecording {
  id: number;
  title: string;
  /** "web" = a Trailmix window, "helper" = the menu bar helper */
  client: "web" | "helper";
  started_at: number;
  /** seconds of audio received so far */
  duration: number;
  has_system: boolean;
  draft: boolean;
  drafts: DraftLine[];
  bookmarks: Bookmark[];
}

export interface SystemInfo {
  ram_total_gb: number;
  ram_available_gb: number;
  ram_used_percent: number;
  swap_used_gb: number;
  cpu_percent: number;
  disk_free_gb: number;
  audio_gb: number;
  headroom_gb: number;
  final_model_gb: number;
  ollama_model_gb: number;
}

export type Provider = "auto" | ProviderId;

/** Fired when the server says we need to sign in (hosted installs only). */
export const AUTH_EVENT = "trailmix:auth-required";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, { credentials: "same-origin", ...init });
  if (res.status === 401 && !path.startsWith("/login")) window.dispatchEvent(new Event(AUTH_EVENT));
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  return res.status === 204 ? (undefined as T) : res.json();
}

const json = (method: string, body?: unknown): RequestInit => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: body === undefined ? undefined : JSON.stringify(body),
});

/** Trailmix's menu bar recorder, when it's running and checking in with this server. */
export interface NativeRecorder {
  available: boolean;
  mic?: string;
  source?: string;
  machine?: string;
  version?: string;
  mic_allowed?: boolean;
  sound_check?: SoundCheckResult | null;
  shortcut_record?: string;
  shortcut_mark?: string;
  open_at_login?: boolean | null;
  update?: { version: string; state: "available" | "downloading" | "installing" | "failed"; progress?: number | null; error?: string; notes?: string } | null;
}

export interface SoundCheckResult {
  running?: boolean;
  at?: number;
  mic?: "ok" | "quiet" | "denied" | "error";
  system?: "ok" | "silent" | "error";
  detail?: string;
}

export type ExportFormat = "pdf" | "docx" | "odt" | "md" | "txt";

export interface ExportJob {
  active?: boolean;
  total?: number;
  done?: number;
  errors?: string[];
  folder?: string;
}

export interface GranolaNote {
  id: string;
  title: string;
  created_at: string;
  imported: boolean;
  meeting_id: number | null;
}

export interface ImportJob {
  active?: boolean;
  total?: number;
  done?: number;
  imported?: number;
  skipped?: number;
  errors?: string[];
}

export interface OllamaStatus {
  can_install: boolean;
  installed: boolean;
  install: { active: boolean; progress: number | null; status: string; error: string | null } | null;
  reachable: boolean;
  models: string[];
  using: string;
  recommended: string;
  pull: { model: string; active: boolean; progress: number | null; status: string; error: string | null } | null;
}

export interface SpeechModel {
  kind: "live" | "final";
  repo: string;
  label: string;
  installed: boolean;
  downloading: boolean;
  progress: number | null;
  error: string | null;
  size_gb: number;
}

export interface SpeechModels {
  local: boolean;
  models: SpeechModel[];
}

/** Running inside Trailmix.app's own window (rather than a browser): the app can be asked to quit. */
type AppBridge = { postMessage: (message: string) => void };
const bridge = (window as unknown as { webkit?: { messageHandlers?: { trailmix?: AppBridge } } }).webkit?.messageHandlers?.trailmix;
export const inApp = !!bridge;
export const quitApp = () => bridge?.postMessage("quit");
/** In the app window: show a file or folder in Finder. */
export const revealInFinder = (path: string) => bridge?.postMessage(`reveal:${path}`);

export const api = {
  auth: () => request<{ required: boolean; ok: boolean }>("/auth"),
  login: (token: string) => request<{ ok: true }>("/login", json("POST", { token })),
  logout: () => request<{ ok: true }>("/logout", json("POST")),
  shutdown: () => request<{ ok: true }>("/shutdown", json("POST")),

  health: () => request<Health>("/health"),
  system: () => request<SystemInfo>("/system"),
  speechModels: () => request<SpeechModels>("/models"),
  downloadSpeechModels: () => request<SpeechModels>("/models/download", json("POST")),
  getSettings: () => request<Settings>("/settings"),
  putSettings: (changes: Partial<Settings>) => request<Settings>("/settings", json("PUT", changes)),
  providerModels: (id: ProviderId) => request<{ models: string[] }>(`/providers/${id}/models`),
  providerTest: (id: ProviderId) => request<{ message: string }>(`/providers/${id}/test`, json("POST")),
  transcriptionTest: () => request<{ message: string }>("/transcription/test", json("POST")),

  listMeetings: () => request<MeetingListItem[]>("/meetings"),
  search: (q: string) => request<SearchHit[]>(`/search?q=${encodeURIComponent(q)}`),
  getMeeting: (id: number) => request<Meeting>(`/meetings/${id}`),
  startMeeting: (title = "") => request<{ id: number }>("/meetings", json("POST", { title })),
  streamUrl: (id: number, draft: boolean) => {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    return `${proto}://${location.host}/api/meetings/${id}/stream?draft=${draft ? 1 : 0}&client=web`;
  },
  live: () => request<LiveRecording[]>("/live"),
  nativeRecorder: () => request<NativeRecorder>("/recorder"),
  ollama: () => request<OllamaStatus>("/ollama"),
  granolaNotes: () => request<{ notes: GranolaNote[] }>("/import/granola/notes"),
  importGranola: (note_ids: string[], keep_summary: boolean) => request<ImportJob>("/import/granola", json("POST", { note_ids, keep_summary })),
  granolaImportStatus: () => request<ImportJob>("/import/granola"),
  exportAll: (include_audio: boolean) => request<ExportJob>("/export/all", json("POST", { include_audio })),
  exportAllStatus: () => request<ExportJob>("/export/all"),
  keepForever: (id: number, keep: boolean) => request<Meeting>(`/meetings/${id}/keep`, json("POST", { keep })),
  importTrailmix: (files: File[]) => {
    const form = new FormData();
    for (const f of files) {
      form.append("files", f);
      form.append("paths", (f as File & { webkitRelativePath?: string }).webkitRelativePath || f.name);
    }
    return request<{ imported: number[]; skipped: number; problems: string[] }>("/import/trailmix", { method: "POST", body: form });
  },
  importText: (text: string, title: string, date: string) => request<{ id: number }>("/import/text", json("POST", { text, title, date })),
  installOllama: () => request<OllamaStatus>("/ollama/install", json("POST")),
  pullOllamaModel: (model = "") => request<OllamaStatus>("/ollama/pull", json("POST", { model })),
  startNativeRecorder: () => request<{ ok: true }>("/recorder/start", json("POST")),
  installUpdate: () => request<{ ok: true }>("/recorder/update", json("POST")),
  setOpenAtLogin: (on: boolean) => request<{ ok: true }>("/recorder/open-at-login", json("POST", { on })),
  soundCheck: () => request<{ ok: true }>("/recorder/sound-check", json("POST")),
  markLive: (id: number, note = "") => request<Bookmark>(`/meetings/${id}/mark`, json("POST", { note })),
  stopLive: (id: number) => request<{ ok: true }>(`/meetings/${id}/stop`, json("POST")),
  audioUrl: (id: number) => `/api/meetings/${id}/audio`,
  renameMeeting: (id: number, title: string) => request<{ ok: true }>(`/meetings/${id}`, json("PATCH", { title })),
  setSpeakers: (id: number, names: Record<string, string>) =>
    request<Record<string, string>>(`/meetings/${id}/speakers`, json("PUT", names)),
  setBookmarks: (id: number, bookmarks: Bookmark[]) =>
    request<Bookmark[]>(`/meetings/${id}/bookmarks`, json("PUT", { bookmarks })),
  confirm: (id: number) => request<{ ok: true }>(`/meetings/${id}/confirm`, json("POST")),
  retry: (id: number) => request<{ ok: true }>(`/meetings/${id}/retry`, json("POST")),
  retranscribe: (id: number) => request<{ ok: true }>(`/meetings/${id}/retranscribe`, json("POST")),
  summarize: (id: number, provider: Provider, template?: string) =>
    request<{ ok: true }>(`/meetings/${id}/summarize`, json("POST", { provider, template })),
  askMeeting: (id: number, question: string) => request<QA>(`/meetings/${id}/ask`, json("POST", { question })),
  clearQuestions: (id: number) => request<void>(`/meetings/${id}/ask`, { method: "DELETE" }),
  askAll: (question: string) =>
    request<{ answer: string; provider: string; sources: { id: number; title: string; created_at: string }[] }>(
      "/ask",
      json("POST", { question }),
    ),
  tasks: () => request<TaskWithMeeting[]>("/tasks"),
  setTaskDone: (id: number, done: boolean) => request<{ ok: true }>(`/tasks/${id}`, json("PATCH", { done })),
  exportNow: (id: number) => request<{ paths: string[] }>(`/meetings/${id}/export`, json("POST")),
  deleteAudio: (id: number) => request<void>(`/meetings/${id}/audio`, { method: "DELETE" }),
  deleteMeeting: (id: number) => request<void>(`/meetings/${id}`, { method: "DELETE" }),
};
