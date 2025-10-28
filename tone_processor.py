from __future__ import annotations

import argparse
import dataclasses
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - fallback for 3.10 runtimes
    import tomli as tomllib  # type: ignore[no-redef]


SILENCE_START_RE = re.compile(r"silence_start:\s*([0-9.]+)")
SILENCE_END_RE = re.compile(r"silence_end:\s*([0-9.]+)")
MIN_SEGMENT_DURATION = 0.05
MERGE_GAPS_SECONDS = [i / 1000 for i in range(60, 301, 10)]


class ConfigError(RuntimeError):
    """Raised when the user configuration is invalid."""


@dataclasses.dataclass(slots=True)
class DetectSettings:
    enabled: bool
    noise_db: float
    min_silence: float
    pad: float


@dataclasses.dataclass(slots=True)
class ClipSettings:
    count: int
    names: list[str]


@dataclasses.dataclass(slots=True)
class GeneralSettings:
    input_path: Path
    output_dir: Path
    audio_format: str
    combined_name: str


@dataclasses.dataclass(slots=True)
class ProcessorConfig:
    general: GeneralSettings
    detect: DetectSettings
    clips: ClipSettings

    @classmethod
    def from_toml(cls, config_path: Path) -> "ProcessorConfig":
        if not config_path.exists():
            raise ConfigError(f"Config file not found: {config_path}")

        with config_path.open("rb") as fh:
            raw = tomllib.load(fh)

        base_dir = config_path.parent
        general_data = raw.get("general") or {}
        detect_data = raw.get("detect") or {}
        clips_data = raw.get("clips") or {}

        missing_general = {"input", "output_dir", "format", "combined_name"} - set(
            general_data
        )
        if missing_general:
            raise ConfigError(
                f"Missing general config values: {', '.join(sorted(missing_general))}"
            )

        input_path = cls._resolve_path(base_dir, general_data["input"])
        output_dir = cls._resolve_path(base_dir, general_data["output_dir"])
        audio_format = str(general_data["format"]).lower()
        combined_name = str(general_data["combined_name"])
        if "." not in Path(combined_name).name:
            combined_name = f"{combined_name}.{audio_format}"

        detect = DetectSettings(
            enabled=bool(detect_data.get("enabled", True)),
            noise_db=float(detect_data.get("noise_db", -30)),
            min_silence=float(detect_data.get("min_silence", 0.06)),
            pad=float(detect_data.get("pad", 0.07)),
        )

        clip_names = clips_data.get("names") or []
        clips = ClipSettings(
            count=int(clips_data.get("count", len(clip_names))),
            names=[str(name) for name in clip_names],
        )
        if clips.count <= 0:
            raise ConfigError("clips.count must be greater than zero.")
        if len(clips.names) < clips.count:
            raise ConfigError(
                f"Not enough clip names ({len(clips.names)}) for required count ({clips.count})."
            )

        if not input_path.exists():
            raise ConfigError(f"Input audio file not found: {input_path}")

        return cls(
            general=GeneralSettings(
                input_path=input_path,
                output_dir=output_dir,
                audio_format=audio_format,
                combined_name=combined_name,
            ),
            detect=detect,
            clips=clips,
        )

    @staticmethod
    def _resolve_path(base: Path, value: str) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = (base / path).resolve()
        return path


class ToneClipProcessor:
    def __init__(self, config: ProcessorConfig) -> None:
        self.config = config
        self.config.general.output_dir.mkdir(parents=True, exist_ok=True)
        self.clips_dir = self.config.general.output_dir / "clips"
        self.clips_dir.mkdir(parents=True, exist_ok=True)

    def run(self) -> dict:
        wav_path = self._convert_to_reference_wav()
        duration = self._probe_duration(wav_path)
        segments = (
            self._detect_segments(wav_path, duration)
            if self.config.detect.enabled
            else self._split_evenly(duration)
        )
        fitted_segments = self._fit_segments_to_target(segments)
        exports = self._export_segments(fitted_segments)
        combined_path = self._combine_exports(exports)
        return {
            "segments": fitted_segments,
            "exports": exports,
            "combined_path": combined_path,
            "duration": duration,
        }

    def _convert_to_reference_wav(self) -> Path:
        output_dir = self.config.general.output_dir
        wav_path = output_dir / f"{self.config.general.input_path.stem}_mono16k.wav"
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(self.config.general.input_path),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-acodec",
            "pcm_s16le",
            str(wav_path),
        ]
        self._run(cmd, capture_output=True)
        return wav_path

    def _probe_duration(self, path: Path) -> float:
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
        result = self._run(cmd, capture_output=True, text=True)
        try:
            return float(result.stdout.strip())
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Unable to parse media duration.") from exc

    def _detect_segments(self, wav_path: Path, duration: float) -> List[Tuple[float, float]]:
        cmd = [
            "ffmpeg",
            "-i",
            str(wav_path),
            "-af",
            f"silencedetect=noise={self.config.detect.noise_db}dB:d={self.config.detect.min_silence}",
            "-f",
            "null",
            "-",
        ]
        result = self._run(cmd, capture_output=True, text=True)
        starts, ends = self._parse_silence_log(result.stderr or "")
        silences = self._pair_silences(starts, ends, duration)
        speech_segments = self._silences_to_speech(silences, duration)
        padded = self._apply_padding(speech_segments, duration)
        if not padded:
            raise RuntimeError("No speech regions were detected with the current settings.")
        return padded

    def _split_evenly(self, duration: float) -> List[Tuple[float, float]]:
        segment_length = duration / self.config.clips.count
        segments: List[Tuple[float, float]] = []
        for idx in range(self.config.clips.count):
            start = idx * segment_length
            end = duration if idx == self.config.clips.count - 1 else (idx + 1) * segment_length
            segments.append((round(start, 5), round(end, 5)))
        return segments

    def _fit_segments_to_target(
        self, segments: Sequence[Tuple[float, float]]
    ) -> List[Tuple[float, float]]:
        if not segments:
            raise RuntimeError("No segments available to fit.")
        target = self.config.clips.count
        ordered = sorted(segments, key=lambda item: item[0])
        if len(ordered) == target:
            return list(ordered)
        if len(ordered) < target:
            raise RuntimeError(
                f"Detected only {len(ordered)} segments but need {target}. "
                "Increase noise threshold or reduce requested clips."
            )

        best = list(ordered)
        for gap in MERGE_GAPS_SECONDS:
            merged = self._merge_adjacent(best, gap)
            if len(merged) <= target:
                best = merged
                break
            best = merged
        if len(best) > target:
            best = best[:target]
        if len(best) < target:
            raise RuntimeError(
                f"Unable to reduce segments to exactly {target}; detected {len(best)} after merging."
            )
        return best

    def _merge_adjacent(
        self, segments: Sequence[Tuple[float, float]], gap_size: float
    ) -> List[Tuple[float, float]]:
        merged: List[Tuple[float, float]] = [segments[0]]
        for start, end in segments[1:]:
            last_start, last_end = merged[-1]
            if start - last_end <= gap_size:
                merged[-1] = (last_start, max(last_end, end))
            else:
                merged.append((start, end))
        return merged

    def _export_segments(
        self, segments: Sequence[Tuple[float, float]]
    ) -> List[dict]:
        exports = []
        fmt = self.config.general.audio_format
        codec_args = self._codec_args(fmt)
        for idx, (start, end) in enumerate(segments):
            clip_name = self.config.clips.names[idx]
            output_path = (
                self.clips_dir
                / f"{clip_name}.{fmt}"
            )
            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                str(self.config.general.input_path),
                "-ss",
                f"{start:.3f}",
                "-to",
                f"{end:.3f}",
            ]
            cmd.extend(codec_args)
            cmd.append(str(output_path))
            self._run(cmd, capture_output=True)
            exports.append(
                {
                    "name": clip_name,
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "duration": round(end - start, 3),
                    "path": output_path,
                }
            )
        return exports

    def _codec_args(self, fmt: str) -> list[str]:
        if fmt == "mp3":
            return ["-acodec", "libmp3lame", "-q:a", "2"]
        if fmt == "wav":
            return ["-acodec", "pcm_s16le"]
        if fmt == "flac":
            return ["-acodec", "flac"]
        return ["-acodec", "copy"]

    def _combine_exports(self, exports: Sequence[dict]) -> Path:
        combined_path = self.config.general.output_dir / self.config.general.combined_name
        if not exports:
            raise RuntimeError("No exports available to combine.")
        concat_file = self.config.general.output_dir / ".tone_concat.txt"
        try:
            with concat_file.open("w", encoding="utf-8") as fh:
                for export in exports:
                    escaped = export["path"].resolve().as_posix().replace("'", r"\'")
                    fh.write(f"file '{escaped}'\n")
            cmd = [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_file),
                "-c",
                "copy",
                str(combined_path),
            ]
            self._run(cmd, capture_output=True)
        finally:
            if concat_file.exists():
                concat_file.unlink()
        return combined_path

    def _apply_padding(
        self, segments: Iterable[Tuple[float, float]], duration: float
    ) -> List[Tuple[float, float]]:
        pad = self.config.detect.pad
        padded = []
        for start, end in segments:
            window_start = max(0.0, start - pad)
            window_end = min(duration, end + pad)
            if window_end - window_start >= MIN_SEGMENT_DURATION:
                padded.append((window_start, window_end))
        return padded

    def _silences_to_speech(
        self, silences: Iterable[Tuple[float, float]], duration: float
    ) -> List[Tuple[float, float]]:
        speech_segments = []
        previous_end = 0.0
        for start, end in silences:
            if start - previous_end > 1e-3:
                speech_segments.append((previous_end, start))
            previous_end = end
        if duration - previous_end > 1e-3:
            speech_segments.append((previous_end, duration))
        return speech_segments

    def _pair_silences(
        self, starts: Sequence[float], ends: Sequence[float], duration: float
    ) -> List[Tuple[float, float]]:
        events = [("start", value) for value in starts] + [
            ("end", value) for value in ends
        ]
        events.sort(key=lambda item: item[1])
        silences = []
        current_start: float | None = None
        for kind, timestamp in events:
            if kind == "start":
                if current_start is None:
                    current_start = timestamp
            else:
                if current_start is None:
                    current_start = 0.0
                silences.append((current_start, timestamp))
                current_start = None
        if current_start is not None:
            silences.append((current_start, duration))
        return silences

    def _parse_silence_log(self, log: str) -> Tuple[List[float], List[float]]:
        starts, ends = [], []
        for line in log.splitlines():
            if match := SILENCE_START_RE.search(line):
                starts.append(float(match.group(1)))
            if match := SILENCE_END_RE.search(line):
                ends.append(float(match.group(1)))
        return starts, ends

    def _run(
        self,
        cmd: Sequence[str],
        *,
        capture_output: bool = False,
        text: bool = False,
    ) -> subprocess.CompletedProcess:
        result = subprocess.run(
            cmd,
            capture_output=capture_output,
            text=text,
            check=False,
        )
        if result.returncode != 0:
            stdout = (result.stdout or "").strip() if capture_output else ""
            stderr = (result.stderr or "").strip() if capture_output else ""
            raise RuntimeError(
                f"Command failed ({' '.join(cmd)}):\nSTDOUT: {stdout}\nSTDERR: {stderr}"
            )
        return result


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Split tone audio into labeled clips.")
    parser.add_argument(
        "--config",
        default="config.toml",
        type=str,
        help="Path to the TOML config file (default: config.toml).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    config = ProcessorConfig.from_toml(Path(args.config).expanduser().resolve())
    processor = ToneClipProcessor(config)
    result = processor.run()
    print(f"Wrote {len(result['exports'])} clips to {config.general.output_dir}")
    print(f"Combined audio: {result['combined_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
