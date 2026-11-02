import { useEffect, useState } from "react";
import { api } from "../api";
import type { LocalAIServer, LocalAIStatus, Settings } from "../api";
import { platformWords } from "../platform";
import { CheckIcon } from "./icons";
import { Select } from "./ui";

/** An address on this computer (as the server's settings.on_this_computer has it). */
export function isLocalUrl(url: string): boolean {
  try {
    return ["localhost", "127.0.0.1", "[::1]"].includes(new URL(url).hostname);
  } catch {
    return false;
  }
}

/** A server of this computer's set as the notes AI through the OpenAI-compatible provider. */
export function usesLocalServer(s: Settings): boolean {
  return s.summary_provider === "custom" && isLocalUrl(s.custom_base_url);
}

/**
 * AI servers already running on this computer (LM Studio, Jan, llama.cpp, vLLM…), found by asking their usual
 * ports, each with its models and "Use": one list, whatever the app. Ollama has its own setup (install, download)
 * and isn't repeated here. Renders nothing when there's nothing to offer.
 */
export function FoundServers({
  s,
  onSaved,
  className = "",
}: {
  s: Settings;
  onSaved: (s: Settings) => void;
  className?: string;
}) {
  const [found, setFound] = useState<LocalAIStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    api
      .localAI()
      .then((v) => alive && setFound(v))
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, []);
  if (!found) return null;
  const servers = found.servers.filter((x) => x.kind === "openai");
  if (!servers.length && !found.installed.length) return null;

  const use = async (server: LocalAIServer, model: string) => {
    setError(null);
    try {
      onSaved(
        await api.putSettings({
          summary_provider: "custom",
          custom_name: server.name,
          custom_base_url: server.url,
          custom_model: model,
          custom_api_key: "",
          custom_context_tokens: server.models.find((m) => m.id === model)?.context ?? 0,
        }),
      );
      setFound(await api.localAI(true));
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <div className={`space-y-2.5 ${className}`}>
      <p className="font-medium text-ink">Found on {platformWords().thisComputer}</p>
      {servers.map((server) => (
        <ServerRow key={server.port} server={server} s={s} small={found.small_context} onUse={(m) => use(server, m)} />
      ))}
      {found.installed.map((app) => (
        <p key={app.name} className="text-hint text-ink-soft">
          {app.hint}
        </p>
      ))}
      {error && <p className="text-trail-deep">{error}</p>}
    </div>
  );
}

function ServerRow({
  server,
  s,
  small,
  onUse,
}: {
  server: LocalAIServer;
  s: Settings;
  small: number;
  onUse: (model: string) => void;
}) {
  const [model, setModel] = useState(
    server.in_use && server.models.some((m) => m.id === s.custom_model) ? s.custom_model : server.models[0].id,
  );
  const context = server.models.find((m) => m.id === model)?.context;
  const using = server.in_use && s.summary_provider === "custom" && s.custom_model === model;
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
      <span className="w-24 font-medium text-ink">{server.name}</span>
      <Select
        label={`${server.name} model`}
        size="sm"
        value={model}
        onChange={setModel}
        className="w-60"
        options={server.models.map((m) => ({
          value: m.id,
          label: m.id,
          hint: m.context ? `${m.context.toLocaleString()} tokens of context` : undefined,
        }))}
      />
      {using ? (
        <span className="flex items-center gap-1.5 text-forest-deep">
          <CheckIcon size={15} /> Writes your notes
        </span>
      ) : (
        <button type="button" className="btn btn-sm" onClick={() => onUse(model)}>
          Use
        </button>
      )}
      {!!context && context < small && (
        <p className="w-full text-hint text-sun-deep">
          {server.name} runs this model with {context.toLocaleString()} tokens of context, too few for a meeting's
          notes: raise its context length to 16,000 or more.
        </p>
      )}
    </div>
  );
}
