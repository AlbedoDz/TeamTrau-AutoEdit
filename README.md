# TeamTrau-AutoEdit: Production-Grade Automated Video Editing Orchestrator

<img width="1919" height="1033" alt="image" src="https://github.com/user-attachments/assets/dacd2867-770f-46b1-8b0e-72996cc7870b" />



Production-grade automated video editing engine on Windows 11 with NVIDIA GPU hardware acceleration (CUDA, NVENC).

The toolkit ingests raw 4K footage and soundtrack audio, isolates camera shot boundaries non-destructively (PySceneDetect), prunes static/dead air sections (Auto-Editor/OpenCV), tracks rhythmic downbeats and tempo (Librosa), scores visual composition and sharpness (CUDA/OpenCV), synthesizes multi-track timelines (OpenTimelineIO), and exports to:
1. **DaVinci Resolve Timeline (`.fcpxml`)** with official DaVinci Resolve Studio API automation.
2. **Adobe Premiere Pro Timeline (`.xml`)** via Apple FCP 7 XML interchange format.
3. **Standalone Headless Master Video (`.mp4`)** via FFmpeg Hardware NVENC (`hevc_nvenc`, preset p7, cq 18, 60M bitrate).

---

## 🚀 Quick Start

### 1. Requirements
- Windows 11 with NVIDIA GPU (CUDA & NVENC support)
- Python 3.11+
- FFmpeg with `--enable-nvenc`
- (Optional) DaVinci Resolve Studio 18.5+ / 21+ with External Scripting enabled

### 2. Verify Environment & Diagnostics
```powershell
.venv\Scripts\python.exe main.py check-env
```

### 3. List Available Presets
```powershell
.venv\Scripts\python.exe main.py list-presets
```
Default preset is **`1080p30`** (1920x1080 @ 30 fps). You can select 4K, high-framerate (60 fps), or vertical 9:16 reels presets:
- `1080p30`: Full HD 1080p @ 30 fps (Default)
- `1080p60`: Full HD 1080p @ 60 fps (Smooth motion)
- `1080p24`: Full HD 1080p @ 24 fps (Cinematic standard)
- `4k30`: Ultra HD 4K (3840x2160) @ 30 fps (Master deliverable)
- `4k60`: Ultra HD 4K (3840x2160) @ 60 fps (High motion action)
- `4k24`: Ultra HD 4K (3840x2160) @ 24 fps (Cinematic 4K master)
- `social_vertical`: Vertical 9:16 (1080x1920) @ 30 fps (TikTok, Reels, Shorts)
- `social_vertical_60`: Vertical 9:16 (1080x1920) @ 60 fps
- `cinema_4k`: DCI 4K (4096x2160) @ 24 fps

### 4. Run Automated Pipeline
```powershell
.venv\Scripts\python.exe main.py run `
  --input-dir "E:/Footage/Raw4K" `
  --soundtrack "E:/Audio/soundtrack.wav" `
  --output-dir "./output" `
  --preset 1080p30 `
  --resolve
```

Options:
- `--preset <name>`: Canvas geometry & fps preset (default: `1080p30`).
- `--fps <val>`: Custom framerate override.
- `--no-nvenc`: Disable GPU NVENC and fallback to CPU `libx265`.
- `--no-render`: Export FCPXML and Premiere XML only, skip standalone MP4 render.
- `--resolve`: Automatically create project, set canvas, import timeline, and queue render in DaVinci Resolve Studio.
- `--dry-run-resolve`: Simulate DaVinci Resolve dispatch without opening GUI.

---

## 📁 Output Artifacts
The pipeline outputs all non-destructive deliverables into `--output-dir`:
- `timeline.fcpxml`: DaVinci Resolve Studio sequence.
- `timeline_premiere.xml`: Adobe Premiere Pro sequence (FCP 7 XML standard).
- `timeline.otio`: Native OpenTimelineIO sequence file.
- `master_<preset>.mp4`: Headless 4K/1080p master video encoded with HEVC NVENC.
- `scenes_metadata.csv`: CSV export of all detected non-destructive scene timecodes.
- `scenes_metadata.edl`: Standard CMX 3600 Edit Decision List.
- `pipeline_summary.json`: Detailed execution report and metrics.

---

## 🧪 Testing & Verification
Run the 24 automated unit and integration tests:
```powershell
.venv\Scripts\python.exe -m pytest tests -v --tb=short
```

Run code formatting and lint checks:
```powershell
.venv\Scripts\python.exe -m ruff check src tests main.py
```
