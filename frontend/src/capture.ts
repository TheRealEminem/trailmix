import { api } from "./api";
import type { Bookmark, DraftLine } from "./api";

export interface ActiveRecording {
  meetingId: number;
  startedAt: number;
  warning?: string;
  /** Name of the microphone that is actually being recorded. */
  micLabel: string;
  /** Current input loudness 0..1 per track (system is null when no meeting audio is shared). Cheap; poll per frame. */
  levels: () => { mic: number; system: number | null };
  /** Flags the current moment; the backend confirms through onMarked. */
  mark: () => void;
  /** Stops capturing and tells the backend to finish; resolves once the backend has taken over. */
  stop: () => Promise<void>;
}

interface Options {
  /** Specific microphone; empty = the browser's default. */
  micDeviceId: string;
  includeMeetingAudio: boolean;
  liveDraft: boolean;
  onDraft: (line: DraftLine) => void;
  onNotice: (message: string) => void;
  onMarked: (b: Bookmark) => void;
  /** Called if the socket drops without stop() being requested. The backend still processes what it got. */
  onDropped: () => void;
  /** Called when the other app (the menu bar helper) stopped this recording. Call stop() to release the mic. */
  onStoppedElsewhere: () => void;
}

// Wire format: 1 channel byte (0 = your mic, 1 = meeting audio) followed by little-endian int16 PCM @ 16 kHz.
const CH_MIC = 0;
const CH_SYSTEM = 1;

interface Tap {
  untap: () => void;
  level: () => number;
}

function tap(ctx: AudioContext, stream: MediaStream, channel: number, ws: WebSocket, sink: AudioNode): Tap {
  const source = ctx.createMediaStreamSource(stream);
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 512;
  const buf = new Float32Array(analyser.fftSize);
  const node = new AudioWorkletNode(ctx, "pcm-worklet", { channelCount: 1, channelCountMode: "explicit" });
  node.port.onmessage = (e: MessageEvent<ArrayBuffer>) => {
    if (ws.readyState !== WebSocket.OPEN) return;
    const frame = new Uint8Array(1 + e.data.byteLength);
    frame[0] = channel;
    frame.set(new Uint8Array(e.data), 1);
    ws.send(frame);
  };
  source.connect(node);
  source.connect(analyser);
  node.connect(sink); // the sink is muted; an unconnected worklet is not guaranteed to be pulled
  analyser.connect(sink);
  return {
    untap: () => {
      source.disconnect();
      node.disconnect();
      analyser.disconnect();
      node.port.onmessage = null;
    },
    level: () => {
      analyser.getFloatTimeDomainData(buf);
      let sum = 0;
      for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
      const db = 20 * Math.log10(Math.sqrt(sum / buf.length) + 1e-8);
      return Math.min(1, Math.max(0, (db + 58) / 46)); // ~-58 dB (silence) .. -12 dB (loud)
    },
  };
}

/**
 * Streams your mic and (optionally) a shared tab's audio to the backend as two separate tracks.
 * Audio is never held in the browser: every ~256 ms chunk is sent and forgotten.
 */
export async function startRecording(opts: Options): Promise<ActiveRecording> {
  const streams: MediaStream[] = [];
  const stopAll = () => streams.forEach((s) => s.getTracks().forEach((t) => t.stop()));
  let ctx: AudioContext | undefined;
  let ws: WebSocket | undefined;

  try {
    // The screen/tab picker must be asked for first, straight from the click: after another await (the mic
    // prompt) Safari and the app's web view refuse it ("must be called from a user gesture handler").
    let warning: string | undefined;
    let display: MediaStream | undefined;
    if (opts.includeMeetingAudio) {
      // Browsers require video: true for getDisplayMedia; the video is never sent anywhere.
      display = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: true });
      streams.push(display);
      if (display.getAudioTracks().length === 0) {
        warning =
          (warning ? warning + " " : "") +
          'No meeting audio was shared, so only your microphone is recorded. Share a browser tab and tick "Also share tab audio" to capture the other side.';
        display = undefined;
      }
    }

    const processing = { echoCancellation: true, noiseSuppression: true };
    let mic: MediaStream;
    try {
      mic = await navigator.mediaDevices.getUserMedia({
        audio: opts.micDeviceId ? { ...processing, deviceId: { exact: opts.micDeviceId } } : processing,
      });
    } catch (e) {
      // The chosen mic was unplugged / is out of range: fall back rather than fail the meeting.
      if (!opts.micDeviceId || !["OverconstrainedError", "NotFoundError"].includes((e as Error).name)) throw e;
      mic = await navigator.mediaDevices.getUserMedia({ audio: processing });
      warning =
        (warning ? warning + " " : "") +
        "The selected microphone isn't available, so the default microphone is being used.";
    }
    streams.push(mic);
    const micLabel = mic.getAudioTracks()[0]?.label || "Default microphone";

    const { id: meetingId } = await api.startMeeting();
    ws = new WebSocket(api.streamUrl(meetingId, opts.liveDraft));
    ws.binaryType = "arraybuffer";
    const socket = ws;
    await new Promise<void>((resolve, reject) => {
      socket.onopen = () => resolve();
      socket.onerror = () => reject(new Error("Could not open the audio connection to the backend"));
    });

    let stopping = false;
    let stoppedElsewhere = false;
    let onStopped: () => void = () => {};
    const stopped = new Promise<void>((resolve) => (onStopped = resolve));
    socket.onmessage = (e) => {
      const msg = JSON.parse(e.data as string);
      if (msg.type === "draft") opts.onDraft({ start: msg.start, speaker: msg.speaker, text: msg.text });
      else if (msg.type === "status") opts.onNotice(msg.message);
      else if (msg.type === "marked") opts.onMarked({ t: msg.t, note: msg.note });
      else if (msg.type === "stopped") {
        onStopped();
        if (!stopping) {
          stoppedElsewhere = true;
          opts.onStoppedElsewhere();
        }
      }
    };
    socket.onclose = () => {
      onStopped();
      if (!stopping && !stoppedElsewhere) opts.onDropped();
    };

    ctx = new AudioContext({ sampleRate: 16000 });
    const audioCtx = ctx;
    await audioCtx.audioWorklet.addModule("/pcm-worklet.js");
    const mute = audioCtx.createGain();
    mute.gain.value = 0;
    mute.connect(audioCtx.destination);

    const micTap = tap(audioCtx, mic, CH_MIC, socket, mute);
    const systemTap = display ? tap(audioCtx, new MediaStream(display.getAudioTracks()), CH_SYSTEM, socket, mute) : null;
    const taps = systemTap ? [micTap, systemTap] : [micTap];
    if (display) {
      display.getAudioTracks()[0].addEventListener("ended", () =>
        opts.onNotice("Meeting-audio sharing stopped. Still recording your microphone."),
      );
    }

    return {
      meetingId,
      startedAt: Date.now(),
      warning,
      micLabel,
      mark: () => {
        if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "mark" }));
      },
      levels: () => ({ mic: micTap.level(), system: systemTap ? systemTap.level() : null }),
      stop: async () => {
        stopping = true;
        taps.forEach((t) => t.untap());
        stopAll();
        void audioCtx.close();
        if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "stop" }));
        await Promise.race([stopped, new Promise((r) => setTimeout(r, 8000))]);
        socket.close();
      },
    };
  } catch (err) {
    stopAll();
    void ctx?.close();
    ws?.close();
    throw err;
  }
}
