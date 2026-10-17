"""Qt signal bridge decoupling backend worker callbacks from UI thread operations."""

from PySide6.QtCore import QObject, Signal

from src.core.service import JobCallbacks
from src.core.types import DeliverResult, PipelineExecutionResult, ScoredScene


class BackendSignalBridge(QObject):
    """Thread-safe bridge translating backend callbacks into Qt signals."""

    stage_changed = Signal(str, int, int)
    progress_updated = Signal(float, float, str)
    scene_discovered = Signal(object)
    scenes_tagged = Signal(list)
    log_received = Signal(str, str)
    job_finished = Signal(object)
    deliver_finished = Signal(object)

    def to_job_callbacks(self) -> JobCallbacks:
        """Create a JobCallbacks bundle bound to this bridge's Qt signals."""

        def _on_stage(name: str, idx: int, total: int) -> None:
            self.stage_changed.emit(name, idx, total)

        def _on_prog(stage_pct: float, total_pct: float, message: str) -> None:
            self.progress_updated.emit(stage_pct, total_pct, message)

        def _on_scene(scene: ScoredScene) -> None:
            self.scene_discovered.emit(scene)

        def _on_tagged(scenes: list[ScoredScene]) -> None:
            self.scenes_tagged.emit(scenes)

        def _on_log(level: str, message: str) -> None:
            self.log_received.emit(level, message)

        return JobCallbacks(
            on_stage_changed=_on_stage,
            on_progress=_on_prog,
            on_scene_discovered=_on_scene,
            on_scenes_tagged=_on_tagged,
            on_log=_on_log,
        )

    def notify_finished(self, result: PipelineExecutionResult) -> None:
        """Emit job completion signal with execution result."""
        self.job_finished.emit(result)

    def notify_deliver_finished(self, result: DeliverResult) -> None:
        """Emit Export/Preview completion signal with the DeliverResult (thread-safe)."""
        self.deliver_finished.emit(result)
