/**
 * Words that fit the computer Trailmix runs on: "this Mac" and the menu bar on a Mac, "this PC" and the system
 * tray on Windows, plain "this computer" when it isn't known. Most of what the app says about "this computer" is
 * about the machine running the server (where the models run, where recordings are kept), so the server's own
 * OS (/api/health) decides, then the native recorder's check-in (same machine in the desktop apps), then, while
 * neither has answered and the page comes from this machine, the browser's own guess.
 *
 * To see another platform's wording: localStorage.setItem("trailmix.platform", "windows") and reload.
 */

export type OS = "macos" | "windows" | "linux" | "unknown";

export interface PlatformWords {
  os: OS;
  /** "macOS", "Windows", "Linux"; "the system" when unknown */
  osName: string;
  /** "Mac", "PC", "computer" */
  computer: string;
  /** "this Mac", "this PC", "this computer" */
  thisComputer: string;
  /** "your Mac", "your PC", "your computer" */
  yourComputer: string;
  /** where the recorder's icon lives: "menu bar", "system tray", "tray" */
  trayName: string;
  /** "menu bar recorder", "tray recorder" */
  recorderName: string;
  /** "menu bar helper", "tray app" */
  helperName: string;
  /** where to find the recorder's icon: "at the top of the screen", "in the system tray, by the clock" */
  trayWhere: string;
  /** "Finder", "File Explorer", "your file manager" */
  fileManager: string;
  /** what to open to start Trailmix: "Trailmix.app", "Trailmix" */
  appName: string;
}

const WORDS: Record<OS, PlatformWords> = {
  macos: {
    os: "macos",
    osName: "macOS",
    computer: "Mac",
    thisComputer: "this Mac",
    yourComputer: "your Mac",
    trayName: "menu bar",
    recorderName: "menu bar recorder",
    helperName: "menu bar helper",
    trayWhere: "at the top of the screen",
    fileManager: "Finder",
    appName: "Trailmix.app",
  },
  windows: {
    os: "windows",
    osName: "Windows",
    computer: "PC",
    thisComputer: "this PC",
    yourComputer: "your PC",
    trayName: "system tray",
    recorderName: "tray recorder",
    helperName: "tray app",
    trayWhere: "in the system tray, by the clock",
    fileManager: "File Explorer",
    appName: "Trailmix",
  },
  linux: {
    os: "linux",
    osName: "Linux",
    computer: "computer",
    thisComputer: "this computer",
    yourComputer: "your computer",
    trayName: "tray",
    recorderName: "tray recorder",
    helperName: "tray app",
    trayWhere: "in the tray",
    fileManager: "your file manager",
    appName: "Trailmix",
  },
  unknown: {
    os: "unknown",
    osName: "the system",
    computer: "computer",
    thisComputer: "this computer",
    yourComputer: "your computer",
    trayName: "tray",
    recorderName: "tray recorder",
    helperName: "tray app",
    trayWhere: "in the menu bar or system tray",
    fileManager: "your file manager",
    appName: "Trailmix",
  },
};

/** "macos-arm64", "windows-x64", "darwin", "win32", "Linux"… → which OS that is. */
export const parseOS = (p: string | null | undefined): OS => {
  const s = (p ?? "").toLowerCase();
  if (s.startsWith("mac") || s.startsWith("darwin")) return "macos";
  if (s.startsWith("win")) return "windows";
  if (s.startsWith("linux")) return "linux";
  return "unknown";
};

const override = (): OS => {
  try {
    return parseOS(localStorage.getItem("trailmix.platform"));
  } catch {
    return "unknown";
  }
};

/** The browser's own OS: right about this computer only when the page comes from this machine. */
const browserOS = (): OS => {
  if (typeof navigator === "undefined" || typeof location === "undefined") return "unknown";
  if (!["localhost", "127.0.0.1", "[::1]"].includes(location.hostname)) return "unknown";
  return /Mac/.test(navigator.platform) ? "macos" : /Win/.test(navigator.platform) ? "windows" : /Linux/.test(navigator.platform) ? "linux" : "unknown";
};

let serverOS: OS = "unknown";
let recorderOS: OS = "unknown";
let localEngine: string | null = null;

/** The server's OS and its local speech engine ("MLX", "whisper.cpp"), from /api/health. */
export const setServerPlatform = (p: string | null | undefined, engine?: string | null) => {
  serverOS = parseOS(p);
  localEngine = engine ?? null;
};

/** The native recorder's platform, from its check-in. */
export const setRecorderPlatform = (p: string | null | undefined) => {
  recorderOS = parseOS(p);
};

/** Which OS Trailmix is running on, as best known. */
export const currentOS = (): OS => {
  for (const os of [override(), serverOS, recorderOS, browserOS()]) if (os !== "unknown") return os;
  return "unknown";
};

/** The words for this computer (or for `os`). */
export const platformWords = (os: OS = currentOS()): PlatformWords => WORDS[os];

/**
 * The words for the computer a native recorder runs on (its check-in's platform, which can differ from the
 * server's), for what happens there: its permission switches, its chimes. The localStorage override still wins.
 */
export const recorderWords = (platform: string | null | undefined): PlatformWords => {
  const os = override();
  if (os !== "unknown") return WORDS[os];
  const own = parseOS(platform);
  return own !== "unknown" ? WORDS[own] : platformWords();
};

/** "this Mac" → "This Mac". */
export const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

/** The local speech engine as a choice: "This Mac (MLX Whisper)" (`long`) or "This Mac (MLX)", "This PC (whisper.cpp)". */
export const localEngineLabel = (long = false) => {
  const w = platformWords();
  const engine = localEngine ?? (w.os === "macos" ? "MLX" : "whisper.cpp");
  return `${cap(w.thisComputer)} (${engine}${long && engine === "MLX" ? " Whisper" : ""})`;
};

/** Is the keyboard in front of you a Mac's? (For shortcuts: that's the browser's machine, not the server's.) */
export const macKeyboard = () => typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform);
