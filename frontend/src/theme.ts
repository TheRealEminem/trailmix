import { useEffect, useState } from "react";

export type ThemePref = "system" | "light" | "dusk";
const KEY = "trailmix.theme";

function read(): ThemePref {
  try {
    const v = localStorage.getItem(KEY);
    return v === "light" || v === "dusk" ? v : "system";
  } catch {
    return "system";
  }
}

const systemDark = () => window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;

/** Light by day, dusk after dark: follows macOS unless you pick one. Remembered per browser. */
export function useTheme() {
  const [pref, setPrefState] = useState<ThemePref>(read);
  const [sysDark, setSysDark] = useState(systemDark);

  useEffect(() => {
    const mq = window.matchMedia?.("(prefers-color-scheme: dark)");
    if (!mq) return;
    const on = () => setSysDark(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);

  const dusk = pref === "dusk" || (pref === "system" && sysDark);

  useEffect(() => {
    document.documentElement.dataset.theme = dusk ? "dusk" : "light";
    document.querySelector('meta[name="theme-color"]')?.setAttribute("content", dusk ? "#0F1513" : "#F8F7F2");
  }, [dusk]);

  const setPref = (p: ThemePref) => {
    setPrefState(p);
    try {
      localStorage.setItem(KEY, p);
    } catch {
      /* ignore */
    }
  };

  return { pref, setPref, dusk };
}
