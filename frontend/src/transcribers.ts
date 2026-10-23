// Where recordings can be transcribed besides this Mac: one list for setup and Settings → Transcription.
// Google uses its own engine (gemini); the rest are OpenAI-compatible /audio/transcriptions endpoints (remote).

export interface Transcriber {
  id: "gemini" | "openai" | "groq" | "server";
  name: string;
  /** For a tight row of choices. */
  short: string;
  engine: "gemini" | "remote";
  /** Endpoint for a remote engine; empty for your own server, whose address you type. */
  url: string;
  model: string;
  live: string;
  /** One line for a tooltip in Settings. */
  hint: string;
  /** What it does and costs, said in setup. */
  about: string;
  keyUrl?: string;
}

export const TRANSCRIBERS: Transcriber[] = [
  {
    id: "gemini",
    name: "Google Gemini",
    short: "Google",
    engine: "gemini",
    url: "",
    model: "",
    live: "",
    hint: "Cloud; tells the individual people apart.",
    about:
      "Tells the individual people apart; the transcript arrives a few minutes after you stop. A free key works, but it's rate-limited, so long meetings take a bit longer. The key also writes your notes if you pick Gemini for summaries.",
    keyUrl: "https://aistudio.google.com/apikey",
  },
  {
    id: "openai",
    name: "OpenAI",
    short: "OpenAI",
    engine: "remote",
    url: "https://api.openai.com/v1",
    model: "whisper-1",
    live: "whisper-1",
    hint: "Cloud; billed per minute.",
    about:
      "Whisper in the cloud, billed per minute (about $0.36 an hour; a call's two sides are sent separately, so double that). Labels you and the other side of a call. The key also writes your notes if you pick OpenAI for summaries.",
    keyUrl: "https://platform.openai.com/api-keys",
  },
  {
    id: "groq",
    name: "Groq",
    short: "Groq",
    engine: "remote",
    url: "https://api.groq.com/openai/v1",
    model: "whisper-large-v3-turbo",
    live: "whisper-large-v3-turbo",
    hint: "Cloud; very fast.",
    about: "Whisper in the cloud, very fast and cheap. Labels you and the other side of a call.",
    keyUrl: "https://console.groq.com/keys",
  },
  {
    id: "server",
    name: "Your own server",
    short: "Your server",
    engine: "remote",
    url: "",
    model: "large",
    live: "live",
    hint: "Your own Mac: run ./trailmix worker on it, then enter its address. Or any OpenAI-compatible server.",
    about:
      "A Mac running ./trailmix worker, or any server with an OpenAI-compatible /audio/transcriptions endpoint (speaches, whisper.cpp). Recordings go only there.",
  },
];

/** The entry a saved setup matches: Google, a known cloud by its address, else your own server. */
export function transcriberFor(engine: string, url: string): Transcriber | undefined {
  if (engine === "gemini") return TRANSCRIBERS[0];
  if (engine !== "remote") return undefined;
  return TRANSCRIBERS.find((t) => t.engine === "remote" && t.url && t.url === url.replace(/\/+$/, "")) ?? TRANSCRIBERS[3];
}
