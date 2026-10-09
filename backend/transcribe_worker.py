"""Runs the final (large-model) transcription in its own process: `python transcribe_worker.py <audio_dir>`.

A separate process is the only way to *guarantee* the model's memory goes back to macOS: when it
exits, every page is released. (Freeing the model inside a long-lived server leaves ~1.5 GB of
allocator memory resident.) Results are written to stdout as `TRAILMIX:<json>` lines.
"""
import json
import sys
from pathlib import Path

import numpy as np

import audio_store as store
import mlx_engine
import vad

SR = store.SAMPLE_RATE


def emit(**msg) -> None:
    print("TRAILMIX:" + json.dumps(msg), flush=True)


def main(folder: Path, vocabulary: str = "") -> None:
    tracks = {}
    for name in store.TRACKS.values():
        audio = store.load_track(folder, name)
        if audio is not None and np.max(np.abs(audio), initial=0) > 0.01:  # skip empty/silent tracks
            tracks[name] = audio
    emit(tracks=list(tracks), duration=max((len(a) for a in tracks.values()), default=0) / SR)

    for name, audio in tracks.items():
        regions = vad.speech_regions(audio)
        segments = []
        for i, (a, b) in enumerate(regions):
            for seg in mlx_engine.run(audio[a:b], mlx_engine.FINAL_MODEL, vocabulary or None):
                segments.append({**seg, "start": seg["start"] + a / SR, "end": seg["end"] + a / SR})
            emit(progress=[name, i + 1, len(regions)])
        emit(track=name, segments=segments)
    emit(done=True)


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else "")
