import { useState } from "react";
import { api } from "../api";
import { KeyIcon } from "./icons";
import { Ambient, Logo, TrailScene } from "./illustrations";
import { Spinner } from "./ui";

/** Shown only for hosted installs (TRAILMIX_ACCESS_TOKEN set on the server). */
export default function Login({ dusk, onDone }: { dusk: boolean; onDone: () => void }) {
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.login(token);
      onDone();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="relative flex min-h-full items-center justify-center p-5">
      <Ambient />
      <div className="relative w-full max-w-[420px] animate-enter">
        <div className="art-frame h-40">
          <TrailScene dusk={dusk} parallax className="h-full w-full" />
        </div>
        <form
          className="glass -mt-10 p-7"
          onSubmit={(e) => {
            e.preventDefault();
            void submit();
          }}
        >
          <div className="mb-5 flex items-center gap-3">
            <Logo size={36} />
            <div>
              <h1 className="font-display text-wordmark font-semibold leading-none tracking-[-0.02em]">Welcome back</h1>
              <p className="mt-1.5 text-hint text-ink-soft">This Trailmix is private.</p>
            </div>
          </div>
          <label htmlFor="token" className="mb-1.5 block text-ui font-medium">
            Access token
          </label>
          <div className="relative">
            <KeyIcon size={16} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-faint" />
            <input
              id="token"
              type="password"
              autoFocus
              autoComplete="current-password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              className="field h-10 w-full pl-9"
              placeholder="The token set on the server"
            />
          </div>
          {error && <p className="mt-2 text-ui text-trail-deep">{error}</p>}
          <button type="submit" disabled={busy || !token} className="btn btn-lg btn-primary mt-5 w-full">
            {busy && <Spinner />} Sign in
          </button>
        </form>
      </div>
    </div>
  );
}
