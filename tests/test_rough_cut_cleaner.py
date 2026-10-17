"""Unit and integration tests for Universal Defect Pruning & Assembly Rough-Cut Engine."""

from pathlib import Path

import cv2
import numpy as np
import pytest

from src.core.config import PipelineConfig
from src.core.types import CleanedSegment, DefectSpan, DefectType, SceneCut
from src.modules.audio_normalizer import AudioNormalizer
from src.modules.rough_cut_cleaner import RoughCutCleaner
from src.modules.timeline_builder import TimelineBuilder


@pytest.fixture
def make_synthetic_video():
    """Factory fixture to create test video clips with parameterized frame generators."""
    def _create(
        video_path: Path,
        duration_sec: float = 4.0,
        fps: float = 30.0,
        frame_generator=None,
    ) -> Path:
        w, h = 320, 240
        total_frames = int(round(duration_sec * fps))
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        out = cv2.VideoWriter(str(video_path), fourcc, fps, (w, h))

        for i in range(total_frames):
            t = i / fps
            if frame_generator:
                frame = frame_generator(i, t, w, h)
            else:
                # Default textured frame with slight pan
                frame = np.full((h, w, 3), 120, dtype=np.uint8)
                cv2.circle(frame, (50 + int(i * 0.5), 100), 30, (20, 20, 200), -1)
                cv2.putText(frame, f"F:{i}", (10, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
            out.write(frame)

        out.release()
        return video_path

    return _create


def test_rough_cut_cleaner_initialization():
    """Verify RoughCutCleaner configures correctly from PipelineConfig."""
    cfg = PipelineConfig(workflow_mode="rough_cut", min_take_duration_sec=1.8)
    cleaner = RoughCutCleaner(cfg)
    assert cleaner.config.workflow_mode == "rough_cut"
    assert cleaner.config.min_take_duration_sec == 1.8
    assert cleaner.config.enable_head_tail_trim is True


def test_head_tail_wobble_trimming(tmp_path: Path, make_synthetic_video):
    """Verify unstable start jerk in first 1.5s is clamped by head_prune_sec."""
    video_path = tmp_path / "test_wobble.mp4"

    def wobble_gen(i: int, t: float, w: int, h: int) -> np.ndarray:
        frame = np.full((h, w, 3), 100, dtype=np.uint8)
        # Unstable violent jerk in first 1.2s (frames 0 to 36)
        if t < 1.2:
            shift = (i % 2) * 80 - 40
            cv2.circle(frame, (160 + shift, 120), 40, (0, 255, 0), -1)
        else:
            # Steady slight movement
            cv2.circle(frame, (160 + int(t * 5), 120), 40, (0, 255, 0), -1)
        cv2.putText(frame, f"T:{t:.2f}", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
        return frame

    make_synthetic_video(video_path, duration_sec=5.0, fps=30.0, frame_generator=wobble_gen)

    scene = SceneCut(
        clip_path=video_path,
        start_time=0.0,
        end_time=5.0,
        start_frame=0,
        end_frame=150,
        duration=5.0,
    )

    cfg = PipelineConfig(
        workflow_mode="rough_cut",
        enable_head_tail_trim=True,
        head_prune_sec=0.8,
        min_take_duration_sec=1.5,
    )
    cleaner = RoughCutCleaner(cfg)
    segments = cleaner.inspect_scene(scene)

    assert len(segments) >= 1
    # Check that wobble was detected
    has_wobble_defect = any(
        d.defect_type == DefectType.HEAD_TAIL_WOBBLE
        for s in segments
        for d in s.defects_removed
    )
    assert has_wobble_defect is True
    # The first golden take start_time must be trimmed after the head wobble
    assert segments[0].start_time >= 0.7


def test_mid_clip_shake_bisection(tmp_path: Path, make_synthetic_video):
    """Verify severe camera shake bisects scene into two distinct golden takes."""
    video_path = tmp_path / "test_shake.mp4"

    # Pre-render a non-periodic, gradient-textured frame with distinct shapes
    base_frame = np.zeros((240, 320, 3), dtype=np.uint8)
    for y in range(240):
        for x in range(320):
            val = int((x / 320) * 150 + (y / 240) * 100)
            base_frame[y, x] = [val, val, val]
    cv2.circle(base_frame, (160, 120), 50, (255, 200, 50), -1)

    def shake_gen(i: int, t: float, w: int, h: int) -> np.ndarray:
        frame = base_frame.copy()
        # Violently shake / whip-pan the frame from 2.2s to 2.8s (>80px shift per frame)
        if 2.2 <= t <= 2.8:
            shift_x = float((i - 66) * 90.0)
            shift_y = float((i - 66) * 20.0)
            M = np.float32([[1, 0, shift_x], [0, 1, shift_y]])
            frame = cv2.warpAffine(frame, M, (w, h), borderMode=cv2.BORDER_REFLECT)
        return frame

    make_synthetic_video(video_path, duration_sec=5.5, fps=30.0, frame_generator=shake_gen)

    scene = SceneCut(
        clip_path=video_path,
        start_time=0.0,
        end_time=5.5,
        start_frame=0,
        end_frame=165,
        duration=5.5,
    )

    cfg = PipelineConfig(
        workflow_mode="rough_cut",
        enable_shake_pruning=True,
        max_shake_threshold=80.0,
        max_shake_min_duration=0.3,
        min_take_duration_sec=1.5,
    )
    cleaner = RoughCutCleaner(cfg)
    segments = cleaner.inspect_scene(scene)

    # Should be bisected into 2 golden segments: one before 2.2s, one after 2.8s
    assert len(segments) == 2
    take1, take2 = segments[0], segments[1]

    assert take1.end_time <= 2.3
    assert take1.duration >= 1.5

    assert take2.start_time >= 2.7
    assert take2.duration >= 1.5

    # Defects removed must record CAMERA_SHAKE
    all_defects = take1.defects_removed + take2.defects_removed
    assert any(d.defect_type == DefectType.CAMERA_SHAKE for d in all_defects)


def test_focus_hunting_blur_pruning(tmp_path: Path, make_synthetic_video):
    """Verify out-of-focus interval with Tenengrad sharpness drop > 65% is excised."""
    video_path = tmp_path / "test_focus.mp4"

    def focus_gen(i: int, t: float, w: int, h: int) -> np.ndarray:
        frame = np.full((h, w, 3), 100, dtype=np.uint8)
        # High contrast chessboard texture
        for y in range(0, h, 20):
            for x in range(0, w, 20):
                if ((x // 20) + (y // 20)) % 2 == 0:
                    frame[y:y+20, x:x+20] = 230

        # Heavy defocus blur between 2.0s and 2.8s
        if 2.0 <= t <= 2.8:
            frame = cv2.GaussianBlur(frame, (45, 45), 20)

        cv2.putText(frame, f"T:{t:.1f}", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
        return frame

    make_synthetic_video(video_path, duration_sec=5.0, fps=30.0, frame_generator=focus_gen)

    scene = SceneCut(
        clip_path=video_path,
        start_time=0.0,
        end_time=5.0,
        start_frame=0,
        end_frame=150,
        duration=5.0,
    )

    cfg = PipelineConfig(
        workflow_mode="rough_cut",
        enable_focus_guard=True,
        focus_drop_ratio=0.65,
        focus_drop_min_duration=0.5,
        min_take_duration_sec=1.5,
    )
    cleaner = RoughCutCleaner(cfg)
    segments = cleaner.inspect_scene(scene)

    assert len(segments) >= 1
    all_defects = [d for s in segments for d in s.defects_removed]
    assert any(d.defect_type == DefectType.DEFOCUS_BLUR for d in all_defects)


def test_lens_occlusion_pruning(tmp_path: Path, make_synthetic_video):
    """Verify lens occlusion (uniform darkness Y < 15) is pruned."""
    video_path = tmp_path / "test_occlusion.mp4"

    def occ_gen(i: int, t: float, w: int, h: int) -> np.ndarray:
        # Uniform pitch black for lens cap / finger occlusion between 2.0s and 2.6s
        if 2.0 <= t <= 2.6:
            return np.zeros((h, w, 3), dtype=np.uint8)

        frame = np.full((h, w, 3), 140, dtype=np.uint8)
        cv2.circle(frame, (160, 120), 40, (0, 200, 200), -1)
        return frame

    make_synthetic_video(video_path, duration_sec=4.5, fps=30.0, frame_generator=occ_gen)

    scene = SceneCut(
        clip_path=video_path,
        start_time=0.0,
        end_time=4.5,
        start_frame=0,
        end_frame=135,
        duration=4.5,
    )

    cfg = PipelineConfig(
        workflow_mode="rough_cut",
        enable_occlusion_guard=True,
        occlusion_area_ratio=0.40,
        occlusion_min_duration=0.4,
        min_take_duration_sec=1.5,
    )
    cleaner = RoughCutCleaner(cfg)
    segments = cleaner.inspect_scene(scene)

    assert len(segments) >= 1
    all_defects = [d for s in segments for d in s.defects_removed]
    assert any(d.defect_type == DefectType.LENS_OCCLUSION for d in all_defects)


def test_micro_take_clamping(tmp_path: Path, make_synthetic_video):
    """Verify segments shorter than min_take_duration_sec (1.5s) are eliminated."""
    video_path = tmp_path / "test_micro.mp4"
    make_synthetic_video(video_path, duration_sec=1.2, fps=30.0)

    scene = SceneCut(
        clip_path=video_path,
        start_time=0.0,
        end_time=1.2,
        start_frame=0,
        end_frame=36,
        duration=1.2,
    )

    cfg = PipelineConfig(
        workflow_mode="rough_cut",
        min_take_duration_sec=1.5,
    )
    cleaner = RoughCutCleaner(cfg)
    segments = cleaner.inspect_scene(scene)

    # 1.2s < 1.5s min_take -> discarded
    assert len(segments) == 0


def test_clean_batch_chronological_ordering(tmp_path: Path, make_synthetic_video):
    """Verify clean_batch returns golden takes in strict chronological order."""
    vid_a = tmp_path / "clip_a.mp4"
    vid_b = tmp_path / "clip_b.mp4"

    make_synthetic_video(vid_a, duration_sec=3.0, fps=30.0)
    make_synthetic_video(vid_b, duration_sec=3.0, fps=30.0)

    scenes = [
        SceneCut(clip_path=vid_b, start_time=1.5, end_time=3.0, start_frame=45, end_frame=90, duration=1.5),
        SceneCut(clip_path=vid_a, start_time=0.0, end_time=1.5, start_frame=0, end_frame=45, duration=1.5),
        SceneCut(clip_path=vid_b, start_time=0.0, end_time=1.5, start_frame=0, end_frame=45, duration=1.5),
        SceneCut(clip_path=vid_a, start_time=1.5, end_time=3.0, start_frame=45, end_frame=90, duration=1.5),
    ]

    cfg = PipelineConfig(workflow_mode="rough_cut", min_take_duration_sec=1.0)
    cleaner = RoughCutCleaner(cfg)
    golden_takes = cleaner.clean_batch(scenes)

    assert len(golden_takes) == 4
    # Check strict alphabetical/file + timestamp order
    assert golden_takes[0].clip_path == vid_a and golden_takes[0].start_time == 0.0
    assert golden_takes[1].clip_path == vid_a and golden_takes[1].start_time == 1.5
    assert golden_takes[2].clip_path == vid_b and golden_takes[2].start_time == 0.0
    assert golden_takes[3].clip_path == vid_b and golden_takes[3].start_time == 1.5


def test_build_rough_cut_timeline_and_export(tmp_path: Path, make_synthetic_video):
    """Verify TimelineBuilder.build_rough_cut_timeline generates V1, A1, V2 and exports FCPXML."""
    vid = tmp_path / "take_seq.mp4"
    make_synthetic_video(vid, duration_sec=4.0, fps=30.0)

    scene = SceneCut(clip_path=vid, start_time=0.0, end_time=4.0, start_frame=0, end_frame=120, duration=4.0)
    seg1 = CleanedSegment(
        scene=scene,
        clip_path=vid,
        start_time=0.0,
        end_time=2.0,
        duration=2.0,
        source_start_time=0.0,
        source_end_time=2.0,
        defects_removed=[DefectSpan(start_time=0.0, end_time=0.5, defect_type=DefectType.HEAD_TAIL_WOBBLE)],
    )
    seg2 = CleanedSegment(
        scene=scene,
        clip_path=vid,
        start_time=2.0,
        end_time=4.0,
        duration=2.0,
        source_start_time=2.0,
        source_end_time=4.0,
        defects_removed=[DefectSpan(start_time=2.0, end_time=2.3, defect_type=DefectType.CAMERA_SHAKE)],
    )

    cfg = PipelineConfig(workflow_mode="rough_cut", fps=30.0)
    builder = TimelineBuilder(cfg)

    timeline = builder.build_rough_cut_timeline([seg1, seg2], timeline_name="RoughCut_Test", fps=30.0)

    assert timeline is not None
    assert len(timeline.tracks) == 3

    v1_track = timeline.tracks[0]
    review_track = timeline.tracks[1]
    a1_track = timeline.tracks[2]

    assert v1_track.name == "V1"
    assert a1_track.name == "A1"
    assert "V2" in review_track.name

    # Both V1 and A1 should have 2 clips with matching duration
    assert len(v1_track) == 2
    assert len(a1_track) == 2

    # Verify review markers on video clips
    assert len(v1_track[0].markers) == 1
    assert "HEAD_TAIL_WOBBLE" in v1_track[0].markers[0].name
    assert len(v1_track[1].markers) == 1
    assert "CAMERA_SHAKE" in v1_track[1].markers[0].name

    # Export to multi-NLE formats without soundtrack
    export_dir = tmp_path / "nle_exports"
    res = builder.export_all(timeline, export_dir, base_name="rough_cut_test")

    assert res.clip_count == 2
    assert res.total_duration >= 3.9
    assert res.otio_path is not None and res.otio_path.exists()
    assert res.premiere_xml_path is not None and res.premiere_xml_path.exists()


def test_audio_normalizer_condition_camera_audio(tmp_path: Path):
    """Verify AudioNormalizer conditions camera audio without errors."""
    audio_path = tmp_path / "mic_test.wav"
    sr = 48000
    # 1-second sine wave with 50Hz rumble
    t = np.linspace(0, 1.0, sr, endpoint=False)
    rumble = 0.6 * np.sin(2 * np.pi * 50 * t)
    voice = 0.3 * np.sin(2 * np.pi * 1000 * t)
    audio_data = (rumble + voice) * 32767
    audio_data = audio_data.astype(np.int16)

    # Write WAV file using scipy or wave
    import wave
    with wave.open(str(audio_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(audio_data.tobytes())

    normalizer = AudioNormalizer(cache_dir=tmp_path / "audio_cache")
    out_path, applied = normalizer.inspect_and_condition_camera_audio(audio_path, force_highpass=True)

    assert out_path.exists()
    assert applied is True
    assert out_path.suffix == ".wav"
