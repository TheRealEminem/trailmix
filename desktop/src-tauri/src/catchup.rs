//! Catch me up and the live draft, from the tray (the Mac menu's recording panel in MenuView.swift: DraftPreview,
//! the moments flagged, and CatchUpSection; the catch-up state in HelperModel.swift). Tray menus can't hold any of
//! that, so it's a card of its own (catchup_card.rs on Windows, linux/catchup_card.rs), drawn from the `View`
//! made here. Which recording the questions go to (this app's, or the one the Trailmix window is capturing), who
//! would answer and about what it costs (GET /api/meetings/{id}/catch-up, backend/catch_up.py), the answers so
//! far, the question being answered, and what went wrong all live here, so an answer still lands when the card was
//! closed while it was coming (the card comes back with it).

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, OnceLock};
use std::thread;
use std::time::Duration;

use serde::Deserialize;

use crate::catchup_card as card;
use crate::client::DraftLine;
use crate::model::{Model, Phase, Snapshot};

/// The quick questions, as the Mac menu lists them (the server's own labels win, from the plan's `kinds`).
pub const QUICK: [(&str, &str); 3] = [("missed", "What did I miss?"), ("explain", "Explain that"), ("decided", "What's been decided?")];
pub const ASK_PLACEHOLDER: &str = "Ask anything about the meeting so far…";
pub const READING: &str = "Reading the meeting so far…";
pub const DRAFT_HINT: &str = "The live draft appears here after a few seconds of speech.";
/// The live draft lines the card shows (the newest last).
const DRAFT_LINES: usize = 3;

/// A question asked while the meeting is recording, and its answer (CatchUpEntry in ServerClient.swift). `a` is
/// markdown with timestamps like [12:34].
#[derive(Clone, Debug, Default, Deserialize, PartialEq)]
pub struct Entry {
    #[serde(default)]
    pub q: String,
    #[serde(default)]
    pub a: String,
    #[serde(default)]
    pub label: String,
    #[serde(default)]
    pub kind: Option<String>,
    /// Seconds into the recording when it was asked.
    #[serde(default)]
    pub t: Option<f64>,
}

/// Who would answer right now, and about what it costs (CatchUpPlan in ServerClient.swift).
#[derive(Clone, Debug, Default, Deserialize, PartialEq)]
pub struct Plan {
    #[serde(default)]
    pub ready: bool,
    /// Why it can't be asked yet.
    #[serde(default)]
    pub reason: Option<String>,
    #[serde(default)]
    pub label: Option<String>,
    #[serde(default)]
    pub local: bool,
    /// Dollars for each quick question (and a typed one): 0 on this computer, None where the price isn't known.
    #[serde(default)]
    pub costs: HashMap<String, Option<f64>>,
    #[serde(default)]
    pub note: Option<String>,
    #[serde(default)]
    pub warning: Option<String>,
    #[serde(default)]
    pub kinds: HashMap<String, String>,
    /// Quick questions this AI isn't good enough for, with why.
    #[serde(default)]
    pub off: HashMap<String, String>,
    #[serde(default)]
    pub history: Vec<Entry>,
}

/// What the card can be clicked on.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Target {
    Close,
    /// "Catch me up ▸": shows or hides the questions.
    Toggle,
    /// One of the quick questions, by its place in QUICK.
    Quick(usize),
    /// "Open in Trailmix".
    Open,
    /// Puts away the error shown.
    DismissError,
}

#[derive(Default)]
struct State {
    /// The recording the questions go to.
    meeting: Option<i64>,
    plan: Option<Plan>,
    entries: Vec<Entry>,
    /// The question being answered.
    pending: Option<String>,
    error: Option<String>,
    /// Whether the questions show (the Mac's "Catch me up ▾"), or only the live draft.
    expanded: bool,
    /// The newest live draft of a recording the Trailmix window is capturing, while the card is up.
    other_drafts: Option<(i64, Vec<DraftLine>)>,
}

pub struct CatchUp {
    model: Arc<Model>,
    state: Mutex<State>,
    /// Opens a meeting in the Trailmix window (the app sets it; the self-test has no window to open).
    opener: Mutex<Option<Box<dyn Fn(i64) + Send + Sync>>>,
    watching: AtomicBool,
}

static SHARED: OnceLock<Arc<CatchUp>> = OnceLock::new();

/// Sets catch-up up for this model, from now on following what it records. Once per run.
pub fn begin(model: &Arc<Model>) -> Arc<CatchUp> {
    let made = SHARED.get_or_init(|| {
        Arc::new(CatchUp { model: model.clone(), state: Mutex::new(State::default()), opener: Mutex::new(None), watching: AtomicBool::new(false) })
    });
    let weak = Arc::downgrade(made);
    model.observe(move || {
        if let Some(c) = weak.upgrade() {
            c.sync();
        }
    });
    made.clone()
}

pub fn shared() -> Option<Arc<CatchUp>> {
    SHARED.get().cloned()
}

/// The recording catch-up questions go to: this app's while it records, else one the Trailmix window is capturing.
pub fn live_meeting(s: &Snapshot) -> Option<i64> {
    match s.phase {
        Phase::Recording => s.meeting_id,
        Phase::Idle => s.other.as_ref().map(|o| o.id),
        _ => None,
    }
}

impl CatchUp {
    fn lock(&self) -> MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(|e| e.into_inner())
    }

    pub fn set_opener(&self, open: impl Fn(i64) + Send + Sync + 'static) {
        *self.opener.lock().unwrap_or_else(|e| e.into_inner()) = Some(Box::new(open));
    }

    /// The recording changed (or ended): forget the last one's questions; the card goes once nothing records.
    fn sync(self: &Arc<Self>) {
        let now = live_meeting(&self.model.snapshot());
        let changed = {
            let mut s = self.lock();
            if s.meeting == now {
                false
            } else {
                let expanded = s.expanded;
                *s = State { meeting: now, expanded, ..State::default() };
                true
            }
        };
        if now.is_none() {
            if card::is_open() {
                card::hide();
            }
            return;
        }
        if card::is_open() {
            if changed {
                self.load_in_background();
            }
            card::refresh();
        }
    }

    /// Opens the card (or brings it up to date), with the questions showing or only the live draft. It never takes
    /// the focus from the call. Nothing recording: a note says so.
    pub fn open(self: &Arc<Self>, expanded: bool) {
        self.sync();
        let meeting = {
            let mut s = self.lock();
            s.expanded = expanded;
            s.meeting
        };
        if meeting.is_none() {
            crate::card::note("Nothing is recording", "Catch me up and the live draft work while a meeting records.", 4);
            return;
        }
        card::show();
        self.load_in_background();
        self.watch();
    }

    pub fn toggle(&self) {
        {
            let mut s = self.lock();
            s.expanded = !s.expanded;
        }
        card::refresh();
    }

    pub fn dismiss_error(&self) {
        self.lock().error = None;
        card::refresh();
    }

    pub fn open_in_trailmix(&self) {
        let Some(id) = self.lock().meeting else { return };
        if let Some(open) = self.opener.lock().unwrap_or_else(|e| e.into_inner()).as_ref() {
            open(id);
        }
    }

    /// What a click on the card does.
    pub fn clicked(self: &Arc<Self>, target: Target) {
        match target {
            Target::Close => card::hide(),
            Target::Toggle => self.toggle(),
            Target::Quick(i) => {
                if let Some((kind, _)) = QUICK.get(i) {
                    self.ask(kind, "");
                }
            }
            Target::Open => self.open_in_trailmix(),
            Target::DismissError => self.dismiss_error(),
        }
    }

    fn load_in_background(self: &Arc<Self>) {
        let me = self.clone();
        thread::spawn(move || me.load());
    }

    /// Asks the server who would answer now (and for the answers so far).
    pub fn load(&self) {
        let Some(id) = self.lock().meeting else { return };
        let got = self.model.client().and_then(|c| c.catch_up_plan(id));
        {
            let mut s = self.lock();
            if s.meeting != Some(id) {
                return;
            }
            match got.map(serde_json::from_value::<Plan>) {
                Ok(Ok(plan)) => {
                    if plan.history.len() >= s.entries.len() {
                        s.entries = plan.history.clone();
                    }
                    s.plan = Some(plan);
                }
                Ok(Err(e)) => {
                    if s.plan.is_none() {
                        s.error = Some(format!("Trailmix's answer made no sense: {e}"));
                    }
                }
                Err(e) => {
                    if s.plan.is_none() {
                        s.error = Some(e.to_string());
                    }
                }
            }
        }
        card::refresh();
    }

    /// "missed", "explain", "decided", or "question" with your own. The answer shows in the card; if the card was
    /// closed meanwhile, it comes back with it (without taking the focus).
    pub fn ask(self: &Arc<Self>, kind: &str, question: &str) {
        let question = question.trim().to_string();
        if kind == "question" && question.is_empty() {
            return;
        }
        let id = {
            let mut s = self.lock();
            let Some(id) = s.meeting else { return };
            if s.pending.is_some() {
                return;
            }
            if let Some(plan) = &s.plan {
                if !plan.ready || plan.off.contains_key(kind) {
                    return;
                }
            }
            s.pending = Some(if kind == "question" { question.clone() } else { kind_label(s.plan.as_ref(), kind) });
            s.error = None;
            id
        };
        card::refresh();
        let me = self.clone();
        let kind = kind.to_string();
        thread::spawn(move || {
            let got = me.model.client().and_then(|c| c.catch_up(id, &kind, &question));
            {
                let mut s = me.lock();
                if s.meeting != Some(id) {
                    return;
                }
                s.pending = None;
                match got.map(serde_json::from_value::<Entry>) {
                    Ok(Ok(entry)) => s.entries.push(entry),
                    Ok(Err(e)) => s.error = Some(format!("Trailmix's answer made no sense: {e}")),
                    Err(e) => s.error = Some(e.to_string()),
                }
            }
            if card::is_open() {
                card::refresh();
            } else {
                card::show();
                me.watch();
            }
            me.load();
        });
    }

    /// While the card is up: its clock ticks, and a recording in the Trailmix window has its live draft fetched
    /// every few seconds (this app's own arrives as it's made).
    fn watch(self: &Arc<Self>) {
        if self.watching.swap(true, Ordering::SeqCst) {
            return;
        }
        let me = self.clone();
        thread::spawn(move || {
            let mut rounds: u64 = 0;
            while card::is_open() {
                thread::sleep(Duration::from_secs(1));
                rounds += 1;
                let snapshot = me.model.snapshot();
                if rounds % 3 == 1 && snapshot.phase == Phase::Idle {
                    if let Some(other) = &snapshot.other {
                        if let Ok(live) = me.model.client().and_then(|c| c.live()) {
                            if let Some(found) = live.into_iter().find(|r| r.id == other.id) {
                                me.lock().other_drafts = Some((found.id, found.drafts));
                            }
                        }
                    }
                }
                card::refresh();
            }
            me.watching.store(false, Ordering::SeqCst);
        });
    }

    /// What the card shows now.
    pub fn view(&self) -> View {
        let snapshot = self.model.snapshot();
        let prefs = self.model.prefs();
        let s = self.lock();
        make_view(&snapshot, prefs.live_draft, &s, this_computer())
    }

    pub fn pending(&self) -> Option<String> {
        self.lock().pending.clone()
    }

    pub fn plan(&self) -> Option<Plan> {
        self.lock().plan.clone()
    }

    pub fn last_answer(&self) -> Option<Entry> {
        self.lock().entries.last().cloned()
    }

    pub fn error(&self) -> Option<String> {
        self.lock().error.clone()
    }
}

#[cfg(windows)]
fn this_computer() -> &'static str {
    "this PC"
}

#[cfg(not(windows))]
fn this_computer() -> &'static str {
    "this computer"
}

fn kind_label(plan: Option<&Plan>, kind: &str) -> String {
    if let Some(label) = plan.and_then(|p| p.kinds.get(kind)) {
        return label.clone();
    }
    QUICK.iter().find(|(k, _)| *k == kind).map(|(_, l)| l.to_string()).unwrap_or_else(|| kind.to_string())
}

// MARK: What the card shows

#[derive(Clone, Debug, Default, PartialEq)]
pub struct DraftView {
    pub speaker: Option<String>,
    pub text: String,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct QuickView {
    pub label: String,
    /// "~0.4¢" for a cloud AI, where the price is known.
    pub cost: Option<String>,
    pub enabled: bool,
    /// This is the one being answered.
    pub busy: bool,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct AnswerView {
    pub q: String,
    /// "asked at 10:42" (into the recording).
    pub asked_at: Option<String>,
    pub lines: Vec<Line>,
    /// Who answered, when it isn't the AI named at the bottom (another one stepped in).
    pub label: Option<String>,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct View {
    /// "Recording", "Recording in Trailmix", "Recording elsewhere".
    pub title: String,
    pub clock: String,
    pub draft: Vec<DraftView>,
    /// Instead of the lines while there are none.
    pub draft_hint: Option<String>,
    /// "2 moments flagged".
    pub marks: Option<String>,
    pub expanded: bool,
    /// Why nothing can be asked (no AI set up, no live transcript): then there are no questions to ask.
    pub reason: Option<String>,
    pub quick: Vec<QuickView>,
    /// (question, why it's off) for the quick questions this AI can't answer well.
    pub off: Vec<(String, String)>,
    /// The box to type a question in shows, and takes typing now.
    pub ask_box: bool,
    pub ask_enabled: bool,
    pub note: Option<String>,
    pub warning: Option<String>,
    pub error: Option<String>,
    /// The question being answered (with "Reading the meeting so far…").
    pub pending: Option<String>,
    pub answer: Option<AnswerView>,
    /// Who answers: "Gemini 2.5 Flash", or "qwen3:8b · on this PC, free".
    pub footer: Option<String>,
    pub can_open: bool,
}

impl View {
    /// Everything it says, a line each (for the self-test, and to read in a log).
    pub fn text(&self) -> String {
        let mut out = vec![format!("{} {}", self.title, self.clock)];
        for d in &self.draft {
            out.push(match &d.speaker {
                Some(s) => format!("{s}: {}", d.text),
                None => d.text.clone(),
            });
        }
        out.extend(self.draft_hint.clone());
        out.extend(self.marks.clone());
        out.push(format!("Catch me up {}", if self.expanded { "▾" } else { "▸" }));
        if self.expanded {
            out.extend(self.reason.clone());
            for q in &self.quick {
                out.push(match &q.cost {
                    Some(c) => format!("{} {c}", q.label),
                    None => q.label.clone(),
                });
            }
            for (q, why) in &self.off {
                out.push(format!("{q} {why}"));
            }
            if self.ask_box {
                out.push(ASK_PLACEHOLDER.to_string());
            }
            out.extend(self.note.clone());
            out.extend(self.warning.clone());
            out.extend(self.error.clone());
            if let Some(p) = &self.pending {
                out.push(p.clone());
                out.push(READING.to_string());
            } else if let Some(a) = &self.answer {
                out.push(match &a.asked_at {
                    Some(at) => format!("{} {at}", a.q),
                    None => a.q.clone(),
                });
                for line in &a.lines {
                    out.push(line.plain());
                }
                out.extend(a.label.clone());
            }
            out.extend(self.footer.clone());
            if self.can_open {
                out.push("Open in Trailmix".into());
            }
        }
        out.join("\n")
    }
}

fn make_view(snapshot: &Snapshot, live_draft: bool, s: &State, this_computer: &str) -> View {
    let own = snapshot.phase == Phase::Recording;
    let other = if own { None } else { snapshot.other.as_ref() };
    let title = match other {
        None => "Recording".to_string(),
        Some(o) if o.client == "web" => "Recording in Trailmix".to_string(),
        Some(_) => "Recording elsewhere".to_string(),
    };
    let (lines, enabled): (Vec<DraftLine>, bool) = match other {
        None => (snapshot.drafts.clone(), live_draft),
        Some(o) => {
            let fetched = s.other_drafts.as_ref().filter(|(id, _)| *id == o.id).map(|(_, d)| d.clone());
            (fetched.unwrap_or_else(|| o.drafts.clone()), o.draft)
        }
    };
    let mut draft: Vec<DraftView> = lines
        .iter()
        .rev()
        .filter(|d| !d.text.trim().is_empty())
        .take(DRAFT_LINES)
        .map(|d| DraftView { speaker: d.speaker.clone(), text: d.text.trim().to_string() })
        .collect();
    draft.reverse();
    let draft_hint = (draft.is_empty() && enabled).then(|| DRAFT_HINT.to_string());
    let marks = (own && !snapshot.marks.is_empty())
        .then(|| format!("{} moment{} flagged", snapshot.marks.len(), if snapshot.marks.len() == 1 { "" } else { "s" }));

    let plan = s.plan.as_ref();
    let mut view = View {
        title,
        clock: clock(snapshot.elapsed.map(|e| e.as_secs_f64()).unwrap_or(0.0)),
        draft,
        draft_hint,
        marks,
        expanded: s.expanded,
        can_open: s.meeting.is_some(),
        ..View::default()
    };
    match plan {
        Some(p) if !p.ready => {
            view.reason = Some(p.reason.clone().unwrap_or_else(|| "Catch me up can't answer for this recording.".into()));
        }
        _ => {
            for (kind, default) in QUICK {
                let label = plan.and_then(|p| p.kinds.get(kind)).cloned().unwrap_or_else(|| default.to_string());
                let off = plan.and_then(|p| p.off.get(kind));
                let cost = plan.filter(|p| !p.local).and_then(|p| money(p.costs.get(kind).copied().flatten(), false)).map(|c| format!("~{c}"));
                view.quick.push(QuickView {
                    busy: s.pending.as_deref() == Some(label.as_str()),
                    enabled: s.pending.is_none() && off.is_none(),
                    label: label.clone(),
                    cost,
                });
                if let Some(why) = off {
                    view.off.push((label, why.clone()));
                }
            }
            view.ask_box = true;
            view.ask_enabled = s.pending.is_none();
        }
    }
    view.note = plan.and_then(|p| p.note.clone());
    view.warning = plan.and_then(|p| p.warning.clone());
    view.error = s.error.clone();
    view.pending = s.pending.clone();
    if view.pending.is_none() {
        view.answer = s.entries.last().map(|e| AnswerView {
            q: e.q.clone(),
            asked_at: e.t.map(|t| format!("asked at {}", clock(t))),
            lines: parse_answer(&e.a),
            label: (!e.label.is_empty() && Some(&e.label) != plan.and_then(|p| p.label.as_ref())).then(|| e.label.clone()),
        });
    }
    if let Some(p) = plan.filter(|p| p.ready) {
        if let Some(label) = &p.label {
            view.footer = Some(if p.local { format!("{label} · on {this_computer}, free") } else { label.clone() });
        }
    }
    view
}

/// "12:34", or "1:02:03" past an hour (Brand.swift's clock).
pub fn clock(seconds: f64) -> String {
    let s = seconds.max(0.0).floor() as u64;
    let (h, m, r) = (s / 3600, s % 3600 / 60, s % 60);
    if h > 0 {
        format!("{h}:{m:02}:{r:02}")
    } else {
        format!("{m}:{r:02}")
    }
}

/// About what one question costs: "free" on this computer, "0.4¢", "$0.012"; None where the price isn't known.
pub fn money(cost: Option<f64>, local: bool) -> Option<String> {
    if local {
        return Some("free".into());
    }
    let cost = cost?;
    if cost < 0.01 {
        return Some(if cost < 0.001 { format!("{:.2}¢", cost * 100.0) } else { format!("{:.1}¢", cost * 100.0) });
    }
    Some(format!("${cost:.3}"))
}

// MARK: Answers

/// A piece of a line: plain, bold, or a timestamp like [12:34] (picked out in the Mac's amber).
#[derive(Clone, Debug, PartialEq)]
pub struct Span {
    pub text: String,
    pub bold: bool,
    pub stamp: bool,
}

/// A line of an answer: "- point", "1. step", "## Heading" or plain text (AnswerLine in MenuView.swift).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Line {
    pub spans: Vec<Span>,
    pub bullet: Option<String>,
    pub indent: usize,
    pub heading: bool,
}

impl Line {
    pub fn plain(&self) -> String {
        let text: String = self.spans.iter().map(|s| s.text.as_str()).collect();
        match &self.bullet {
            Some(b) => format!("{}{b} {text}", "  ".repeat(self.indent)),
            None => text,
        }
    }
}

pub fn parse_answer(markdown: &str) -> Vec<Line> {
    markdown
        .lines()
        .filter_map(|raw| {
            let lead = raw.chars().take_while(|c| *c == ' ' || *c == '\t').count();
            let indent = lead / 2;
            let text = raw.trim();
            if text.is_empty() {
                return None;
            }
            for marker in ["- ", "* ", "+ ", "• "] {
                if let Some(rest) = text.strip_prefix(marker) {
                    return Some(Line { spans: spans(rest), bullet: Some("•".into()), indent, heading: false });
                }
            }
            let digits = text.chars().take_while(|c| c.is_ascii_digit()).count();
            if digits > 0 {
                let rest = &text[digits..];
                if let Some(after) = rest.strip_prefix(". ").or_else(|| rest.strip_prefix(") ")) {
                    let bullet = text[..digits + 1].to_string();
                    return Some(Line { spans: spans(after), bullet: Some(bullet), indent, heading: false });
                }
            }
            if text.starts_with('#') {
                let rest = text.trim_start_matches('#').trim();
                let mut spans = spans(rest);
                for s in &mut spans {
                    s.bold = true;
                }
                return Some(Line { spans, bullet: None, indent: 0, heading: true });
            }
            Some(Line { spans: spans(text), bullet: None, indent, heading: false })
        })
        .collect()
}

/// The length of a timestamp like "[12:34]" or "[1:02:03]" at the start of `text`, if there is one.
fn stamp_len(text: &str) -> Option<usize> {
    let inner = text.strip_prefix('[')?;
    let end = inner.find(']')?;
    let parts: Vec<&str> = inner[..end].split(':').collect();
    let shaped = match parts.as_slice() {
        [m, s] => (1..=2).contains(&m.len()) && s.len() == 2,
        [h, m, s] => !h.is_empty() && h.len() <= 2 && (1..=2).contains(&m.len()) && s.len() == 2,
        _ => false,
    };
    let digits = parts.iter().all(|p| p.chars().all(|c| c.is_ascii_digit()));
    (shaped && digits).then_some(end + 2)
}

/// Inline markdown, as far as a card shows it: **bold** (and __bold__), `code` without its ticks, links as their
/// text, and timestamps picked out.
fn spans(text: &str) -> Vec<Span> {
    let mut out: Vec<Span> = Vec::new();
    let mut bold = false;
    let mut current = String::new();
    let push = |out: &mut Vec<Span>, text: &mut String, bold: bool, stamp: bool| {
        if text.is_empty() {
            return;
        }
        let joins = !stamp && out.last().map(|last| last.bold == bold && !last.stamp).unwrap_or(false);
        if joins {
            if let Some(last) = out.last_mut() {
                last.text.push_str(text);
            }
        } else {
            out.push(Span { text: text.clone(), bold, stamp });
        }
        text.clear();
    };
    let mut i = 0;
    while i < text.len() {
        let rest = &text[i..];
        if rest.starts_with("**") || rest.starts_with("__") {
            push(&mut out, &mut current, bold, false);
            bold = !bold;
            i += 2;
            continue;
        }
        if rest.starts_with('`') {
            i += 1;
            continue;
        }
        if let Some(n) = stamp_len(rest) {
            push(&mut out, &mut current, bold, false);
            let mut stamp = rest[..n].to_string();
            push(&mut out, &mut stamp, bold, true);
            i += n;
            continue;
        }
        if rest.starts_with('[') {
            // [text](link): the text
            if let Some(close) = rest.find("](") {
                if let Some(end) = rest[close..].find(')') {
                    current.push_str(&rest[1..close]);
                    i += close + end + 1;
                    continue;
                }
            }
        }
        let c = rest.chars().next().unwrap_or(' ');
        current.push(c);
        i += c.len_utf8();
    }
    push(&mut out, &mut current, bold, false);
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::client::LiveRecording;
    use crate::model::Connection;

    fn recording() -> Snapshot {
        Snapshot {
            phase: Phase::Recording,
            connection: Connection::Online,
            other: None,
            meeting_id: Some(12),
            elapsed: Some(Duration::from_secs(754)),
            mic_name: "Mic".into(),
            capturing_meeting_audio: true,
            last_meeting_id: None,
            error: None,
            notice: None,
            starting_server: false,
            drafts: Vec::new(),
            marks: Vec::new(),
        }
    }

    fn line(start: f64, speaker: &str, text: &str) -> DraftLine {
        DraftLine { start, speaker: Some(speaker.into()), text: text.into() }
    }

    #[test]
    fn money_reads_as_on_the_mac() {
        assert_eq!(money(Some(0.0042), false).as_deref(), Some("0.4¢"));
        assert_eq!(money(Some(0.0004), false).as_deref(), Some("0.04¢"));
        assert_eq!(money(Some(0.0123), false).as_deref(), Some("$0.012"));
        assert_eq!(money(None, false), None);
        assert_eq!(money(Some(0.5), true).as_deref(), Some("free"));
        assert_eq!(clock(754.9), "12:34");
        assert_eq!(clock(3723.0), "1:02:03");
    }

    #[test]
    fn the_live_draft_shows_its_last_lines() {
        let s = State { meeting: Some(12), ..State::default() };
        let mut snap = recording();
        let view = make_view(&snap, true, &s, "this PC");
        assert_eq!(view.title, "Recording");
        assert_eq!(view.clock, "12:34");
        assert_eq!(view.draft_hint.as_deref(), Some(DRAFT_HINT));
        assert!(make_view(&snap, false, &s, "this PC").draft_hint.is_none());
        snap.drafts = (0..5).map(|i| line(i as f64, if i % 2 == 0 { "You" } else { "Them" }, &format!("line {i}"))).collect();
        snap.marks = vec![3.0, 9.0];
        let view = make_view(&snap, true, &s, "this PC");
        assert_eq!(view.draft.iter().map(|d| d.text.as_str()).collect::<Vec<_>>(), vec!["line 2", "line 3", "line 4"]);
        assert_eq!(view.draft[2].speaker.as_deref(), Some("You"));
        assert!(view.draft_hint.is_none());
        assert_eq!(view.marks.as_deref(), Some("2 moments flagged"));
        assert!(view.quick.is_empty() || !view.expanded);
    }

    #[test]
    fn a_recording_in_the_window_shows_its_own_draft() {
        let mut snap = recording();
        snap.phase = Phase::Idle;
        snap.meeting_id = None;
        snap.other = Some(LiveRecording {
            id: 7,
            title: "Weekly sync".into(),
            client: "web".into(),
            duration: 30.0,
            has_system: true,
            draft: true,
            drafts: vec![line(1.0, "You", "hello")],
        });
        assert_eq!(live_meeting(&snap), Some(7));
        let s = State { meeting: Some(7), other_drafts: Some((7, vec![line(5.0, "Them", "newer")])), ..State::default() };
        let view = make_view(&snap, false, &s, "this PC");
        assert_eq!(view.title, "Recording in Trailmix");
        assert_eq!(view.draft[0].text, "newer");
        assert!(view.marks.is_none());
    }

    #[test]
    fn no_ai_says_why_and_offers_nothing_to_ask() {
        let plan: Plan = serde_json::from_str(
            r#"{"ready": false, "reason": "Ollama isn't reachable at http://127.0.0.1:9 (or has no models)", "provider": null,
                "label": null, "local": false, "cost": null, "costs": {}, "note": null, "warning": null, "off": {},
                "kinds": {"missed": "What did I miss?"}, "history": [], "as_of": "2026-10"}"#,
        )
        .unwrap();
        let s = State { meeting: Some(12), plan: Some(plan), expanded: true, ..State::default() };
        let view = make_view(&recording(), true, &s, "this PC");
        assert_eq!(view.reason.as_deref(), Some("Ollama isn't reachable at http://127.0.0.1:9 (or has no models)"));
        assert!(view.quick.is_empty() && !view.ask_box && view.footer.is_none());
        assert!(view.text().contains("Ollama isn't reachable"));
    }

    #[test]
    fn a_cloud_ai_shows_its_costs_and_the_answer() {
        let plan: Plan = serde_json::from_str(
            r#"{"ready": true, "label": "Gemini 2.5 Flash", "local": false,
                "costs": {"missed": 0.0042, "explain": 0.0031, "decided": 0.012, "question": null},
                "kinds": {"missed": "What did I miss?", "explain": "Explain that", "decided": "What's been decided?"},
                "off": {}, "history": []}"#,
        )
        .unwrap();
        let entry = Entry { q: "What did I miss?".into(), a: "- The launch moved to **March** [12:01]".into(), label: "Gemini 2.5 Flash".into(), kind: None, t: Some(722.0) };
        let mut s = State { meeting: Some(12), plan: Some(plan), expanded: true, entries: vec![entry], ..State::default() };
        let view = make_view(&recording(), true, &s, "this PC");
        assert_eq!(view.quick.iter().map(|q| q.cost.clone().unwrap_or_default()).collect::<Vec<_>>(), vec!["~0.4¢", "~0.3¢", "~$0.012"]);
        assert!(view.quick.iter().all(|q| q.enabled) && view.ask_box && view.ask_enabled);
        let answer = view.answer.clone().unwrap();
        assert_eq!(answer.asked_at.as_deref(), Some("asked at 12:02"));
        assert_eq!(answer.lines[0].plain(), "• The launch moved to March [12:01]");
        assert!(answer.label.is_none());
        assert_eq!(view.footer.as_deref(), Some("Gemini 2.5 Flash"));

        s.pending = Some("Explain that".into());
        let view = make_view(&recording(), true, &s, "this PC");
        assert!(view.answer.is_none() && view.quick.iter().all(|q| !q.enabled) && !view.ask_enabled);
        assert!(view.quick[1].busy);
        assert!(view.text().contains(READING));
    }

    #[test]
    fn a_model_here_is_free_and_says_so() {
        let plan: Plan = serde_json::from_str(
            r#"{"ready": true, "label": "Ollama (qwen2.5:3b)", "local": true, "costs": {"missed": 0.0},
                "off": {"decided": "Needs a cloud AI or a bigger model"}, "kinds": {}, "history": []}"#,
        )
        .unwrap();
        let s = State { meeting: Some(12), plan: Some(plan), expanded: true, ..State::default() };
        let view = make_view(&recording(), true, &s, "this PC");
        assert!(view.quick.iter().all(|q| q.cost.is_none()));
        assert!(!view.quick[2].enabled && view.off == vec![("What's been decided?".to_string(), "Needs a cloud AI or a bigger model".to_string())]);
        assert_eq!(view.footer.as_deref(), Some("Ollama (qwen2.5:3b) · on this PC, free"));
    }

    #[test]
    fn answers_are_parsed_line_by_line() {
        let lines = parse_answer("## Gist\n- First **point** [1:02]\n  - nested [1:02:03]\n2. Second\n\nPlain [link](http://x) `code`");
        assert_eq!(lines.len(), 5);
        assert!(lines[0].heading && lines[0].spans[0].bold);
        assert_eq!(lines[1].spans.iter().map(|s| (s.text.as_str(), s.bold, s.stamp)).collect::<Vec<_>>(), vec![
            ("First ", false, false),
            ("point", true, false),
            (" ", false, false),
            ("[1:02]", false, true)
        ]);
        assert_eq!(lines[2].indent, 1);
        assert_eq!(lines[2].plain(), "  • nested [1:02:03]");
        assert_eq!(lines[3].bullet.as_deref(), Some("2."));
        assert_eq!(lines[4].plain(), "Plain link code");
        assert_eq!(stamp_len("[123:45]"), None);
        assert_eq!(stamp_len("[ok]"), None);
    }
}
