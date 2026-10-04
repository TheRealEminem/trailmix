import { useState } from "react";
import { api } from "../api";
import type { NativeRecorder } from "../api";
import { AlertIcon, CheckIcon } from "./icons";
import { Spinner } from "./ui";

const MIC_SETTINGS =
  "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone";
const AUDIO_SETTINGS =
  "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture";

const settingsLink = (href: string, text: string) => (
  <a
    className="font-medium text-forest underline underline-offset-2"
    href={href}
  >
    {text}
  </a>
);

/** Is the last sound check a pass? Used by the setup checklist. */
export const soundCheckPassed = (n: NativeRecorder | null) =>
  n?.sound_check?.mic === "ok" && n.sound_check.system === "ok";

/**
 * Plays a chime and listens: checks that your mic and other apps' audio both reach Trailmix, walking you
 * through the macOS permission prompts (what they say, what to click) and what to switch on if it fails.
 */
export default function SoundCheck({ native }: { native: NativeRecorder }) {
  const [askedAt, setAskedAt] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const result = native.sound_check;
  const fresh =
    result &&
    !result.running &&
    result.at &&
    (!askedAt || result.at * 1000 >= askedAt - 1000);
  const running = askedAt !== null && !fresh;

  const run = () => {
    setError(null);
    setAskedAt(Date.now());
    api.soundCheck().catch((e) => {
      setAskedAt(null);
      setError((e as Error).message);
    });
  };

  return (
    <div className="text-hint leading-relaxed text-ink-soft">
      {!running && !fresh && (
        <>
          <p>
            Trailmix plays two short chimes and listens for 5 seconds, to make
            sure both sides of your calls get recorded. The first time, macOS
            asks twice:
          </p>
          <ol className="mt-1.5 list-decimal space-y-1 pl-5">
            <li>
              <b className="text-ink">
                “Trailmix” would like to access the microphone.
              </b>{" "}
              Click <b className="text-ink">Allow</b>.
            </li>
            <li>
              <b className="text-ink">
                “Trailmix” would like to record this computer's audio
              </b>{" "}
              (or a System Audio Recording prompt). Click{" "}
              <b className="text-ink">Allow</b>. This is how Trailmix hears
              Zoom, Meet, Teams or FaceTime; it never records your screen.
            </li>
          </ol>
        </>
      )}

      {running && (
        <p className="flex items-center gap-2 text-ink">
          <Spinner /> Listening… you should hear two chimes. Answer any macOS
          prompts with Allow.
        </p>
      )}

      {fresh && result && (
        <ul className="space-y-2">
          <Outcome
            ok={result.mic === "ok"}
            title={
              result.mic === "ok" ? "Your microphone works" : "Your microphone"
            }
          >
            {result.mic === "denied" && (
              <>
                Trailmix isn't allowed to use it.{" "}
                {settingsLink(
                  MIC_SETTINGS,
                  "Open Privacy & Security → Microphone",
                )}{" "}
                and turn on the switch next to Trailmix, then run the check
                again.
              </>
            )}
            {result.mic === "quiet" && (
              <>
                Trailmix barely heard anything. Check that the right microphone
                is picked in the Trailmix menu bar menu, and that it isn't
                muted.
              </>
            )}
            {result.mic === "error" && (
              <>Couldn't start the microphone. {result.detail}</>
            )}
          </Outcome>
          <Outcome
            ok={result.system === "ok"}
            title={
              result.system === "ok"
                ? "Trailmix can hear other apps (your calls)"
                : "Other apps' audio (your calls)"
            }
          >
            {result.system === "silent" && (
              <>
                Trailmix couldn't hear the chime, which usually means it isn't
                allowed to record system audio yet.
                <ol className="mt-1 list-decimal space-y-0.5 pl-5">
                  <li>
                    {settingsLink(
                      AUDIO_SETTINGS,
                      "Open Privacy & Security → Screen & System Audio Recording",
                    )}
                    .
                  </li>
                  <li>
                    Scroll to{" "}
                    <b className="text-ink">System Audio Recording Only</b> and
                    turn on the switch next to{" "}
                    <b className="text-ink">Trailmix</b>. If it isn't listed,
                    click <b className="text-ink">+</b> and choose Trailmix in
                    Applications.
                  </li>
                  <li>
                    Quit Trailmix (menu bar icon → Quit), open it again, and run
                    the check again.
                  </li>
                </ol>
              </>
            )}
            {result.system === "error" && (
              <>Couldn't listen to other apps. {result.detail}</>
            )}
          </Outcome>
        </ul>
      )}

      {error && <p className="mt-2 text-trail-deep">{error}</p>}
      {!running && (
        <button className="btn btn-sm btn-soft mt-2.5" onClick={run}>
          {fresh ? "Run it again" : "Run sound check"}
        </button>
      )}
    </div>
  );
}

function Outcome({
  ok,
  title,
  children,
}: {
  ok: boolean;
  title: string;
  children?: React.ReactNode;
}) {
  return (
    <li className="flex gap-2">
      {ok ? (
        <CheckIcon size={16} className="mt-0.5 shrink-0 text-forest" />
      ) : (
        <AlertIcon size={16} className="mt-0.5 shrink-0 text-trail-deep" />
      )}
      <div>
        <div className={`font-medium ${ok ? "text-ink" : "text-trail-deep"}`}>
          {title}
        </div>
        {!ok && children}
      </div>
    </li>
  );
}
