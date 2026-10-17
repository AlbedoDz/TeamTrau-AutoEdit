"""Audio rhythm, downbeat, onset tracking, and structural energy-sync module using Librosa."""

import hashlib
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf

from src.core.config import PipelineConfig
from src.core.ffmpeg_utils import run_tool
from src.core.types import BeatMarker, SongSection, SongSectionType

logger = logging.getLogger(__name__)


@dataclass
class AudioAnalysisResult:
    """Comprehensive analysis of the musical soundtrack."""

    tempo_bpm: float
    duration: float
    beats: list[BeatMarker]
    cut_timestamps: list[float]
    sample_rate: int
    sections: list[SongSection] = field(default_factory=list)
    extracted_audio_path: Path | None = None


class AudioAnalyzer:
    """Analyzes musical structure, tracking downbeats, tempo, sections, and cut points."""

    def __init__(self, config: PipelineConfig) -> None:
        self.config = config

    def _load_audio(self, audio_path: Path) -> tuple[np.ndarray, int]:
        """Safely load audio waveform via librosa or soundfile fallback."""
        if not audio_path.is_file():
            raise FileNotFoundError(f"Soundtrack file not found: {audio_path}")
        audio_path = self._decode_to_analysis_wav(audio_path)
        try:
            y, sr = librosa.load(str(audio_path), sr=self.config.audio_sample_rate, mono=True)
        except Exception:
            data, sr_sf = sf.read(str(audio_path))
            if data.ndim > 1:
                data = np.mean(data, axis=1)
            if sr_sf != self.config.audio_sample_rate:
                y = librosa.resample(data, orig_sr=sr_sf, target_sr=self.config.audio_sample_rate)
                sr = self.config.audio_sample_rate
            else:
                y = data
                sr = sr_sf
        return y, sr

    def _decode_to_analysis_wav(self, audio_path: Path) -> Path:
        """Decode compressed audio (mp3/m4a/aac...) once with ffmpeg into a cached analysis WAV.

        Avoids librosa's slow, deprecated audioread fallback for formats libsndfile can't read.
        """
        if audio_path.suffix.lower() in (".wav", ".flac", ".ogg", ".aiff", ".aif"):
            return audio_path
        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            return audio_path
        st = audio_path.stat()
        key = f"{audio_path.resolve()}|{st.st_size}|{st.st_mtime_ns}|{self.config.audio_sample_rate}"
        h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
        out = Path(self.config.cache_dir) / "analysis_audio" / f"{audio_path.stem}_{h}.wav"
        if out.is_file() and out.stat().st_size > 0:
            return out
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.stem + ".part.wav")
        cmd = [
            ffmpeg_bin, "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
            "-i", str(audio_path), "-vn", "-ac", "1", "-ar", str(self.config.audio_sample_rate),
            "-c:a", "pcm_s16le", str(tmp),
        ]
        try:
            res = run_tool(cmd, timeout=600)
            if res.returncode == 0 and tmp.is_file():
                os.replace(tmp, out)
                return out
        except (subprocess.SubprocessError, OSError) as exc:
            logger.debug("ffmpeg analysis decode failed for %s: %s", audio_path, exc)
        finally:
            tmp.unlink(missing_ok=True)
        return audio_path

    @staticmethod
    def _cut_native_section(
        src: Path, dst: Path, start: float, duration: float, fade_in: float, fade_out: float
    ) -> bool:
        """Cut [start, start+duration) from the source at native rate/channels with fades."""
        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            return False
        filters = []
        if fade_in > 0:
            filters.append(f"afade=t=in:st=0:d={fade_in:.3f}:curve=hsin")
        if fade_out > 0:
            filters.append(
                f"afade=t=out:st={max(0.0, duration - fade_out):.3f}:d={fade_out:.3f}:curve=hsin"
            )
        tmp = dst.with_name(dst.stem + ".part.wav")
        cmd = [
            ffmpeg_bin, "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
            "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(src),
            "-vn", "-map_metadata", "-1",
        ]
        if filters:
            cmd += ["-af", ",".join(filters)]
        cmd += ["-c:a", "pcm_s24le", str(tmp)]
        try:
            res = run_tool(cmd, timeout=600)
            if res.returncode != 0 or not tmp.is_file():
                logger.warning("Native soundtrack cut failed: %s", (res.stderr or "")[-300:])
                return False
            os.replace(tmp, dst)
            return True
        except (subprocess.SubprocessError, OSError) as exc:
            logger.warning("Native soundtrack cut failed: %s", exc)
            return False
        finally:
            tmp.unlink(missing_ok=True)

    def compute_energy_profile(
        self, y: np.ndarray, sr: int, hop_length: int = 512
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Compute continuous normalized RMS energy, spectral novelty, chroma novelty, and smoothed energy profile.

        Returns:
            (times, smooth_energy, norm_rms, norm_onset, chroma)
        """
        frame_len = 2048
        rms = librosa.feature.rms(y=y, frame_length=frame_len, hop_length=hop_length)[0]
        onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop_length)

        # 12-dimensional pitch-class Chroma profile
        try:
            chroma = librosa.feature.chroma_cens(y=y, sr=sr, hop_length=hop_length)
        except Exception:
            chroma = librosa.feature.chroma_stft(y=y, sr=sr, hop_length=hop_length)

        min_len = min(len(rms), len(onset_env), chroma.shape[1])
        rms = rms[:min_len]
        onset_env = onset_env[:min_len]
        chroma = chroma[:, :min_len]
        times = librosa.frames_to_time(np.arange(min_len), sr=sr, hop_length=hop_length)

        # Normalize to [0.0, 1.0]
        rms_range = float(rms.max() - rms.min())
        norm_rms = (rms - rms.min()) / (rms_range if rms_range > 1e-8 else 1.0)

        onset_range = float(onset_env.max() - onset_env.min())
        norm_onset = (onset_env - onset_env.min()) / (onset_range if onset_range > 1e-8 else 1.0)

        # Harmonic novelty from chroma flux
        if chroma.shape[1] > 1:
            chroma_flux = np.sqrt(np.sum(np.diff(chroma, axis=1) ** 2, axis=0))
            chroma_novelty = np.pad(chroma_flux, (1, 0), mode="edge")
        else:
            chroma_novelty = np.zeros(min_len)

        chroma_range = float(chroma_novelty.max() - chroma_novelty.min())
        norm_chroma = (chroma_novelty - chroma_novelty.min()) / (
            chroma_range if chroma_range > 1e-8 else 1.0
        )

        # Multi-feature energy blend: amplitude intensity + rhythmic attack + harmonic change
        raw_energy = 0.55 * norm_rms + 0.30 * norm_onset + 0.15 * norm_chroma

        # Moving average smoothing (0.75-1.5s window) to isolate macro structural energy
        win_size = max(5, int(sr / hop_length * 1.0))
        if win_size % 2 == 0:
            win_size += 1
        kernel = np.ones(win_size) / win_size
        smooth_energy = np.convolve(raw_energy, kernel, mode="same")

        return times, smooth_energy, norm_rms, norm_onset, chroma

    def detect_song_sections(
        self, audio_path: Path, y: np.ndarray | None = None, sr: int | None = None
    ) -> list[SongSection]:
        """Classify the track into INTRO, BUILDUP, DROP, and OUTRO sections based on energy dynamics."""
        if y is None or sr is None:
            y, sr = self._load_audio(audio_path)

        duration = float(len(y)) / float(sr)
        if duration <= 0:
            return []

        times, smooth_energy, _, _, chroma = self.compute_energy_profile(y, sr)
        if len(times) == 0:
            return [
                SongSection(
                    section_type=SongSectionType.DROP,
                    start_time=0.0,
                    end_time=duration,
                    duration=duration,
                    avg_energy=1.0,
                    chroma_profile=[0.0] * 12,
                    is_climax=True,
                )
            ]

        def get_chroma_vec(t_start: float, t_end: float) -> list[float]:
            c_mask = (times >= t_start) & (times <= t_end)
            if np.any(c_mask) and chroma.shape[1] > 0:
                return [float(x) for x in np.mean(chroma[:, c_mask], axis=1)]
            return [0.0] * 12

        # For very short audio (< 4.0s), partition proportionally
        if duration < 4.0:
            p1, p2, p3 = duration * 0.20, duration * 0.45, duration * 0.80
            return [
                SongSection(
                    section_type=SongSectionType.INTRO,
                    start_time=0.0,
                    end_time=p1,
                    duration=p1,
                    avg_energy=float(np.mean(smooth_energy[times <= p1])) if any(times <= p1) else 0.2,
                    chroma_profile=get_chroma_vec(0.0, p1),
                    is_climax=False,
                ),
                SongSection(
                    section_type=SongSectionType.BUILDUP,
                    start_time=p1,
                    end_time=p2,
                    duration=p2 - p1,
                    avg_energy=float(np.mean(smooth_energy[(times > p1) & (times <= p2)])) if any((times > p1) & (times <= p2)) else 0.5,
                    chroma_profile=get_chroma_vec(p1, p2),
                    is_climax=False,
                ),
                SongSection(
                    section_type=SongSectionType.DROP,
                    start_time=p2,
                    end_time=p3,
                    duration=p3 - p2,
                    avg_energy=float(np.mean(smooth_energy[(times > p2) & (times <= p3)])) if any((times > p2) & (times <= p3)) else 0.9,
                    chroma_profile=get_chroma_vec(p2, p3),
                    is_climax=True,
                ),
                SongSection(
                    section_type=SongSectionType.OUTRO,
                    start_time=p3,
                    end_time=duration,
                    duration=duration - p3,
                    avg_energy=float(np.mean(smooth_energy[times > p3])) if any(times > p3) else 0.3,
                    chroma_profile=get_chroma_vec(p3, duration),
                    is_climax=False,
                ),
            ]

        # Full structural segmentation for realistic audio tracks
        e_max = float(np.max(smooth_energy))
        e_min = float(np.min(smooth_energy))
        e_mean = float(np.mean(smooth_energy))

        # Peak energy threshold for DROP
        drop_threshold = max(0.40, e_mean + 0.15 * (e_max - e_mean))

        # Find primary drop region around the global maximum energy index
        peak_idx = int(np.argmax(smooth_energy))

        # Expand outwards from peak while energy remains high
        left_idx = peak_idx
        while left_idx > 0 and (smooth_energy[left_idx] >= drop_threshold or times[peak_idx] - times[left_idx] < 2.0):
            left_idx -= 1
        right_idx = peak_idx
        while right_idx < len(smooth_energy) - 1 and (smooth_energy[right_idx] >= drop_threshold or times[right_idx] - times[peak_idx] < 2.0):
            right_idx += 1

        t_drop_start = float(times[left_idx])
        t_drop_end = float(times[right_idx])

        # Enforce reasonable boundaries for DROP so INTRO/BUILDUP and OUTRO have room
        min_start_margin = max(1.5, duration * 0.10)
        if t_drop_start < min_start_margin:
            t_drop_start = min_start_margin

        min_end_margin = duration - max(1.5, duration * 0.10)
        if t_drop_end > min_end_margin:
            t_drop_end = min_end_margin

        if t_drop_end <= t_drop_start:
            t_drop_end = min(duration - 1.0, t_drop_start + max(2.0, duration * 0.25))

        # Pre-drop segmentation: INTRO vs BUILDUP
        pre_drop_span = t_drop_start
        pre_mask = times < (t_drop_start * 0.8)
        if np.any(pre_mask):
            pre_min_idx = int(np.argmin(smooth_energy[pre_mask]))
            t_buildup_start = max(1.0, float(times[pre_min_idx]))
        else:
            t_buildup_start = pre_drop_span * 0.5

        if t_buildup_start >= t_drop_start - 0.5:
            t_buildup_start = max(0.5, t_drop_start * 0.5)

        sections: list[SongSection] = []

        # 1. INTRO
        intro_mask = (times >= 0.0) & (times <= t_buildup_start)
        intro_energy = float(np.mean(smooth_energy[intro_mask])) if np.any(intro_mask) else e_min
        sections.append(
            SongSection(
                section_type=SongSectionType.INTRO,
                start_time=0.0,
                end_time=t_buildup_start,
                duration=t_buildup_start,
                avg_energy=intro_energy,
                chroma_profile=get_chroma_vec(0.0, t_buildup_start),
                is_climax=False,
            )
        )

        # 2. BUILDUP
        buildup_mask = (times >= t_buildup_start) & (times <= t_drop_start)
        buildup_energy = float(np.mean(smooth_energy[buildup_mask])) if np.any(buildup_mask) else 0.5 * (e_min + e_max)
        sections.append(
            SongSection(
                section_type=SongSectionType.BUILDUP,
                start_time=t_buildup_start,
                end_time=t_drop_start,
                duration=t_drop_start - t_buildup_start,
                avg_energy=buildup_energy,
                chroma_profile=get_chroma_vec(t_buildup_start, t_drop_start),
                is_climax=False,
            )
        )

        # 3. DROP
        drop_mask_sec = (times >= t_drop_start) & (times <= t_drop_end)
        drop_energy = float(np.mean(smooth_energy[drop_mask_sec])) if np.any(drop_mask_sec) else e_max
        sections.append(
            SongSection(
                section_type=SongSectionType.DROP,
                start_time=t_drop_start,
                end_time=t_drop_end,
                duration=t_drop_end - t_drop_start,
                avg_energy=drop_energy,
                chroma_profile=get_chroma_vec(t_drop_start, t_drop_end),
                is_climax=True,
            )
        )

        # 4. OUTRO
        outro_mask = (times >= t_drop_end) & (times <= duration)
        outro_energy = float(np.mean(smooth_energy[outro_mask])) if np.any(outro_mask) else e_min
        sections.append(
            SongSection(
                section_type=SongSectionType.OUTRO,
                start_time=t_drop_end,
                end_time=duration,
                duration=duration - t_drop_end,
                avg_energy=outro_energy,
                chroma_profile=get_chroma_vec(t_drop_end, duration),
                is_climax=False,
            )
        )

        logger.info(
            "Detected song structure: INTRO(%.1fs-%.1fs), BUILDUP(%.1fs-%.1fs), DROP(%.1fs-%.1fs, E=%.2f), OUTRO(%.1fs-%.1fs)",
            sections[0].start_time, sections[0].end_time,
            sections[1].start_time, sections[1].end_time,
            sections[2].start_time, sections[2].end_time, sections[2].avg_energy,
            sections[3].start_time, sections[3].end_time,
        )
        return sections

    def extract_coherent_section(
        self,
        audio_path: Path,
        target_duration: float,
        y: np.ndarray | None = None,
        sr: int | None = None,
        sections: list[SongSection] | None = None,
        cut_timestamps: list[float] | None = None,
    ) -> tuple[Path, float, float, list[SongSection], list[float]]:
        """Extract optimal cohesive musical segment of target_duration with parametric fade-out.

        Returns:
            (extracted_file_path, start_time, end_time, adjusted_sections, adjusted_cuts)
        """
        if y is None or sr is None:
            y, sr = self._load_audio(audio_path)

        duration = float(len(y)) / float(sr)
        if target_duration >= duration or target_duration <= 0:
            sec = sections or self.detect_song_sections(audio_path, y, sr)
            cuts = cut_timestamps or [0.0, duration]
            return audio_path, 0.0, duration, sec, cuts

        if sections is None:
            sections = self.detect_song_sections(audio_path, y, sr)

        times, smooth_energy, _, _, _ = self.compute_energy_profile(y, sr)

        drop_sec = next((s for s in sections if s.section_type == SongSectionType.DROP), None)
        drop_start = drop_sec.start_time if drop_sec else duration * 0.4
        drop_end = drop_sec.end_time if drop_sec else duration * 0.8
        drop_dur = drop_end - drop_start

        candidate_starts: list[float] = [0.0]
        for s in sections:
            if s.start_time <= (duration - target_duration):
                candidate_starts.append(s.start_time)

        if target_duration > drop_dur:
            lead_in = (target_duration - drop_dur) * 0.4
            opt_start = max(0.0, min(duration - target_duration, drop_start - lead_in))
            candidate_starts.append(opt_start)
        else:
            candidate_starts.append(min(duration - target_duration, drop_start))

        best_start = 0.0
        best_score = -1e9

        for c_start in candidate_starts:
            c_end = c_start + target_duration
            if c_end > duration:
                continue

            win_mask = (times >= c_start) & (times <= c_end)
            win_energy = float(np.mean(smooth_energy[win_mask])) if np.any(win_mask) else 0.0

            drop_overlap = max(0.0, min(c_end, drop_end) - max(c_start, drop_start))
            drop_ratio = drop_overlap / min(target_duration, drop_dur + 1e-5)

            flow_bonus = 0.0
            buildup_sec = next((s for s in sections if s.section_type == SongSectionType.BUILDUP), None)
            if buildup_sec and (c_start <= buildup_sec.end_time) and (c_end >= drop_start):
                flow_bonus = 0.4

            score = win_energy + 0.8 * drop_ratio + flow_bonus
            if score > best_score:
                best_score = score
                best_start = c_start

        best_end = best_start + target_duration
        logger.info(
            "Selected optimal musical window: [%.2fs - %.2fs] (score: %.3f)",
            best_start,
            best_end,
            best_score,
        )

        # Slice audio samples
        start_sample = int(round(best_start * sr))
        end_sample = min(len(y), int(round(best_end * sr)))
        y_slice = y[start_sample:end_sample].copy()

        # Apply parametric cosine fade-out
        fade_sec = min(self.config.audio_fade_out_sec, target_duration * 0.15)
        fade_samples = max(1, int(round(fade_sec * sr)))
        if len(y_slice) > fade_samples:
            fade_out_curve = 0.5 * (1.0 + np.cos(np.linspace(0, np.pi, fade_samples)))
            y_slice[-fade_samples:] *= fade_out_curve.astype(y_slice.dtype)

        # Apply subtle fade-in if not starting at 0
        if best_start > 0.2:
            fade_in_sec = min(0.3, target_duration * 0.05)
            fade_in_samples = max(1, int(round(fade_in_sec * sr)))
            if len(y_slice) > fade_in_samples:
                fade_in_curve = 0.5 * (1.0 - np.cos(np.linspace(0, np.pi, fade_in_samples)))
                y_slice[:fade_in_samples] *= fade_in_curve.astype(y_slice.dtype)

        # Save to disk. Cut the ORIGINAL file with ffmpeg so the deliverable keeps the
        # source sample rate and stereo image (y_slice is the mono 22 kHz analysis buffer).
        output_file = (
            self.config.cache_dir
            / f"soundtrack_{audio_path.stem}_{int(best_start * 1000)}_{int(target_duration * 1000)}ms.wav"
        )
        output_file.parent.mkdir(parents=True, exist_ok=True)
        fade_in = min(0.3, target_duration * 0.05) if best_start > 0.2 else 0.0
        if not self._cut_native_section(
            audio_path, output_file, best_start, target_duration, fade_in, fade_sec
        ):
            sf.write(str(output_file), y_slice, sr)
        logger.info("Exported decoupled & faded soundtrack to: %s", output_file.name)

        # Re-zero adjusted sections
        adj_sections: list[SongSection] = []
        for s in sections:
            s_start = max(0.0, s.start_time - best_start)
            s_end = min(target_duration, s.end_time - best_start)
            if s_end > s_start:
                adj_sections.append(
                    SongSection(
                        section_type=s.section_type,
                        start_time=s_start,
                        end_time=s_end,
                        duration=s_end - s_start,
                        avg_energy=s.avg_energy,
                    )
                )

        # Re-zero adjusted cuts
        adj_cuts: list[float] = []
        if cut_timestamps:
            for c in cut_timestamps:
                if best_start <= c <= best_end:
                    adj_cuts.append(c - best_start)

        if not adj_cuts or adj_cuts[0] > 0.1:
            adj_cuts.insert(0, 0.0)
        if adj_cuts[-1] < (target_duration - 0.1):
            adj_cuts.append(target_duration)

        return output_file, best_start, best_end, adj_sections, adj_cuts

    def filter_beats_by_structure(
        self,
        markers: list[BeatMarker],
        sections: list[SongSection],
    ) -> list[BeatMarker]:
        """Apply structure-aware beat filtering: rapid cuts in DROP, relaxed cuts elsewhere."""
        if not markers:
            return []

        def get_min_interval_at(timestamp: float) -> float:
            for s in sections:
                if s.start_time <= timestamp <= s.end_time:
                    if s.is_climax or s.section_type == SongSectionType.DROP:
                        return self.config.climax_min_beat_interval
                    return self.config.standard_min_beat_interval
            return self.config.standard_min_beat_interval

        filtered: list[BeatMarker] = [markers[0]]
        for candidate in markers[1:]:
            last_timestamp = filtered[-1].timestamp
            req_interval = get_min_interval_at(candidate.timestamp)
            interval = candidate.timestamp - last_timestamp

            if interval >= req_interval:
                filtered.append(candidate)
            elif candidate.is_downbeat and not filtered[-1].is_downbeat:
                filtered[-1] = candidate

        return filtered

    def solve_footage_driven_duration(
        self,
        available_footage_dur: float,
        downbeats: list[float],
        full_duration: float,
    ) -> float:
        """Snap target duration to the nearest musical downbeat or phrase boundary based on available footage."""
        if available_footage_dur >= full_duration or available_footage_dur <= 0:
            return full_duration

        # Filter downbeats (>= 3.0s up to full duration)
        valid_downbeats = [db for db in downbeats if 3.0 <= db <= full_duration]
        if not valid_downbeats:
            valid_downbeats = [db for db in downbeats if 0.0 < db <= full_duration]
        if not valid_downbeats:
            return min(full_duration, max(3.0, available_footage_dur))

        # Find closest downbeat
        closest_db = min(valid_downbeats, key=lambda db: abs(db - available_footage_dur))
        logger.info(
            "Footage-driven duration solved: available=%.2fs snapped to downbeat=%.2fs",
            available_footage_dur,
            closest_db,
        )
        return float(closest_db)

    def analyze(
        self,
        audio_path: Path,
        target_duration: float | None = None,
        available_footage_duration: float | None = None,
        duration_mode: str | None = None,
    ) -> AudioAnalysisResult:
        """Load audio file and extract rhythmic pulse, downbeats, sections, and cut intervals."""
        if not audio_path.is_file():
            raise FileNotFoundError(f"Soundtrack file not found: {audio_path}")

        logger.info("Loading soundtrack: %s", audio_path.name)
        y, sr = self._load_audio(audio_path)
        duration = float(len(y)) / float(sr)

        # Detect song structure sections (INTRO, BUILDUP, DROP, OUTRO)
        sections = self.detect_song_sections(audio_path, y, sr)

        # Compute dynamic onset strength envelope
        onset_env = librosa.onset.onset_strength(y=y, sr=sr)

        # Estimate tempo and beat positions
        tempo_val, beat_frames = librosa.beat.beat_track(
            y=y, sr=sr, onset_envelope=onset_env, trim=False
        )

        if isinstance(tempo_val, np.ndarray):
            tempo_bpm = float(tempo_val[0]) if tempo_val.size > 0 else 120.0
        else:
            tempo_bpm = float(tempo_val)

        beat_times = librosa.frames_to_time(beat_frames, sr=sr)

        # Calculate beat strengths from onset envelope
        max_frame = len(onset_env) - 1
        beat_strengths: list[float] = []
        for bf in beat_frames:
            f_idx = min(int(bf), max_frame)
            beat_strengths.append(float(onset_env[f_idx]))

        max_strength = max(beat_strengths) if beat_strengths else 1.0
        norm_strengths = [s / (max_strength + 1e-8) for s in beat_strengths]

        # Downbeat identification: in 4/4 meter, phase tracking based on max strength
        meter = 4
        best_phase = 0
        best_phase_sum = -1.0
        for phase in range(min(meter, len(norm_strengths))):
            phase_sum = sum(norm_strengths[phase::meter])
            if phase_sum > best_phase_sum:
                best_phase_sum = phase_sum
                best_phase = phase

        raw_markers: list[BeatMarker] = []
        for i, (bt, bf, st) in enumerate(zip(beat_times, beat_frames, norm_strengths)):
            is_downbeat = (i % meter) == best_phase
            raw_markers.append(
                BeatMarker(
                    timestamp=float(bt),
                    frame_idx=int(bf),
                    strength=st,
                    is_downbeat=is_downbeat,
                )
            )

        # Structure-aware dynamic beat filtering: 0.8s in Intro/Buildup/Outro, 0.4s in Drop
        if self.config.enable_structural_sync and sections:
            filtered_markers = self.filter_beats_by_structure(raw_markers, sections)
        else:
            filtered_markers = self._filter_micro_cuts(raw_markers, self.config.min_beat_interval)

        cut_timestamps = [m.timestamp for m in filtered_markers]

        # Ensure cut sequence starts at 0.0
        if not cut_timestamps or cut_timestamps[0] > 0.1:
            cut_timestamps.insert(0, 0.0)

        # Ensure cut sequence extends up to duration
        if cut_timestamps and cut_timestamps[-1] < (duration - 0.5):
            cut_timestamps.append(duration)

        # Resolve duration mode & target duration
        mode = duration_mode or self.config.duration_mode
        downbeats = [m.timestamp for m in raw_markers if m.is_downbeat]

        if (
            mode == "footage_driven"
            and available_footage_duration is not None
            and available_footage_duration > 0
        ):
            target_dur = self.solve_footage_driven_duration(
                available_footage_duration, downbeats, duration
            )
        elif mode == "full_track":
            target_dur = None
        else:  # "fixed" or default
            target_dur = target_duration if target_duration is not None else self.config.target_duration

        extracted_audio_path = None
        if target_dur is not None and target_dur < (duration - 1.0):
            (
                extracted_audio_path,
                start_t,
                end_t,
                sections,
                cut_timestamps,
            ) = self.extract_coherent_section(
                audio_path=audio_path,
                target_duration=target_dur,
                y=y,
                sr=sr,
                sections=sections,
                cut_timestamps=cut_timestamps,
            )
            duration = target_dur
            filtered_markers = [
                BeatMarker(
                    timestamp=float(m.timestamp - start_t),
                    frame_idx=int(round((m.timestamp - start_t) * sr)),
                    strength=m.strength,
                    is_downbeat=m.is_downbeat,
                )
                for m in filtered_markers
                if start_t <= m.timestamp <= end_t
            ]

        logger.info(
            "Analyzed audio: BPM=%.1f, raw beats=%d, cuts=%d, duration=%.2fs, mode=%s, decoupled=%s",
            tempo_bpm,
            len(raw_markers),
            len(cut_timestamps),
            duration,
            mode,
            "YES" if extracted_audio_path else "NO",
        )

        return AudioAnalysisResult(
            tempo_bpm=tempo_bpm,
            duration=duration,
            beats=filtered_markers,
            cut_timestamps=cut_timestamps,
            sample_rate=sr,
            sections=sections,
            extracted_audio_path=extracted_audio_path,
        )

    def _filter_micro_cuts(
        self, markers: list[BeatMarker], min_interval: float
    ) -> list[BeatMarker]:
        """Prune beat markers that are too close (< min_interval seconds).

        Preserves downbeats preferentially when dropping adjacent beats.
        """
        if not markers:
            return []

        filtered: list[BeatMarker] = [markers[0]]
        for candidate in markers[1:]:
            last_timestamp = filtered[-1].timestamp
            interval = candidate.timestamp - last_timestamp

            if interval >= min_interval:
                filtered.append(candidate)
            elif candidate.is_downbeat and not filtered[-1].is_downbeat:
                filtered[-1] = candidate

        return filtered

