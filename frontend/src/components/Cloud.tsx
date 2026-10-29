import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { CloudAccount, CloudLink, CloudStatus } from "../api";
import { platformWords } from "../platform";
import { CheckIcon } from "./icons";
import { Spinner } from "./ui";

/** Trailmix Cloud: signing this Mac in (like a TV: a code, then the browser) and what the plan gives right now. */

export const PLAN_NAMES: Record<NonNullable<CloudAccount["plan"]>, string> = {
  trial: "Free trial",
  basic: "Basic",
  pro: "Pro",
  max: "Max",
  none: "No plan",
};

/** "2026-11-01" as a local date ("Nov 1"): read as midnight UTC it would be the day before west of Greenwich. */
export function periodDate(iso: string): string {
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  return new Date(y, m - 1, d).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

const hours = (h: number) => (h < 10 ? h.toFixed(1).replace(/\.0$/, "") : Math.round(h).toString());

/** One line on what the plan gives now, e.g. "Pro · 12 of 50 live hours used this month". */
export function planLine(a: CloudAccount): string {
  const name = PLAN_NAMES[a.plan ?? "none"];
  if (a.status === "lapsed" || a.plan === "none") return `${name}: pick a plan to keep using Trailmix Cloud`;
  if (a.plan === "trial")
    return a.trial_hours_left != null ? `${name} · ${hours(a.trial_hours_left)} hours left at the Pro level` : name;
  if (a.plan === "basic") return `${name} · unlimited meetings, notes right after each one`;
  if (a.level === "basic") return `${name} · this month's live hours are used`;
  if (a.pro_hours_limit != null && a.pro_hours_used != null)
    return `${name} · ${hours(a.pro_hours_used)} of ${hours(a.pro_hours_limit)} live hours used this month`;
  return name;
}

type Flow =
  | { state: "idle" }
  | { state: "starting" }
  | { state: "waiting"; link: CloudLink }
  | { state: "expired" }
  | { state: "error"; message: string };

/** Signing in: asks for a code, opens the sign-in page, and asks every few seconds until it's done. */
function useSignIn(onDone: () => void) {
  const [flow, setFlow] = useState<Flow>({ state: "idle" });
  const timer = useRef<number | null>(null);
  const stop = () => {
    if (timer.current != null) window.clearTimeout(timer.current);
    timer.current = null;
  };
  useEffect(() => stop, []);

  const start = async () => {
    stop();
    setFlow({ state: "starting" });
    let link: CloudLink;
    try {
      link = await api.cloudLink();
    } catch (e) {
      setFlow({ state: "error", message: (e as Error).message });
      return;
    }
    setFlow({ state: "waiting", link });
    window.open(link.link_url, "_blank", "noopener"); // the app opens it in your browser; a link stays below if not
    const until = Date.now() + link.expires_in * 1000;
    const ask = async () => {
      if (Date.now() > until) return setFlow({ state: "expired" });
      try {
        const r = await api.cloudPoll(link.code);
        if (r.status === "done") {
          setFlow({ state: "idle" });
          onDone();
          return;
        }
        if (r.status === "expired") return setFlow({ state: "expired" });
      } catch {
        // offline for a moment: keep asking until the code runs out
      }
      timer.current = window.setTimeout(() => void ask(), Math.max(2, link.interval) * 1000);
    };
    timer.current = window.setTimeout(() => void ask(), Math.max(2, link.interval) * 1000);
  };
  const cancel = () => {
    stop();
    setFlow({ state: "idle" });
  };
  return { flow, start, cancel };
}

/**
 * Trailmix Cloud's sign-in and plan, for Settings and the first-run setup. `onChange` is called after signing in or
 * out, with the new status.
 */
export default function CloudPanel({
  onChange,
  intro = true,
}: {
  onChange?: (status: CloudStatus) => void;
  /** say what Trailmix Cloud is above the sign-in button */
  intro?: boolean;
}) {
  const [status, setStatus] = useState<CloudStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = async (refresh = true) => {
    try {
      const s = await api.cloudAccount(refresh);
      setStatus(s);
      return s;
    } catch (e) {
      setError((e as Error).message);
      return null;
    }
  };
  useEffect(() => {
    void load(false).then(() => load(true)); // the cached plan at once, then the server's
  }, []);
  const { flow, start, cancel } = useSignIn(() => void load().then((s) => s && onChange?.(s)));

  const signOut = async () => {
    setError(null);
    try {
      await api.cloudSignOut();
      const s = await load(false);
      if (s) onChange?.(s);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  if (!status) return <Spinner className="my-3" />;

  if (status.signed_in) {
    const a = status.account;
    const upgrade = a.upgrade_url && a.plan !== "max";
    return (
      <div className="py-3.5">
        <p className="flex items-start gap-1.5 text-label">
          <CheckIcon size={16} className="mt-0.5 shrink-0 text-forest" />
          <span>
            Signed in{a.email ? ` as ${a.email}` : ""}. {planLine(a)}.
          </span>
        </p>
        {status.off.live_captions && (
          <p className="mt-1.5 text-hint leading-relaxed text-ink-soft">
            {status.off.live_captions}
            {a.period_end ? `, until ${periodDate(a.period_end)}` : ""}.
          </p>
        )}
        <div className="mt-3 flex flex-wrap gap-2">
          {upgrade && (
            <a className="btn btn-sm btn-primary" href={a.upgrade_url!} target="_blank" rel="noreferrer">
              {a.plan === "pro" ? "See Max" : a.plan === "basic" ? "See Pro" : "Pick a plan"}
            </a>
          )}
          {a.manage_url && (
            <a className="btn btn-sm btn-soft" href={a.manage_url} target="_blank" rel="noreferrer">
              Manage plan
            </a>
          )}
          <button type="button" className="btn btn-sm btn-ghost" onClick={() => void signOut()}>
            Sign out
          </button>
        </div>
        {error && <p className="mt-2 text-hint text-trail-deep">{error}</p>}
      </div>
    );
  }

  return (
    <div className="py-3.5">
      {intro && (
        <p className="text-hint leading-relaxed text-ink-soft">
          Trailmix runs the AI for you: a transcript and notes for every meeting, on any {platformWords().computer}, with no keys to set
          up. Start with 5 free hours of Pro: captions while you talk and catch-up when you zone out. Plans from $8 a
          month; cancel any time and everything stays on {platformWords().thisComputer}.
        </p>
      )}
      {flow.state === "waiting" ? (
        <div className="mt-3 rounded-lg bg-surface-subtle/60 p-3.5 ring-1 ring-inset ring-line">
          <p className="flex items-center gap-2 text-label">
            <Spinner />
            Finish signing in in your browser.
          </p>
          <p className="mt-1.5 text-hint text-ink-soft">
            Check the page shows the code <span className="font-mono text-ink">{flow.link.code}</span>.
          </p>
          <div className="mt-3 flex flex-wrap gap-2">
            <a className="btn btn-sm btn-soft" href={flow.link.link_url} target="_blank" rel="noreferrer">
              Open the sign-in page
            </a>
            <button type="button" className="btn btn-sm btn-ghost" onClick={cancel}>
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <div className="mt-3">
          <button
            type="button"
            className="btn btn-sm btn-primary"
            disabled={flow.state === "starting"}
            onClick={() => void start()}
          >
            {flow.state === "starting" ? "Opening…" : "Sign in or start a free trial"}
          </button>
          {flow.state === "expired" && (
            <p className="mt-2 text-hint text-trail-deep">That sign-in code ran out. Try again.</p>
          )}
          {flow.state === "error" && <p className="mt-2 text-hint text-trail-deep">{flow.message}</p>}
          {error && <p className="mt-2 text-hint text-trail-deep">{error}</p>}
        </div>
      )}
    </div>
  );
}
