// Runs on the audio thread: batches 128-sample blocks into ~256 ms int16 chunks and posts them out.
class PCMWorklet extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buf = new Float32Array(4096);
    this.n = 0;
  }

  push(sample) {
    this.buf[this.n++] = sample;
    if (this.n === this.buf.length) {
      const out = new Int16Array(this.n);
      for (let i = 0; i < this.n; i++) {
        const s = Math.max(-1, Math.min(1, this.buf[i]));
        out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
      this.port.postMessage(out.buffer, [out.buffer]);
      this.n = 0;
    }
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    // With no input (e.g. a stopped share) still emit silence, so both tracks stay time-aligned.
    for (let i = 0; i < 128; i++) this.push(channel ? channel[i] : 0);
    return true;
  }
}

registerProcessor("pcm-worklet", PCMWorklet);
