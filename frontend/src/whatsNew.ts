/**
 * "What's new", shown once after an update (WhatsNew.tsx): the changes since the version you had, in plain words.
 * Add an entry with each release that has something worth telling people, newest first.
 */
export interface Release {
  version: string;
  items: { title: string; body: string; where?: string }[];
}

export const RELEASES: Release[] = [
  {
    version: "0.15.0",
    items: [
      {
        title: "Choose the AI before it runs",
        body: "Before an import is processed, or notes are written again, Trailmix shows what each choice would take: how many passes a long transcript needs on this Mac, how much memory, or what the cloud would cost. Pick one for this time, or make it your default. The switch under your workspace changes the default in a click.",
        where: "Sidebar → AI · Settings → Automatic mode",
      },
      {
        title: "Who said what, with Google Gemini",
        body: "Choose Google Gemini for transcription and every voice gets its own label, even in a room or a call with several people. Live captions appear as you talk, and the full transcript follows when you stop. Your recording is sent to Google, so Lockdown mode turns it off.",
        where: "Settings → Transcription",
      },
      {
        title: "Pick the engine before you record",
        body: "Switch between this Mac and Google right on the recording card.",
        where: "Home",
      },
      {
        title: "Try another model, compare, switch back",
        body: "Every transcript of a meeting is kept. Transcribe the saved audio again with another engine or model, see the two side by side word by word, and use whichever you like.",
        where: "A meeting → Transcript → Versions",
      },
      {
        title: "Better models, for speech too",
        body: "Trailmix now rates speech models like it rates note-writing ones, and offers to redo old transcripts and notes with a better one. It shows what it will cost first and does a few meetings at a time. Notes can be rewritten at half price as a Google batch job that finishes in the background.",
        where: "Settings → Better models",
      },
      {
        title: "A history of every change",
        body: "Titles, notes, transcripts, names and more are recorded with what they were, what they became and why. Exports keep it too.",
        where: "A meeting → Transcript → Versions",
      },
    ],
  },
  {
    version: "0.14.1",
    items: [
      {
        title: "Change a meeting's date",
        body: "Click the date at the top of a meeting to move it to when it really happened. Not sure when that was? “Find the date” has the AI look for clues in what was said.",
      },
      {
        title: "Captions from YouTube and Google Drive",
        body: "Transcripts with the time on its own line above the words now import cleanly, joined into one paragraph per speaker, and the date box no longer defaults to tomorrow.",
        where: "Import",
      },
    ],
  },
  {
    version: "0.14.0",
    items: [
      {
        title: "A reminder to record",
        body: "When a Zoom, Teams, FaceTime, Slack or browser call starts and Trailmix isn't recording, a small card asks if you'd like to. When a recorded call ends, it asks whether to stop.",
        where: "Settings → App",
      },
      {
        title: "Your own notes",
        body: "Type notes and paste links while you record. They're worked into the meeting's notes, links and all, and you can edit them afterwards under “Your notes”.",
      },
      {
        title: "A note style for each workspace",
        body: "Board minutes for a committee, sales notes for work: pick a style per workspace. There's a new “Board or committee minutes” style too.",
        where: "Settings → Workspaces",
      },
      {
        title: "Names spelled your way",
        body: "Trailmix now knows the names in your workspaces and the people you've named, and you can add your own. It never asks to “correct” them.",
        where: "Settings → Automatic mode",
      },
      {
        title: "Lockdown mode",
        body: "For maximum privacy: nothing leaves your computer. Settings explains exactly what you give up.",
        where: "Settings → Privacy",
      },
    ],
  },
  {
    version: "0.13.0",
    items: [
      {
        title: "Go back if an update lets you down",
        body: "One button puts the previous version back, and your meetings stay. Trailmix also backs up its database before each new version.",
        where: "Settings → Updates",
      },
      {
        title: "Anonymous usage stats",
        body: "A few anonymous numbers a day help decide which computers to support. Never anything from your meetings, and off with one switch.",
        where: "Settings → Privacy",
      },
    ],
  },
  {
    version: "0.12.0",
    items: [
      {
        title: "Notes appear as they're written",
        body: "No more waiting for the whole summary: it fills in line by line a few seconds after the AI starts.",
      },
      {
        title: "Notes during the meeting",
        body: "Optionally, the notes AI jots notes every ten minutes while you record, so a long meeting's notes are ready sooner.",
        where: "Settings → Automatic mode",
      },
    ],
  },
];

function parts(v: string): number[] {
  return v.split(".").map((x) => parseInt(x, 10) || 0);
}

export function newer(a: string, b: string): boolean {
  const x = parts(a);
  const y = parts(b);
  for (let i = 0; i < Math.max(x.length, y.length); i++) {
    if ((x[i] ?? 0) !== (y[i] ?? 0)) return (x[i] ?? 0) > (y[i] ?? 0);
  }
  return false;
}

/** What changed after `seen`, up to and including `current`. */
export function since(seen: string, current: string): Release[] {
  return RELEASES.filter((r) => newer(r.version, seen) && !newer(r.version, current));
}
