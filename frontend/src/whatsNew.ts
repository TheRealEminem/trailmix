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
