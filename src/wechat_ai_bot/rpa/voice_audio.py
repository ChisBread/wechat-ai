"""Bounded local audio decoding and an isolated PulseAudio recording source."""

import base64
import binascii
import json
import os
from pathlib import Path
import subprocess
import time
import uuid
import wave

MAX_AUDIO_BYTES = 10 * 1024 * 1024
MAX_VOICE_SECONDS = 55  # Leave headroom below WeChat's 60-second recording limit.


def prepare_voice(directory, *, file_path=None, audio_base64=None):
    """Snapshot and decode an upload; never fetch URLs or silently truncate audio."""
    if bool(file_path) == bool(audio_base64):
        raise ValueError("Provide exactly one of file_path or audio_base64")
    if audio_base64:
        if len(audio_base64) > 4 * ((MAX_AUDIO_BYTES + 2) // 3) + 128:
            raise ValueError("Audio exceeds 10 MiB")
        encoded = audio_base64
        if encoded.startswith("data:"):
            header, separator, encoded = encoded.partition(",")
            if not separator or not header.startswith("data:audio/") or not header.endswith(";base64"):
                raise ValueError("Expected an audio base64 data URI")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("Invalid audio base64") from exc
    else:
        path = Path(file_path).expanduser()
        if not path.is_file():
            raise ValueError("file_path must be a local audio file inside the container")
        with path.open("rb") as stream:
            data = stream.read(MAX_AUDIO_BYTES + 1)
    if not data or len(data) > MAX_AUDIO_BYTES:
        raise ValueError("Audio must be nonempty and at most 10 MiB")
    source = Path(directory) / "input.audio"
    destination = Path(directory) / "voice.wav"
    source.write_bytes(data)
    try:
        subprocess.run([
            "ffmpeg", "-nostdin", "-v", "error", "-y",
            "-protocol_whitelist", "file,pipe", "-i", str(source),
            "-map", "0:a:0", "-vn", "-t", str(MAX_VOICE_SECONDS + 1),
            "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", str(destination),
        ], check=True, capture_output=True, timeout=20)
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg is required for voice messages") from exc
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ValueError("Audio could not be decoded within 20 seconds") from exc
    duration = voice_duration(destination)
    return destination, {"duration_seconds": duration, "input_bytes": len(data),
                         "sample_rate": 48000, "channels": 1}


def voice_duration(path):
    with wave.open(str(path), "rb") as stream:
        if (stream.getnchannels(), stream.getsampwidth(), stream.getframerate()) != (1, 2, 48000):
            raise ValueError("Voice playback requires mono 48 kHz PCM16 WAV")
        frames = stream.getnframes()
        duration = frames / stream.getframerate()
        if not 1 <= duration <= MAX_VOICE_SECONDS:
            raise ValueError(f"Voice audio must be between 1 and {MAX_VOICE_SECONDS} seconds")
        if len(stream.readframes(frames)) != frames * 2:
            raise ValueError("Truncated WAV audio")
    return duration


class PulseVoiceSource:
    """Route only a newly opened WeChat recording stream; always undo our changes."""

    def __init__(self):
        self.env = os.environ.copy()
        # Selkies uses /defaults as PULSE_RUNTIME_PATH. Root clients must use
        # the explicit socket, or libpulse chmods that shared directory to 0700.
        if not self.env.get("PULSE_SERVER") and Path("/defaults/native").is_socket():
            self.env["PULSE_SERVER"] = "unix:/defaults/native"
        self.name = "wechat_ai_voice_" + uuid.uuid4().hex
        self.source = self.name + "_mic"
        self.modules = []
        self.original_source = None

    def pactl(self, *arguments):
        return subprocess.run(["pactl", *map(str, arguments)], env=self.env,
                              check=True, capture_output=True, text=True, timeout=5).stdout.strip()

    def streams(self):
        return [stream for stream in json.loads(self.pactl("-f", "json", "list", "source-outputs"))
                if stream.get("properties", {}).get("application.process.binary", "").lower()
                in {"wechat", "weixin"}]

    def __enter__(self):
        try:
            if self.streams():
                raise RuntimeError("WeChat is already recording or in a call; voice send was not started")
            self.original_source = self.pactl("get-default-source")
            self.modules.append(self.pactl("load-module", "module-null-sink",
                                          f"sink_name={self.name}", "rate=48000", "channels=1"))
            self.modules.append(self.pactl("load-module", "module-remap-source",
                                          f"master={self.name}.monitor", f"source_name={self.source}",
                                          "source_properties=device.description=WeChat-AI-Voice"))
            self.pactl("set-default-source", self.source)
            return self
        except Exception as exc:
            self.__exit__(type(exc), exc, exc.__traceback__)
            raise

    def bind_recording(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            streams = self.streams()
            if len(streams) > 1:
                raise RuntimeError("Ambiguous WeChat recording streams")
            if streams:
                self.stream_id = streams[0]["index"]
                self.pactl("move-source-output", self.stream_id, self.source)
                self.pactl("set-source-output-mute", self.stream_id, "0")
                self.pactl("set-source-output-volume", self.stream_id, "100%")
                self.check_recording()
                return
            time.sleep(0.1)
        raise RuntimeError("WeChat did not open a recording stream; check microphone access")

    def check_recording(self):
        sources = json.loads(self.pactl("-f", "json", "list", "sources"))
        source_id = next((s["index"] for s in sources if s["name"] == self.source), None)
        streams = self.streams()
        if source_id is None or not any(
            s["index"] == self.stream_id and s["source"] == source_id
            and not s.get("corked") and not s.get("mute") for s in streams
        ):
            raise RuntimeError("WeChat recording stopped or its audio route changed")

    def play(self, path, duration):
        subprocess.run(["paplay", f"--device={self.name}", "--latency-msec=50", str(path)],
                       env=self.env, check=True, capture_output=True, timeout=duration + 3)
        self.check_recording()

    def __exit__(self, *args):
        errors = []
        try:
            if self.original_source and self.pactl("get-default-source") == self.source:
                self.pactl("set-default-source", self.original_source)
        except Exception as exc:
            errors.append(exc)
        for module in reversed(self.modules):
            try:
                self.pactl("unload-module", module)
            except Exception as exc:
                errors.append(exc)
        self.modules.clear()
        if errors and not (args and args[0]):
            raise RuntimeError("Failed to restore PulseAudio after voice send") from errors[0]
