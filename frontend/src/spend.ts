import { useEffect, useState } from "react";
import { api } from "./api";

/**
 * When something is worth asking about first. Trailmix keeps a running tally of what the paid AI has done (Settings →
 * Usage) and says nothing about small costs; only a job that would cost this much or more gets a warning or a price
 * tag. The amount is a setting, in cents (0: never ask).
 */
let warnCents = 50;
let loading: Promise<void> | null = null;

const load = () => (loading ??= api.getSettings().then((s) => void (warnCents = s.spend_warn_cents)).catch(() => undefined));

/** Settings changed it. */
export const setSpendWarn = (cents: number) => void (warnCents = cents);

/**
 * Whether `cost` (dollars) is worth a warning. A price Trailmix doesn't know counts only for `bulk` work (a lot of audio
 * or text), so one small unpriced question never nags.
 */
export const isBigSpend = (cost: number | null | undefined, bulk = false, cents = warnCents) =>
  cents > 0 && (cost == null ? bulk : cost * 100 >= cents);

/** `isBigSpend` with the setting as of now. */
export function useBigSpend() {
  const [cents, setCents] = useState(warnCents);
  useEffect(() => {
    let alive = true;
    void load().then(() => alive && setCents(warnCents));
    return () => {
      alive = false;
    };
  }, []);
  return (cost: number | null | undefined, bulk = false) => isBigSpend(cost, bulk, cents);
}
