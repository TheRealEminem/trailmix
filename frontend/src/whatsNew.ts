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
    version: "0.18.1",
    items: [
      {
        title: "A better app icon",
        body: "Trailmix's icon now fills its whole tile like other Mac apps, instead of sitting small on a gray plate.",
      },
    ],
  },
  {
    version: "0.18.0",
    items: [
      {
        title: "Slides, PDFs and photos with your notes",
        body: "Drop a deck, a PDF, a photo of the whiteboard or a Google Slides link onto a meeting's notes, even while you record, so everything from a meeting is in one place.",
        where: "A meeting → Your notes → Attachments",
      },
      {
        title: "Big files made small",
        body: "Add a huge PDF, PowerPoint or iPhone photo and Trailmix offers to shrink it first, showing how small it will get: photos are resized, PDF pages flattened, and the pictures in slides recompressed. Or have it always shrink them.",
        where: "Settings → Attachments",
      },
      {
        title: "Attachments you can search",
        body: "Tick \"Add to searchable notes\" on an attachment and its text becomes searchable and part of what Ask knows, with a short digest in its own part under the summary. It never adds tasks. It's off unless you tick it, and you choose whether it's read only on this computer or by your summary provider.",
      },
    ],
  },
  {
    version: "0.17.0",
    items: [
      {
        title: "A blank page for your notes while you record",
        body: "While a meeting records, Trailmix is now a page you type on: # makes a heading, - a list, and links stay links. The timer, Stop, the live transcript and Catch me up sit in a bar at the bottom, out of the way until you open it.",
        where: "Start a recording",
      },
      {
        title: "Notes built around yours",
        body: "When you've typed notes, the meeting's notes keep your headings as their sections, in your order, and fill in what was said under each of your lines.",
      },
      {
        title: "Catch me up from the menu bar",
        body: "\"What did I miss?\", \"Explain that\" or your own question, answered right in the menu bar while you record, without bringing up the window.",
        where: "Menu bar → Catch me up",
      },
      {
        title: "Transcribe with any service",
        body: "Pick Google, OpenAI, Groq or your own server for transcription, with your own key.",
        where: "Settings → Transcription",
      },
      {
        title: "Less disk space for old recordings",
        body: "Trailmix now clears its temporary playback copies of old recordings to save disk space.",
      },
    ],
  },
  {
    version: "0.16.1",
    items: [
      {
        title: "Setup, one step at a time",
        body: "The first-run checklist is now a short guided setup, one question per screen: your name, where you are, how recordings become text, who writes your notes, a sound check. Every step can be skipped, and you can go through it again whenever you like.",
        where: "Settings → App → Run setup again",
      },
    ],
  },
  {
    version: "0.16.0",
    items: [
      {
        title: "Your meetings come in by themselves",
        body: "Connect Granola, Fathom or Fireflies once and new meetings arrive with their transcripts, notes and tasks, checked every 15 minutes. A meeting Trailmix recorded too is merged into one.",
        where: "Import → Connected",
      },
      {
        title: "Watched folders",
        body: "Point Trailmix at a folder, like where Zoom, Meet or Teams recordings land through Drive or OneDrive, and new recordings and transcripts saved there come in by themselves.",
        where: "Import → Connected → Connect",
      },
      {
        title: "One Import page",
        body: "Everything that brings meetings in is in one place: what's connected, as one list, and a single spot for importing files, Trailmix exports and pasted transcripts once.",
        where: "Import",
      },
      {
        title: "Recording laws where you are",
        body: "Some places require everyone on a call to agree before it's recorded or transcribed. Pick your country or US state for a short summary of the rules there, with its sources. It's general information, and nothing changes unless you choose to apply a suggestion.",
        where: "Settings → Privacy → Recording laws",
      },
      {
        title: "Choose how long recordings are kept",
        body: "Keep each recording for 30 days (as before), 7 days, until it's transcribed, or forever. The transcript and notes always stay.",
        where: "Settings → Privacy → Keep recordings",
      },
    ],
  },
  {
    version: "0.15.2",
    items: [
      {
        title: "Catch me up, while the meeting is still going",
        body: "Zoned out or got pulled away? While you're recording, ask \"What did I miss?\" for the last five minutes, \"Explain that\" for the last thing said in plainer words, \"What's been decided?\", or a question of your own. Answers come from the meeting so far, with times you can click to find that moment in the live transcript. Each one shows about what it costs before you ask. On a small AI on this Mac it stays basic and private.",
        where: "Home, while recording",
      },
    ],
  },
  {
    version: "0.15.1",
    items: [
      {
        title: "One meeting, not two",
        body: "When Granola was taking notes while Trailmix recorded, importing Granola's copy no longer leaves the meeting there twice. Trailmix spots it by the words both transcripts share and merges them: you keep the recording, your flags and your notes, with Granola's transcript (lined up with the recording) and Granola's notes. Trailmix's own transcript stays under Versions, and Undo merge splits them again. With a Granola API key, Granola's notes are brought in by themselves after a recording.",
        where: "A meeting · Settings → Automatic mode",
      },
      {
        title: "Already have duplicates?",
        body: "Find duplicates looks through every meeting for ones that are there twice and merges them in one click.",
        where: "Settings → Automatic mode → Find duplicates",
      },
    ],
  },
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
