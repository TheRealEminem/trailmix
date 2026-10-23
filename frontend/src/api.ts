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
  workspace_id: number | null;
  /** An import waiting for you to choose which AI processes it. */
  ask_first: number;
}

export type WorkspaceColor = "forest" | "sky" | "sun" | "trail" | "ink";

/** A part of your life meetings belong to: a job, a committee, personal. */
export interface Workspace {
  id: number;
  name: string;
  color: WorkspaceColor;
  /** What it's about, which helps Trailmix sort meetings into it. */
  about: string;
  position: number;
  meetings: number;
  /** The note style for its meetings ("" = the default in Settings). */
  template: string;
}

/** Which meetings you're looking at: all of them, one workspace, or the ones in none yet. */
export type WorkspaceFilter = "all" | "none" | number;

export type OrganizeKind = "sort" | "resort" | "tag" | "suggest";

export interface OrganizeJob {
  active?: boolean;
  kind?: OrganizeKind;
  total?: number;
  done?: number;
  sorted?: number;
  /** The AI model doing it. */
  model?: string;
  /** After "suggest": workspace ideas from your meetings' tags. */
  suggestions?: { name: string; about: string }[] | null;
  error?: string;
}

/** Work an older, weaker model did that the model you have now would do better. */
export interface Upgrades {
  model: string;
  score: number | null;
  notes: { count: number; from: Record<string, number> };
  tags: { count: number; from: Record<string, number> };
  sorting: { count: number; from: Record<string, number> };
  /** The same notes, rewritten by Gemini as a half-price batch job (needs a Google key; off in Lockdown). */
  notes_batch: { ready: boolean; model: string; score: number | null; count: number; from: Record<string, number> };
  /** Transcripts a speech engine you have now would do better, where the audio is still kept. */
  transcripts: {
    best: { engine: TranscribeEngine; model: string; score: number | null } | null;
    engines: { engine: TranscribeEngine; model: string; score: number | null }[];
    count: number;
    from: Record<string, number>;
  };
}

/** One meeting's transcript being redone (or done) with another engine, in the batch of trials. */
export interface TrialRow extends Trial {
  meeting_id: number;
  title: string;
  /** how the transcript it would replace was made */
  current: string | null;
  /** the share of words that match that transcript (1 = identical), once it's made */
  agreement: number | null;
  apply: boolean;
}

/** What this Mac can comfortably run, for setup. */
export interface DeviceAdvice {
  verdict: "local" | "mixed" | "cloud";
  chip: string;
  ram_gb: number;
  disk_free_gb: number;
  notes: string[];
}

/** Something the AI wasn't sure about in a meeting, for you to answer with a tap. */
export interface MeetingQuestion {
  id: number;
  kind: "them" | "spelling" | "term" | "workspace";
  prompt: string;
  options: { label: string; value: string }[];
  /** You can also type an answer (a name, the right spelling). */
  free_text: boolean;
}

/** A problem report with personal information removed, ready to check and post. */
export interface ProblemReport {
  title: string;
  body: string;
  url: string;
  /** The local model that checked it, or null if only patterns and known names did. */
  model: string | null;
}

export interface FileImport {
  kind: "recording" | "transcript";
  id: number | null;
  skipped: boolean;
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
  /** 0 = your microphone, 1 = the meeting's audio (live captions from Google) */
  channel?: number;
}

/** One transcript of a meeting: the one in use, or another engine's attempt kept to compare. */
export interface TranscriptVersion {
  id: number;
  created_at: string;
  engine: string | null;
  model: string | null;
  made_with: string | null;
  /** recording: made when the meeting was processed; upgrade: made later to compare; original: from before versions */
  source: "recording" | "upgrade" | "original";
  applied: boolean;
  seconds: number | null;
  duration_sec: number | null;
  note: string | null;
  words: number;
  lines: number;
  speakers: string[];
}

export type TranscribeEngine = "local" | "remote" | "gemini";

/** Another engine or model being tried on a meeting's saved audio. */
export interface Trial {
  engine: TranscribeEngine;
  model: string | null;
  state: "queued" | "running" | "done" | "failed" | "cancelled";
  detail: string | null;
  version_id: number | null;
  error: string | null;
}

export interface Comparison {
  a: TranscriptVersion;
  b: TranscriptVersion;
  /** the share of words that match (1 = identical) */
  agreement: number;
  a_words: number;
  b_words: number;
  matched: number;
  changes: { op: "replace" | "delete" | "insert"; a: string; b: string; at: number }[];
  /** both transcripts as one stream: "delete" is only in A, "insert" only in B */
  inline: { op: "equal" | "delete" | "insert" | "skip"; text: string }[];
}

/** What a batch of notes would cost, before anything is sent. */
export interface NotesBatchEstimate {
  meetings: number;
  tokens_in: number;
  /** US dollars at the half-price batch rate, about */
  cost: number | null;
  /** the same at the normal price, for comparison */
  standard_cost: number | null;
  model: string;
  as_of: string;
  skipped: { id: number; why: string }[];
  limit: number;
}

/** Work sent to Google's Batch API: answers come back later and are put in the meetings by themselves. */
export interface BatchJob {
  id: number;
  kind: "notes" | "transcripts";
  model: string;
  state: "pending" | "running" | "succeeded" | "failed" | "cancelled" | "expired";
  created_at: string;
  finished_at: string | null;
  meetings: number;
  applied: boolean;
  est_cost: number | null;
  error: string | null;
  result: { applied: number; failed: number; skipped: Record<string, number>; cost: number } | null;
}

/** What redoing some transcripts would send and cost, before anything is spent. */
export interface TrialEstimate {
  meetings: { id: number; title: string; minutes: number; cost: number | null }[];
  /** minutes of audio that would be sent (each track counts; an upper bound) */
  minutes: number;
  /** US dollars, about (null: the price of this model isn't known) */
  cost: number | null;
  per_minute: number | null;
  model: string | null;
  as_of: string;
  /** how many meetings qualify before the limit */
  available?: number;
  limit?: number;
}

export interface Usage {
  days: number;
  rows: { kind: string; engine: string; model: string | null; seconds: number; cost: number | null; unpriced: number }[];
  cost: number;
  unpriced: number;
  as_of: string;
}

export interface HistoryEntry {
  id: number;
  at: string;
  field: string;
  old_value: string | null;
  new_value: string | null;
  why: string | null;
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
  workspace_id: number | null;
}

export interface QA {
  q: string;
  a: string;
  provider: string;
  at: string;
}

/** What the law says about recording calls in one place (backend/recording_laws.py). Information only. */
export interface RecordingLaw {
  id: string;
  country?: string;
  name: string;
  /** Who must agree: everyone | one person | it depends | no clear rule */
  consent: "all" | "one" | "mixed" | "unknown";
  summary: string;
  notes: string;
  /** Keeping audio for less time that the entry suggests (days, as audio_retention_days), and why */
  retention: { suggest: number; why: string } | null;
  sources: string[];
  reviewed: string;
}

export interface RecordingLaws {
  version: number;
  updated: string;
  disclaimer: string;
  regions: RecordingLaw[];
  fallback: RecordingLaw;
}

/** A question asked while the meeting was still recording (backend/catch_up.py). */
export interface CatchUpEntry extends QA {
  /** who answered, e.g. "Google Gemini" */
  label: string;
  live: true;
  kind: CatchUpKind;
  /** seconds into the recording when it was asked */
  t: number;
}

export type CatchUpKind = "missed" | "explain" | "decided" | "question";

/** Who would answer a catch-up question right now, and about what it costs. */
export interface CatchUpPlan {
  ready: boolean;
  /** why it can't be asked (no live transcript, no AI allowed to answer) */
  reason: string | null;
  provider: string | null;
  label: string | null;
  model: string | null;
  /** answered on this computer */
  local: boolean;
  /** about what one question costs in dollars: 0 on this computer, null where the price isn't known */
  cost: number | null;
  /** the same for each quick question, and for one you type */
  costs: Partial<Record<CatchUpKind, number | null>>;
  /** the token estimate behind it */
  tokens: { in: number; out: number } | null;
  /** the estimate follows the tokens Google counted for earlier answers (else a cautious guess) */
  measured: boolean;
  /** e.g. "Answered by qwen2.5:3b, a small model that fits next to your call" */
  note: string | null;
  /** a model on this Mac that may slow the call down */
  warning: string | null;
  kinds: Record<Exclude<CatchUpKind, "question">, string>;
  /** quick questions this AI isn't good enough for, with why (a small model on this Mac does the basic ones) */
  off: Partial<Record<CatchUpKind, string>>;
  history: CatchUpEntry[];
  as_of: string;
}

export type ProviderId = "ollama" | "anthropic" | "openai" | "gemini" | "deepseek" | "custom";

export interface Settings {
  auto_transcribe: boolean;
  auto_summarize: boolean;
  /** Imports wait for you to pick which AI transcribes them and writes their notes. */
  ask_before_ai: boolean;
  /** The same meeting from Trailmix and Granola (or a pasted transcript): merge, ask, or keep both */
  duplicates: "merge" | "ask" | "keep";
  auto_title: boolean;
  auto_workspace: boolean;
  current_workspace: number;
  ask_questions: boolean;
  vocabulary: string[];
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
  transcribe_engine: "local" | "remote" | "gemini";
  transcribe_url: string;
  transcribe_api_key: string;
  transcribe_model: string;
  transcribe_live_model: string;
  /** Gemini: send the mic and the call mixed into one recording (half the cost) */
  gemini_mix_tracks: boolean;
  gemini_transcribe_model: string;
  live_final: boolean;
  /** Notes written during the meeting (see LiveNotesPlan) */
  live_notes: "auto" | "on" | "off";
  /** Anonymous usage stats (on by default; sent only once the choice was seen) */
  share_stats: boolean;
  stats_notice_seen: boolean;
  /** Lockdown mode: nothing leaves this computer */
  lockdown: boolean;
  /** First-run setup was finished or skipped (Welcome.tsx) */
  setup_done: boolean;
  /** the version whose "What's new" was last closed ("" = never) */
  whats_new_seen: string;
  /** Days a meeting's audio is kept: 0 = deleted once transcribed, -1 = forever */
  audio_retention_days: number;
  /** The recording-laws region you read ("" = not yet), and the table's date when you read it */
  legal_region: string;
  legal_seen: string;
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
  fathom_api_key?: string;
  fireflies_api_key?: string;
  /** sources whose new meetings come in by themselves */
  auto_import: string[];
  /** with their notes as the summary (off: Trailmix writes new ones) */
  auto_import_keep_notes: boolean;
  /** folders whose new recordings and transcripts come in by themselves */
  watch_folders: string[];
}

export interface Meeting extends MeetingListItem {
  wait_reason: string | null;
  transcript: string | null;
  segments: Segment[] | null;
  has_system: boolean;
  summary: string | null;
  summary_provider: string | null;
  summary_error: string | null;
  /** The notes so far, while they're being written (null otherwise). */
  summary_draft: string | null;
  error: string | null;
  audio_bytes: number;
  audio_deleted: boolean;
  audio_expires_at: string | null;
  keep_audio: boolean;
  export_folder: string | null;
  /** Trailmix sorted it there (rather than you). */
  workspace_auto: number;
  /** e.g. "whisper-large-v3-turbo, while recording", or where an imported transcript came from. */
  transcribed_with: string | null;
  /** The model that wrote the notes, e.g. "qwen2.5:7b". */
  summary_model: string | null;
  /** The AI picked for its notes when it isn't the default, e.g. "Google Gemini · gemini-3.6-flash". */
  notes_with: string | null;
  /** Copies of this meeting merged into it (Granola's, a pasted one), oldest first. offset: seconds its transcript was moved. */
  merged: { from: string; title: string; at: string; offset: number }[];
  /** A meeting that looks like the same one (you're asked whether to merge them). */
  duplicate_of: { id: number; title: string; from: string; created_at: string } | null;
  tags: string[];
  exported_paths: string[];
  export_error: string | null;
  title_auto: boolean;
  bookmarks: Bookmark[];
  /** Notes you typed; `t` is seconds into the recording (null when added afterwards). */
  my_notes: MyNote[];
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
    engine: "local" | "remote" | "gemini";
    model: string;
    /** null with Gemini: it transcribes after the recording, not during */
    live_model: string | null;
    url: string | null;
    local_available: boolean;
    /** the accurate model transcribes while recording, so the live transcript is the final one */
    live_final: boolean;
    /** A Google API key is saved and Lockdown is off: "Label speakers" (Gemini) can be used */
    gemini_ready: boolean;
    /** A transcription endpoint is set (and allowed) */
    remote_ready: boolean;
  };
  /** This copy of Trailmix, e.g. "0.14.0" ("dev" from source) */
  version: string;
  /** "app": the installed Trailmix.app (stable or beta); "source": run from the code folder (Trailmix Dev) */
  build: "app" | "source";
  templates: { id: string; name: string }[];
  providers: (ProviderInfo & { kind: "local" | "cloud" | "custom" })[];
  /** Imports wait for you to choose which AI processes them. */
  ask_before_ai: boolean;
  audio_retention_days: number;
  auth: boolean;
  recording: boolean;
}

export interface MyNote {
  t: number | null;
  text: string;
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
  /** The words being said right now (live captions from Google), which may still change */
  interims?: DraftLine[];
  bookmarks: Bookmark[];
  my_notes: MyNote[];
  /** Notes written during the meeting so far, each on a stretch of it */
  notes: { start: number; end: number; text: string }[];
  /** e.g. "Next notes at 20:00", "Paused on battery at 32%. They catch up after the meeting." */
  notes_status: string | null;
}

/** Anonymous usage stats: on or off, the latest events as sent, and today's summary. */
export interface StatsInfo {
  /** this build has somewhere to send them */
  available: boolean;
  on: boolean;
  recent: { event: string; at: string; properties: Record<string, unknown> }[];
  daily: Record<string, unknown>;
}

export const PRIVACY_POLICY = "https://therealeminem.github.io/trailmix/privacy.html";

/** Whether notes are written during meetings with your settings, and why. */
export interface LiveNotesPlan {
  on: boolean;
  /** the notes AI runs on this Mac (so memory and battery matter) */
  local: boolean;
  reason: string;
  warning: string | null;
  ram_gb: number;
  model_gb: number;
  power: { battery: boolean; plugged_in: boolean; percent: number | null; low_power: boolean };
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
  disk_free_percent: number;
  swap_disk_free_percent: number;
  memory_pressure: number;
  ollama_model_gb: number;
}

export type Provider = "auto" | ProviderId;

/** Which AI could process some meetings, and what the picked one would take (see backend/preflight.py). */
export interface Preflight {
  meetings: { id: number; title: string; minutes: number; needs_transcript: boolean }[];
  defaults: { engine: TranscribeEngine; provider: ProviderId; model: string; template: string; transcribe_model: string };
  notes: {
    options: { provider: ProviderId; label: string; kind: "local" | "cloud" | "custom"; local: boolean; model: string; ready: boolean; reason: string }[];
    plan: {
      provider: ProviderId;
      label: string;
      model: string;
      /** Ollama on this computer */
      local: boolean;
      runs_here: boolean;
      /** parts: how many pieces the transcript is read in (1 = in one go) */
      meetings: { id: number; chars: number; parts: number; estimated: boolean }[];
      calls: number;
      tokens_in: number;
      tokens_out: number;
      /** dollars; 0 on this computer, null when the price isn't known */
      cost: number | null;
      window_chars: number;
      ram_needed_gb: number | null;
      ram_free_gb: number;
      ram_total_gb: number;
    };
  };
  transcribe: {
    options: { engine: TranscribeEngine; label: string; local: boolean; ready: boolean; reason: string; models: { id: string; label: string }[] }[];
    plan: { engine: TranscribeEngine; model: string; minutes: number; cost: number | null; per_minute: number | null; ram_needed_gb: number | null; ram_free_gb: number };
  } | null;
  as_of: string;
  recording: boolean;
}

/** One meeting found twice. keep: the meeting that stays (the one with the recording). */
export interface DuplicatePair {
  keep: DuplicateSide;
  other: DuplicateSide;
  score: number;
}
export interface DuplicateSide {
  id: number;
  title: string;
  created_at: string;
  source: string;
  label: string;
  minutes: number;
  has_audio: boolean;
  has_notes: boolean;
}

export interface AIChoice {
  meeting_ids: number[];
  engine?: TranscribeEngine;
  transcribe_model?: string;
  provider?: ProviderId;
  model?: string;
}

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

export interface DateGuess {
  /** "YYYY-MM-DD", or null when nothing in the transcript says. */
  date: string | null;
  sure_of: "day" | "month" | "year" | null;
  why: string;
}

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
  /** "Zoom call started. Record it?" */
  call_reminders?: boolean;
  update?: UpdateInfo | null;
}

/** What Trailmix.app's updater is doing (only the full app updates itself). */
export interface UpdateInfo {
  current?: string;
  state: "idle" | "checking" | "up-to-date" | "offline" | "available" | "downloading" | "installing" | "failed";
  version?: string;
  progress?: number | null;
  error?: string;
  notes?: string;
  checked_at?: number | null;
  beta?: boolean;
  prerelease?: boolean;
  /** the version Go back installs, if there is one */
  previous?: string | null;
  /** a version you went back from: not offered until a newer one exists */
  skipped?: string | null;
  /** the download/install under way is going back */
  going_back?: boolean;
  /** Lockdown mode: no automatic checks (checking by hand still works) */
  lockdown?: boolean;
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

/** Another note taker whose new meetings can come in by themselves, and how its sync is going. */
export interface ImportSource {
  name: string;
  label: string;
  has_key: boolean;
  auto: boolean;
  running: boolean;
  since: string | null;
  last_ok: string | null;
  last_try: string | null;
  last_count: number | null;
  total: number | null;
  error: string | null;
}

export type SyncStart = "now" | "week" | "all";

/** A folder whose new recordings and transcripts come in by themselves. */
export interface WatchedFolder {
  path: string;
  imported: number;
  skipped: number;
  failed: number;
  /** new files seen once, waiting to stop changing */
  waiting: number;
  last_scan: string | null;
  last_count: number | null;
  error: string | null;
}

/** Anything that brings meetings in by itself, one shape for both kinds: a service (Granola) or a watched folder. */
export interface Connection {
  id: string;
  kind: "service" | "folder";
  label: string;
  /** a folder's full path (null for a service) */
  path: string | null;
  /** a service: bringing in new meetings automatically (folders always are) */
  auto: boolean;
  running: boolean;
  /** when it was last checked */
  last: string | null;
  last_count: number | null;
  total: number;
  waiting: number;
  failed: number;
  error: string | null;
  since: string | null;
}

export interface Connections {
  connections: Connection[];
  available: { services: ServiceInfo[]; folders: { label: string; path: string }[] };
}

/** A service that can be connected: how to get its API key, and what plan it needs. */
export interface ServiceInfo {
  name: string;
  label: string;
  steps: string[];
  placeholder: string;
  note: string;
}

export interface WatchedFolders {
  folders: WatchedFolder[];
  /** folders other meeting apps save into, found on this Mac and not watched yet */
  suggestions: { label: string; path: string }[];
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
  recordingLaws: () => request<RecordingLaws>("/recording-laws"),
  providerModels: (id: ProviderId) => request<{ models: string[] }>(`/providers/${id}/models`),
  providerTest: (id: ProviderId) => request<{ message: string }>(`/providers/${id}/test`, json("POST")),
  transcriptionTest: () => request<{ message: string }>("/transcription/test", json("POST")),

  listMeetings: () => request<MeetingListItem[]>("/meetings"),
  search: (q: string) => request<SearchHit[]>(`/search?q=${encodeURIComponent(q)}`),
  getMeeting: (id: number) => request<Meeting>(`/meetings/${id}`),
  startMeeting: (title = "", workspace_id: number | null = null) =>
    request<{ id: number }>("/meetings", json("POST", { title, workspace_id })),
  workspaces: () => request<Workspace[]>("/workspaces"),
  createWorkspace: (w: { name: string; color?: WorkspaceColor; about?: string }) =>
    request<Workspace>("/workspaces", json("POST", w)),
  updateWorkspace: (id: number, w: Partial<Pick<Workspace, "name" | "color" | "about" | "position" | "template">>) =>
    request<Workspace>(`/workspaces/${id}`, json("PATCH", w)),
  deleteWorkspace: (id: number) => request<{ ok: true }>(`/workspaces/${id}`, json("DELETE")),
  setMeetingWorkspace: (id: number, workspace_id: number | null) =>
    request<{ ok: true }>(`/meetings/${id}/workspace`, json("POST", { workspace_id })),
  organize: (kind: OrganizeKind, only_older = false) =>
    request<OrganizeJob>("/organize", json("POST", { kind, only_older })),
  organizeStatus: () => request<OrganizeJob>("/organize"),
  upgrades: () => request<Upgrades>("/upgrades"),
  startTrials: (body: {
    engine: TranscribeEngine;
    model?: string;
    scope?: "better" | "all";
    /** at most this many meetings, newest first */
    limit?: number;
    batch?: boolean;
    apply?: boolean;
    summarize?: boolean;
  }) => request<{ queued: number; skipped: number } & TrialEstimate>("/transcript-trials", json("POST", body)),
  /** What starting these would cost, without starting anything. */
  estimateTrials: (body: { engine: TranscribeEngine; model?: string; scope: "better" | "all"; limit: number; batch?: boolean }) =>
    request<TrialEstimate>("/transcript-trials", json("POST", { ...body, dry_run: true })),
  trialEstimate: (id: number, engine: TranscribeEngine, model?: string, mixTracks?: boolean) =>
    request<TrialEstimate>(
      `/meetings/${id}/trial/estimate?engine=${engine}${model ? `&model=${encodeURIComponent(model)}` : ""}${
        mixTracks === undefined ? "" : `&mix_tracks=${mixTracks}`
      }`,
    ),
  usage: (days = 30) => request<Usage>(`/usage?days=${days}`),
  trials: () =>
    request<{ trials: TrialRow[]; counts: Record<string, number> }>("/transcript-trials"),
  cancelTrials: () => request<{ cancelled: number }>("/transcript-trials", json("DELETE")),
  estimateNotesBatch: (limit: number) =>
    request<NotesBatchEstimate>("/upgrades/notes/batch", json("POST", { limit, dry_run: true })),
  startNotesBatch: (limit: number) =>
    request<{ jobs: number[]; submitted: number; skipped: { id: number; why: string }[]; cost: number }>(
      "/upgrades/notes/batch",
      json("POST", { limit }),
    ),
  batches: () => request<BatchJob[]>("/batches"),
  checkBatches: () => request<{ changed: number; jobs: BatchJob[] }>("/batches/check", json("POST")),
  cancelBatch: (id: number) => request<{ ok: true }>(`/batches/${id}/cancel`, json("POST")),
  upgradeNotes: () => request<{ queued: number }>("/upgrades/notes", json("POST")),
  duplicates: () => request<{ pairs: DuplicatePair[] }>("/duplicates"),
  mergeDuplicates: (pairs: [number, number][] | "all") =>
    request<{ merged: number; kept: number[]; problems: string[] }>(
      "/duplicates/merge",
      json("POST", pairs === "all" ? { all: true } : { pairs }),
    ),
  keepApart: (a: number, b: number) => request<void>("/duplicates/apart", json("POST", { a, b })),
  /** Undoes the last merge into this meeting; returns the meeting that came back. */
  unmerge: (id: number) => request<{ id: number }>(`/meetings/${id}/unmerge`, json("POST")),
  retitle: (id: number) => request<{ title: string }>(`/meetings/${id}/retitle`, json("POST")),
  device: () => request<DeviceAdvice>("/device"),
  questions: (meetingId: number) => request<MeetingQuestion[]>(`/meetings/${meetingId}/questions`),
  questionCounts: () => request<Record<string, number>>("/questions"),
  answerQuestion: (id: number, value: string) => request<{ ok: true }>(`/questions/${id}`, json("POST", { value })),
  dismissQuestion: (id: number) => request<{ ok: true }>(`/questions/${id}/dismiss`, json("POST")),
  prepareReport: (r: {
    description: string;
    screen: string;
    page: string;
    include_screen: boolean;
    include_diagnostics: boolean;
    blocks: boolean;
    meeting_id: number | null;
  }) => request<ProblemReport>("/report", json("POST", r)),
  /** One file as the request body (streamed to disk on the server, however big). */
  importFile: (file: File) =>
    request<FileImport>(
      `/import/file?name=${encodeURIComponent(file.name)}&modified=${file.lastModified || 0}`,
      { method: "POST", body: file, headers: { "Content-Type": "application/octet-stream" } },
    ),
  streamUrl: (id: number, draft: boolean) => {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    return `${proto}://${location.host}/api/meetings/${id}/stream?draft=${draft ? 1 : 0}&client=web`;
  },
  live: () => request<LiveRecording[]>("/live"),
  liveNotesPlan: () => request<LiveNotesPlan>("/live-notes"),
  stats: () => request<StatsInfo>("/stats"),
  lockdown: () => request<{ on: boolean }>("/lockdown"),
  vocabulary: () => request<{ learned: string[]; seeded: string[] }>("/vocabulary"),
  setMyNotes: (id: number, notes: MyNote[]) => request<MyNote[]>(`/meetings/${id}/my-notes`, json("PUT", { notes })),
  nativeRecorder: () => request<NativeRecorder>("/recorder"),
  ollama: () => request<OllamaStatus>("/ollama"),
  granolaNotes: () => request<{ notes: GranolaNote[] }>("/import/granola/notes"),
  importGranola: (note_ids: string[], keep_summary: boolean) => request<ImportJob>("/import/granola", json("POST", { note_ids, keep_summary })),
  granolaImportStatus: () => request<ImportJob>("/import/granola"),
  setImportSource: (name: string, auto: boolean, start: SyncStart = "now") =>
    request<{ sources: ImportSource[] }>(`/import/sources/${name}`, json("PUT", { auto, start })),
  importConnections: () => request<Connections>("/import/connections"),
  disconnectImportSource: (name: string) => request<Connections>(`/import/sources/${name}`, json("DELETE")),
  scanFolders: () => request<WatchedFolders>("/import/folders/scan", json("POST", {})),
  watchFolder: (path: string, existing: boolean) => request<WatchedFolders>("/import/folders", json("POST", { path, existing })),
  unwatchFolder: (path: string) => request<WatchedFolders>(`/import/folders?path=${encodeURIComponent(path)}`, json("DELETE")),
  syncImportSource: (name: string) => request<{ sources: ImportSource[] }>(`/import/sources/${name}/sync`, json("POST", {})),
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
  checkForUpdate: () => request<{ ok: true }>("/recorder/check-update", json("POST")),
  goBack: () => request<{ ok: true }>("/recorder/go-back", json("POST")),
  unskipUpdate: () => request<{ ok: true }>("/recorder/unskip-update", json("POST")),
  setBetaUpdates: (on: boolean) => request<{ ok: true }>("/recorder/beta-updates", json("POST", { on })),
  setOpenAtLogin: (on: boolean) => request<{ ok: true }>("/recorder/open-at-login", json("POST", { on })),
  setCallReminders: (on: boolean) => request<{ ok: true }>("/recorder/call-reminders", json("POST", { on })),
  soundCheck: () => request<{ ok: true }>("/recorder/sound-check", json("POST")),
  markLive: (id: number, note = "") => request<Bookmark>(`/meetings/${id}/mark`, json("POST", { note })),
  stopLive: (id: number) => request<{ ok: true }>(`/meetings/${id}/stop`, json("POST")),
  audioUrl: (id: number) => `/api/meetings/${id}/audio`,
  renameMeeting: (id: number, title: string) => request<{ ok: true }>(`/meetings/${id}`, json("PATCH", { title })),
  /** Moves a meeting to when it really happened (an ISO time). */
  setMeetingDate: (id: number, createdAt: string) =>
    request<{ ok: true }>(`/meetings/${id}`, json("PATCH", { created_at: createdAt })),
  /** The AI's guess at when a meeting happened, from what was said in it (nothing changes). */
  guessDate: (id: number) => request<DateGuess>(`/meetings/${id}/guess-date`, json("POST")),
  setSpeakers: (id: number, names: Record<string, string>) =>
    request<Record<string, string>>(`/meetings/${id}/speakers`, json("PUT", names)),
  setBookmarks: (id: number, bookmarks: Bookmark[]) =>
    request<Bookmark[]>(`/meetings/${id}/bookmarks`, json("PUT", { bookmarks })),
  confirm: (id: number) => request<{ ok: true }>(`/meetings/${id}/confirm`, json("POST")),
  retry: (id: number) => request<{ ok: true }>(`/meetings/${id}/retry`, json("POST")),
  versions: (id: number) =>
    request<{ versions: TranscriptVersion[]; trial: Trial | null }>(`/meetings/${id}/versions`),
  startTrial: (
    id: number,
    body: {
      engine: TranscribeEngine;
      model?: string;
      apply?: boolean;
      summarize?: boolean;
      mix_tracks?: boolean;
      /** an economy model only: send as a half-price batch job, answers back later */
      batch?: boolean;
    },
  ) => request<Trial & { batch?: boolean; submitted?: number; cost?: number }>(`/meetings/${id}/trial`, json("POST", body)),
  applyVersion: (id: number, versionId: number, summarize = false) =>
    request<{ ok: true }>(`/meetings/${id}/versions/${versionId}/apply`, json("POST", { summarize })),
  compareVersions: (id: number, a: number, b: number) =>
    request<Comparison>(`/meetings/${id}/compare?a=${a}&b=${b}`),
  history: (id: number) => request<HistoryEntry[]>(`/meetings/${id}/history`),
  /** engine: use this one just this time (Settings' engine otherwise) */
  retranscribe: (id: number, engine?: "local" | "remote" | "gemini") =>
    request<{ ok: true }>(`/meetings/${id}/retranscribe`, json("POST", engine ? { engine } : undefined)),
  preflight: (choice: AIChoice) => request<Preflight>("/preflight", json("POST", choice)),
  /** Transcribe (if needed) and write notes with this AI; `remember` makes it the default too. */
  process: (choice: AIChoice & { template?: string; remember?: boolean }) =>
    request<{ queued: number }>("/process", json("POST", choice)),
  summarize: (id: number, provider: Provider, template?: string) =>
    request<{ ok: true }>(`/meetings/${id}/summarize`, json("POST", { provider, template })),
  askMeeting: (id: number, question: string) => request<QA>(`/meetings/${id}/ask`, json("POST", { question })),
  clearQuestions: (id: number) => request<void>(`/meetings/${id}/ask`, { method: "DELETE" }),
  catchUpPlan: (id: number) => request<CatchUpPlan>(`/meetings/${id}/catch-up`),
  catchUp: (id: number, kind: CatchUpKind, question = "") =>
    request<CatchUpEntry>(`/meetings/${id}/catch-up`, json("POST", { kind, question })),
  askAll: (question: string, workspace_id: number | null = null) =>
    request<{ answer: string; provider: string; sources: { id: number; title: string; created_at: string }[] }>(
      "/ask",
      json("POST", { question, workspace_id }),
    ),
  tasks: () => request<TaskWithMeeting[]>("/tasks"),
  setTaskDone: (id: number, done: boolean) => request<{ ok: true }>(`/tasks/${id}`, json("PATCH", { done })),
  exportNow: (id: number) => request<{ paths: string[] }>(`/meetings/${id}/export`, json("POST")),
  deleteAudio: (id: number) => request<void>(`/meetings/${id}/audio`, { method: "DELETE" }),
  deleteMeeting: (id: number) => request<void>(`/meetings/${id}`, { method: "DELETE" }),
};
