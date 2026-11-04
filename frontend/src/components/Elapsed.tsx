import { useEffect, useState } from "react";
import { formatDuration } from "../format";

/** Seconds since `from` (when the recording's clock read 0, in ms), counting up four times a second; 0 without one. */
export function useElapsed(from: number | null): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (from === null) return;
    setNow(Date.now());
    const t = setInterval(() => setNow(Date.now()), 250);
    return () => clearInterval(t);
  }, [from]);
  return from === null ? 0 : Math.max(0, (now - from) / 1000);
}

/** A recording's running time (1:05). It ticks by itself, so nothing around it draws again each tick. */
export default function Elapsed({ from }: { from: number | null }) {
  return <>{formatDuration(useElapsed(from))}</>;
}
