import { useState } from "react";
import { api } from "../api";
import type { NativeRecorder } from "../api";
import { recorderWords } from "../platform";
import { AlertIcon, CheckIcon } from "./icons";
import { Spinner } from "./ui";

const MIC_SETTINGS =
  "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone";
const AUDIO_SETTINGS =
  "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture";
/** Windows: Settings → Privacy & security → Microphone, and Settings → System → Sound. */
const WINDOWS_MIC_SETTINGS = "ms-settings:privacy-microphone";
const WINDOWS_SOUND_SETTINGS = "ms-settings:sound";

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
 * Windows asks nothing first (hearing other apps needs no permission there): only the microphone's privacy
 * switch can stand in the way, and the result says where it is.
 */
export default function SoundCheck({
  native,
  brief = false,
}: {
  native: NativeRecorder;
  /** The page around it already says what the check does: start at the macOS prompts. */
  brief?: boolean;
}) {
  const [askedAt, setAskedAt] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const words = recorderWords(native.platform);
  const windows = words.os === "windows";
  // Linux asks no permission either (no prompts, no privacy switch): Windows' wording, with its own settings.
  const linux = words.os === "linux";
  const noPrompts = windows || linux;
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
      {!running && !fresh && noPrompts && (
        <p>
          {brief
            ? "Turn your speakers or headphones up a little: you should hear two chimes."
            : `Trailmix plays two short chimes and listens for 5 seconds, to make sure both sides of your calls get recorded. ${words.osName} doesn't ask first.`}
        </p>
      )}
      {!running && !fresh && !noPrompts && (
        <>
          <p>
            {brief
              ? "The first time, macOS asks twice:"
              : "Trailmix plays two short chimes and listens for 5 seconds, to make sure both sides of your calls get recorded. The first time, macOS asks twice:"}
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
          <Spinner />{" "}
          {noPrompts
            ? "Listening… you should hear two chimes."
            : "Listening… you should hear two chimes. Answer any macOS prompts with Allow."}
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
            {result.mic === "denied" &&
              (windows ? (
                <>
                  Windows isn't letting Trailmix use it.{" "}
                  {settingsLink(
                    WINDOWS_MIC_SETTINGS,
                    "Open Settings → Privacy & security → Microphone",
                  )}
                  , turn on <b className="text-ink">Microphone access</b> and{" "}
                  <b className="text-ink">
                    Let desktop apps access your microphone
                  </b>
                  , then run the check again.
                </>
              ) : (
                <>
                  Trailmix isn't allowed to use it.{" "}
                  {settingsLink(
                    MIC_SETTINGS,
                    "Open Privacy & Security → Microphone",
                  )}{" "}
                  and turn on the switch next to Trailmix, then run the check
                  again.
                </>
              ))}
            {result.mic === "quiet" &&
              (linux ? (
                <>
                  Trailmix barely heard anything. Trailmix records the
                  microphone your system uses by default: check that it's the
                  right one in your sound settings (Input), and that it isn't
                  muted.
                </>
              ) : windows ? (
                <>
                  Trailmix barely heard anything. Trailmix records the
                  microphone Windows uses by default: check that it's the right
                  one in{" "}
                  {settingsLink(
                    WINDOWS_SOUND_SETTINGS,
                    "Settings → System → Sound → Input",
                  )}
                  , and that it isn't muted.
                </>
              ) : (
                <>
                  Trailmix barely heard anything. Check that the right
                  microphone is picked in the Trailmix menu bar menu, and that
                  it isn't muted.
                </>
              ))}
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
            {result.system === "silent" &&
              (linux ? (
                <>
                  Trailmix couldn't hear the chimes it played.
                  <ol className="mt-1 list-decimal space-y-0.5 pl-5">
                    <li>
                      Check that a speaker or headphones are connected and
                      picked in your sound settings (Output), and not muted.
                    </li>
                    <li>
                      Quit Trailmix ({words.trayName} icon → Quit Trailmix),
                      open it again, and run the check again.
                    </li>
                  </ol>
                </>
              ) : windows ? (
                <>
                  Trailmix couldn't hear the chimes it played.
                  <ol className="mt-1 list-decimal space-y-0.5 pl-5">
                    <li>
                      Check that a speaker or headphones are connected and
                      picked in{" "}
                      {settingsLink(
                        WINDOWS_SOUND_SETTINGS,
                        "Settings → System → Sound → Output",
                      )}
                      .
                    </li>
                    <li>
                      Quit Trailmix ({words.trayName} icon → Quit Trailmix),
                      open it again, and run the check again.
                    </li>
                  </ol>
                </>
              ) : (
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
                      <b className="text-ink">System Audio Recording Only</b>{" "}
                      and turn on the switch next to{" "}
                      <b className="text-ink">Trailmix</b>. If it isn't listed,
                      click <b className="text-ink">+</b> and choose Trailmix in
                      Applications.
                    </li>
                    <li>
                      Quit Trailmix (menu bar icon → Quit), open it again, and
                      run the check again.
                    </li>
                  </ol>
                </>
              ))}
            {result.system === "error" && (
              <>Couldn't listen to other apps. {result.detail}</>
            )}
          </Outcome>
        </ul>
      )}

      {error && <p className="mt-2 text-trail-deep">{error}</p>}
      {!running && (
        <button type="button" className={`btn mt-2.5 ${brief ? "btn-md btn-primary" : "btn-sm btn-soft"}`} onClick={run}>
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
