import { useEffect, useState } from "react";
import { api } from "../api";
import type { RecordingLaw, RecordingLaws as Table } from "../api";
import { Select } from "./ui";

// EU countries without an entry of their own read the shared EU one.
const EU = "AT BE BG CY CZ DK EE ES FI GR HR HU IT LT LU LV MT NL PL PT RO SE SI SK".split(" ");
// Time zones that only one US state uses. (Most zones span several states, so you pick yours.)
const US_ZONES: Record<string, string> = {
  "America/Phoenix": "AZ",
  "America/Boise": "ID",
  "America/Detroit": "MI",
  "America/Menominee": "MI",
  "America/Anchorage": "AK",
  "America/Juneau": "AK",
  "America/Sitka": "AK",
  "America/Nome": "AK",
  "Pacific/Honolulu": "HI",
};
export const OTHER = "OTHER";

export const CONSENT: Record<RecordingLaw["consent"], { label: string; tone: string }> = {
  all: { label: "Everyone must agree", tone: "bg-sun-soft text-sun-deep" },
  one: { label: "One person's OK is enough", tone: "bg-forest-soft text-forest-deep" },
  mixed: { label: "It depends", tone: "bg-sky-soft text-sky-deep" },
  unknown: { label: "No clear rule", tone: "bg-surface-subtle text-ink-soft" },
};

/** The region this computer seems to be in, from its language and time zone: a starting point you confirm. */
export function guess(table: Table): string {
  let zone = "";
  let country = "";
  try {
    zone = Intl.DateTimeFormat().resolvedOptions().timeZone ?? "";
    country = (new Intl.Locale(navigator.language).region ?? "").toUpperCase();
  } catch {
    /* no Intl.Locale */
  }
  if (zone.startsWith("America/Indiana/")) return "US-IN";
  if (zone.startsWith("America/Kentucky/") || zone === "America/Louisville") return "US-KY";
  if (zone.startsWith("America/North_Dakota/")) return "US-ND";
  if (US_ZONES[zone]) return `US-${US_ZONES[zone]}`;
  if (country === "US") return "US";
  if (EU.includes(country)) return "EU";
  return table.regions.some((r) => r.id === country) ? country : "";
}

export function stamp(table: Table) {
  return `${table.version}/${table.updated}`;
}

/** The entry for a region: null until one is picked (or only "United States", which needs a state). */
export function entryFor(table: Table, region: string): RecordingLaw | null {
  return region === "US" || !region ? null : (table.regions.find((r) => r.id === region) ?? table.fallback);
}

/** The country list (the US first, then A to Z, then "somewhere else") and the US states. */
export function regionOptions(table: Table) {
  const countries = [
    { value: "US", label: "United States" },
    ...table.regions
      .filter((r) => r.country !== "US")
      .map((r) => ({ value: r.id, label: r.name }))
      .sort((a, b) => a.label.localeCompare(b.label)),
    { value: OTHER, label: table.fallback.name },
  ];
  const states = table.regions
    .filter((r) => r.country === "US")
    .map((r) => ({ value: r.id, label: r.name }));
  return { countries, states };
}

/** Has your region's entry changed since you read it? (The table is corrected now and then.) */
export function lawChanged(table: Table | null, region: string, seen: string): boolean {
  if (!table || !region || !seen || seen === stamp(table)) return false;
  const [, seenDate] = seen.split("/");
  const entry = table.regions.find((r) => r.id === region);
  return !!entry && entry.reviewed > seenDate.slice(0, 7);
}

export function regionName(table: Table | null, region: string): string {
  if (!table || !region) return "";
  if (region === OTHER) return table.fallback.name;
  return table.regions.find((r) => r.id === region)?.name ?? region;
}

export function Source({ text }: { text: string }) {
  return text.startsWith("http") ? (
    <a className="text-forest underline decoration-forest/30 underline-offset-2" href={text} target="_blank" rel="noreferrer">
      {new URL(text).hostname.replace(/^www\./, "")}
    </a>
  ) : (
    <span>{text}</span>
  );
}

/**
 * What the law says about recording calls where you are, from a table that's updated now and then. Information
 * only: Trailmix's settings stay as they are unless you tick the box and apply what an entry suggests.
 */
export default function RecordingLaws({
  region: saved,
  retention,
  onSaved,
}: {
  region: string;
  retention: number;
  onSaved: () => void;
}) {
  const [table, setTable] = useState<Table | null>(null);
  const [region, setRegion] = useState(saved);
  const [agreed, setAgreed] = useState(false);
  const [applied, setApplied] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .recordingLaws()
      .then((t) => {
        setTable(t);
        if (!saved) setRegion(guess(t));
      })
      .catch((e) => setError((e as Error).message));
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  if (!table) return error ? <p className="text-hint text-trail-deep">{error}</p> : null;

  const country = region.startsWith("US") ? "US" : region;
  const entry = entryFor(table, region);
  const { countries, states } = regionOptions(table);
  const suggest = entry?.retention;
  const alreadySet = suggest && retention === suggest.suggest;

  const confirm = async () => {
    setError(null);
    try {
      await api.putSettings({ legal_region: region, legal_seen: stamp(table) });
      onSaved();
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const apply = async () => {
    if (!suggest) return;
    setError(null);
    try {
      await api.putSettings({
        audio_retention_days: suggest.suggest,
        legal_region: region,
        legal_seen: stamp(table),
      });
      setApplied(true);
      onSaved();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <div className="space-y-3 text-hint leading-relaxed text-ink-soft">
      <div className="flex flex-wrap gap-2">
        <Select
          label="Country"
          size="sm"
          className="w-56"
          value={country}
          onChange={(v) => setRegion(v)}
          options={[...(country ? [] : [{ value: "", label: "Where are you?" }]), ...countries]}
        />
        {country === "US" && (
          <Select
            label="State"
            size="sm"
            className="w-48"
            value={region === "US" ? "" : region}
            onChange={(v) => setRegion(v)}
            options={[...(region === "US" ? [{ value: "", label: "Pick your state" }] : []), ...states]}
          />
        )}
      </div>

      {entry && (
        <div className="space-y-2 rounded-md bg-surface-subtle p-3">
          <span className={`chip ${CONSENT[entry.consent].tone}`}>{CONSENT[entry.consent].label}</span>
          <p className="text-ink">{entry.summary}</p>
          {entry.notes && <p>{entry.notes}</p>}
          {entry.sources.length > 0 && (
            <p className="text-meta">
              Sources:{" "}
              {entry.sources.map((s, i) => (
                <span key={s}>
                  {i > 0 && "; "}
                  <Source text={s} />
                </span>
              ))}
              . Reviewed {entry.reviewed}.
            </p>
          )}
          <p className="text-meta italic">{table.disclaimer}</p>
        </div>
      )}

      {entry && suggest && (
        <div className="space-y-2">
          <p>
            <span className="font-medium text-ink">A setting you could change: </span>
            {suggest.suggest === 0
              ? "delete each recording once it's transcribed, keeping the transcript and notes."
              : `keep recordings for ${suggest.suggest} days.`}{" "}
            {suggest.why} This doesn't replace asking people before you record them.
          </p>
          {applied || alreadySet ? (
            <p className="text-forest-deep">
              {applied ? "Done. " : ""}Recordings are deleted once they're transcribed. You can change this in
              Settings → Privacy.
            </p>
          ) : (
            <>
              <label className="flex items-start gap-2">
                <input
                  type="checkbox"
                  className="mt-1"
                  checked={agreed}
                  onChange={(e) => setAgreed(e.target.checked)}
                />
                <span>I've read this and want Trailmix to delete recordings right after they're transcribed.</span>
              </label>
              <button className="btn btn-sm btn-primary" disabled={!agreed} onClick={apply}>
                Apply
              </button>
            </>
          )}
        </div>
      )}

      {entry && (
        <div className="flex flex-wrap items-center gap-2">
          <button className="btn btn-sm btn-soft" onClick={confirm}>
            {saved === region ? "Got it" : "Got it, this is where I am"}
          </button>
          <span className="text-meta">Trailmix's settings stay as they are unless you apply a change.</span>
        </div>
      )}
      {error && <p className="text-trail-deep">{error}</p>}
    </div>
  );
}
