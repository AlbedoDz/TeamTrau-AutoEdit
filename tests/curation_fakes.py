"""Fakes for curation tests: an adapter that records keyword calls and returns canned results."""

from pathlib import Path

from src.core.types import EditClip, EditTimeline, PipelineExecutionResult, SceneCut, ScoredScene


class FakeHandle:
    def __init__(self, result):
        self._r = result

    def wait(self, timeout=None):
        return True

    @property
    def result(self):
        return self._r


class FakeAdapter:
    def __init__(self, *results):
        self.results = list(results)
        self.calls: list[tuple[str, dict]] = []

    def submit_job(self, **kw):
        self.calls.append(("job", kw))
        return FakeHandle(self.results.pop(0))

    def submit_deliver_job(self, **kw):
        self.calls.append(("deliver", kw))
        return FakeHandle(self.results.pop(0))


def scene(i: int, role: str | None, dur: float = 4.0, q: float = 0.5) -> ScoredScene:
    s = float(i * 10)
    sc = SceneCut(clip_path=Path("c.mp4"), start_time=s, end_time=s + dur,
                  start_frame=int(s * 30), end_frame=int((s + dur) * 30), duration=dur)
    md = {"content_role": role} if role else {}
    return ScoredScene(scene=sc, sharpness_score=q, aesthetic_score=q, composite_score=q,
                       metadata=md)


def timeline(name: str = "m", kind: str = "music") -> EditTimeline:
    return EditTimeline(name=name, fps=30.0, canvas_width=1920, canvas_height=1080,
                        clips=[EditClip(source_path=Path("c.mp4"), source_in=0.0, frames=60,
                                        available=2.0)], kind=kind)


def pipeline_result(scenes, timelines=None, success=True, error=None):
    return PipelineExecutionResult(
        success=success, total_duration=0.0, clip_count=len(scenes),
        scenes_detected=len(scenes), audio_bpm=0.0, sections_detected=0, fcpxml_path=None,
        premiere_xml_path=None, otio_path=None, capcut_draft_path=None, render_path=None,
        encoder_used=None, resolve_status=None, error_message=error, scored_scenes=scenes,
        edit_timelines=timelines or {},
    )
