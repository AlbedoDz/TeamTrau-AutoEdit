# Architecture & Design Specification: AI-Assisted Auto-Curation & Selects Stringout Orchestrator

- **Status:** Approved (Ready for Implementation Planning)
- **Date:** 2026-10-23
- **Author:** Autonomous System Architect Agent (Antigravity Ecosystem)
- **Target Workflows:** Travel Vlogs, Talking Head / Interviews, Weddings, Event Highlights
- **Target Platform:** Windows 11 (NVIDIA RTX CUDA / NVENC), Multi-NLE Ecosystem (DaVinci Resolve Studio, Adobe Premiere Pro, CapCut Desktop)

---

## 1. Executive Summary & Intent

### 1.1 The Problem
Traditional AI video editing tools attempt to be an "all-in-one autonomous editor" that renders a finished master MP4 from scratch. In practice, professional video editors working on commercial events, weddings, travel vlogs, and client interviews **never** accept a final AI cut blindly. Furthermore:
1. **Monolithic Rendering Bottleneck:** Full-encode rendering of 4K 60fps/10-bit footage takes tens of minutes and produces rigid, non-editable pixels.
2. **NLE Handoff Breakdowns:** When exporting XML/EDL/FCPXML, editors face pervasive media relink errors, audio channel mismatches (mono vs stereo vs 5.1), shifted timecodes, and missing files due to pathing or codec inconsistencies.
3. **Agent Token Bloat:** Current LLM/agent tools dump raw frame-level arrays and float coordinate matrices into agent prompts, consuming 8k-15k tokens per prompt turn and forcing the LLM to do arithmetic on millisecond intervals.
4. **Sub-optimal Shot Selection:** Human editors spend 70% of their time on mundane "selects stringout": trimming camera start/stop shakes, discarding out-of-focus pans, and finding the single 4-second "golden take" hidden inside a 45-second clip.

### 1.2 The Solution
Refactor `TeamTrau-AutoEdit` into a specialized **AI-Assisted Auto-Curation & Selects Stringout Orchestrator**:
- **Structured Pre-Sorted Ingestion:** Rely on standard folder classification (`A-roll/`, `B-roll/`, `Music/`).
- **Two-Stage Curation Pipeline:** Stage 1 (Hard Technical Defect Excision) + Stage 2 (Golden Sub-Window Extraction via 4Hz Quality Curves).
- **Instant Stream Slicing:** Lossless Direct Stream Copy (`-c copy`) and surgical Smart-Cut for instant (<0.1s per cut) clip isolation without full re-encoding.
- **Fail-Safe NLE Interchange:** Pre-flight probed metadata enforcing strict RFC 3986 URI paths, standardized 48kHz audio track mappings, and fractional NTSC frame rate grids for DaVinci Resolve, Premiere Pro, and CapCut Desktop.
- **Token-Optimized Macro-Tools (<500 tokens):** High-level MCP abstraction where the agent directs creative intent (cadence, genre pacing, mood) while local engines calculate timecodes and frame math.

---

## 2. Goals, Non-Goals, and Anti-Goals

### 2.1 Goals
1. **Pre-Flight Invariant Guarantee:** 100% successful relink and timeline alignment when importing exported timelines into DaVinci Resolve Studio (FCPXML 1.8), Adobe Premiere Pro (FCP7 XML), and CapCut Desktop (`draft_content.json`).
2. **Sub-Second Rough Cut Processing:** Generate rough cut stringout timelines and lossless slices in seconds rather than minutes by eliminating mandatory full-sequence re-encoding.
3. **Autonomous Golden Window Extraction:** Automatically identify and extract 3s–8s optimal sub-windows from 15s–60s raw camera takes based on sharpness, camera stability, framing, and facial/emotional saliency.
4. **Token-Efficient Agent Orchestration:** Keep agent context footprint under 500 tokens per curation turn using semantic tag aggregations rather than raw coordinate dumps.
5. **Dual Review Delivery:** Always provide (a) native NLE interchange timeline for the editor and (b) an ultra-fast, lightweight preview MP4 (540p proxy) for instant playback verification.

### 2.2 Non-Goals
1. **Replacing the Professional NLE:** Not building a standalone timeline compositor, color grading suite, audio mixing board, or title animator inside Python. DaVinci Resolve and Premiere Pro remain the master finishing environments.
2. **Heavy Multi-Minute AI Models for Ingestion:** Not using multi-gigabyte slow VLM models just to determine if a clip has speech or b-roll. Classification must be ultra-fast (<0.3s/clip) using lightweight VAD and keyframe face sampling.

### 2.3 Anti-Goals (Things We Must NEVER Do)
1. **No Monolithic Full-Encode for Selects:** Never force a 4K NVENC re-encode just to review rough cut trims or selects stringouts.
2. **No Raw Frame Dumps to LLMs:** Never transmit frame-by-frame float arrays or raw optical flow vectors to MCP agents.
3. **No Non-Deterministic Timecodes:** Never approximate NTSC framerates (e.g. treating 29.97 fps as 30.0 fps). Frame counts must snap to exact rational integer fractions (`30000/1001` or `24000/1001`).

---

## 3. Workflow & Input Paradigm

The system supports two complementary ingestion modes:

### 3.1 Dual Ingestion Modes
1. **Mode A (Fully Automated - Single Folder):**
   - The user puts all raw footage into a single directory: `input/` (plus an optional `soundtrack.mp3`).
   - The **Automated Media Role Classifier** analyzes all files in parallel (<0.3s per clip) and assigns roles:
     - `A-roll` (Speech/Dialogue anchor)
     - `B-roll` (Visual cutaway/scenery)
     - `B-roll with Ambient Sound` (Mixed/Atmospheric)
   - Results are surfaced in the GUI/MCP metadata with one-click user override capability.
2. **Mode B (Manual Pre-Sorted Folders):**
   - Explicit folder hierarchy for users who prefer strict manual separation:
     ```
     [Project Input Directory]
     ├── A-roll/                  # Primary narrative: dialogue, interviews, speech
     │   ├── interview_01.mp4
     │   └── vows_bride.mov
     ├── B-roll/                  # Visual b-roll: details, scenery, action, reactions
     │   ├── detail_rings.mp4
     │   ├── walking_drone.mp4
     │   └── guest_cheering.mp4
     └── Music/                   # Structured soundtrack
         └── background_track.mp3
     ```

### 3.2 Automated Media Role Classifier Architecture (<0.3s/clip)

```
Raw Media File (Single Folder)
       │
       ├─► Probe 1: Audio Voice Activity & Clarity (VAD + SNR) [~50ms]
       │    ├── Extract 16kHz mono audio stream via ffmpeg pipe
       │    ├── Compute speech time ratio: voice_ratio = T_voice / T_duration
       │    └── Compute speech clarity / SNR: speech_snr_db
       │
       ├─► Probe 2: Visual Face & Subject Presence (Keyframe Sampling) [~30ms]
       │    ├── Sample 3 evenly spaced keyframes (at 20%, 50%, 80%)
       │    └── Detect human face presence & relative area (Medium/Close-up >= 8% area)
       │
       └─► Probe 3: Camera Motion Dynamics (Macro Optical Flow) [~20ms]
            └── Measure frame-difference / phase correlation:
                 * Low / panning motion -> Talking head / Anchor
                 * Fast / flycam / drone / walking motion -> B-roll
```

#### Classification Decision Matrix:
- **`A-roll` Rule:** `voice_ratio >= 0.30` AND `face_present == True` AND `speech_snr_db >= 10.0`.
- **`B-roll` Rule:** `voice_ratio < 0.15` OR `face_present == False` with high scene motion.
- **`Ambiguous / Hybrid` Rule:** `0.15 <= voice_ratio < 0.30`: Tagged as `B-roll (ambient speech)`, available for visual cutaway or hook soundbites.

### 3.3 Processing Treatment by Role:
- **`A-roll` Treatment:** Primary speech/dialogue anchor. Sliced based on speech activity (faster-whisper VAD/word timestamps), removing dead air, pauses, and technical jerks while maintaining audio-video sync.
- **`B-roll` Treatment:** Sliced based on visual quality curves (sharpness, camera motion smoothness, facial expression, framing stability) to find 3s–8s "golden takes". Native camera audio is ducked (-14dB) or muted under soundtrack music.
- **`Music` Treatment:** Analyzed for BPM, beat grid, 4/4 downbeats, and macro musical sections (`INTRO`, `BUILDUP`, `DROP`, `OUTRO`).

---

## 4. Two-Stage Curation & Slicing Engine

```
Raw Clip (15s - 60s)
  │
  ▼
[Stage 1: Defect Excision]
  ├── Head/Tail Jerk Pruning (camera start/stop button press)
  ├── Wild Shake / Whip-Pan Bisection (DIS Optical Flow > 80 px/frame)
  ├── Out-of-Focus Hunting Excision (Tenengrad Sharpness drop > 65%)
  ├── Ground / Carrying Excision (Downward optical flow + low texture)
  └── Extreme Exposure Clipping & Lens Cap Occlusion
  │
  ▼
Cleaned Valid Intervals [t_start, t_end]
  │
  ▼
[Stage 2: Golden Sub-Window Extraction]
  ├── Continuous 4Hz Quality Curve Generation:
  │     Q(t) = w_sharp * S(t) + w_motion * M(t) + w_face * F(t) + w_aesthetic * A(t)
  ├── Sliding Window Integrator (Window size W ∈ [3.0s, 8.0s]):
  │     Score(t_w) = ∫_{t_w}^{t_w + W} Q(t) dt - Penalty(Jitter)
  ├── Local Maxima Peak Extraction (Golden Takes)
  └── Perceptual 64-bit dHash Deduplication (Hamming distance ≤ 8 drops redundant angles)
  │
  ▼
Curated Golden Takes (3s - 8s each)
```

### 4.1 Stage 1: Defect Excision Specifications
- **Head & Tail Pruning:** Drops start ($0.5\text{s} - 1.2\text{s}$) and end ($0.5\text{s} - 1.5\text{s}$) where camera operator button press introduces angular jerk.
- **Motion Stability Filter:** DIS Optical Flow monitors translational and rotational variance. If average vector $>80\text{ px/f}$ across $>0.3\text{s}$, clip is split and the shaky span is discarded.
- **Focus Guard:** Normalized Tenengrad gradient variance. Drops intervals where sharpness drops below 35% of the clip's 75th percentile baseline.
- **Ground Carrying Detector:** Downward optical flow ($v_y > +15\text{ px/f}$) combined with low high-frequency spatial texture identifies moments when the camera is carried pointing at the ground.

### 4.2 Stage 2: Golden Sub-Window Extraction Specifications
- **Continuous Sampling:** Evaluated at 4 samples per second ($250\text{ms}$ step).
- **Composite Quality Function:**
  $$Q(t) = 0.35 \cdot S_{\text{norm}}(t) + 0.25 \cdot M_{\text{smooth}}(t) + 0.25 \cdot F_{\text{face}}(t) + 0.15 \cdot A_{\text{aesthetic}}(t)$$
- **Sliding Sub-Window:** Window duration $W$ adapts to genre:
  - Wedding / Cinematic: $W \in [4.0\text{s}, 8.0\text{s}]$
  - Travel Vlog / Highlight: $W \in [2.5\text{s}, 4.5\text{s}]$
  - Talking Head: Window bounded by spoken sentences ($1.5\text{s} - 12.0\text{s}$).
- **Non-Destructive Snapping:** Window boundaries snap to nearest music beats (or sentence pauses for A-roll).

---

## 5. Lossless-Cut & Smart-Cut Slicing Engine

To avoid the CPU/GPU rendering bottleneck during the curation phase, the engine implements a hybrid cutting pipeline inspired by `mifi/lossless-cut`:

```
                       Input File (H.264 / HEVC / ProRes)
                                       │
                      Can in/out snap to Keyframes (I-frames)?
                                    /       \
                             YES   /         \   NO
                                  /           \
                                 ▼             ▼
                [Direct Stream Copy]     [Surgical Smart Cut]
                 ffmpeg -ss in -to out    Head GOP: Re-encode from in-point to 1st Keyframe
                 -c copy                  Interior: Direct Stream Copy (-c copy)
                 Execution: < 50ms        Tail GOP: Re-encode from last Keyframe to out-point
                                          Concat Pass: Seamless stitch without re-encoding body
```

### 5.1 Direct Stream Copy Mode (`-c copy`)
- Used when cutting selects for NLE rough cuts where exact frame-level boundaries will be refined by the editor, or when in/out points fall on GOP keyframes.
- Zero generational quality loss, zero color shift, execution time bounded only by disk I/O ($>500\text{MB/s}$).

### 5.2 Surgical Smart Cut Mode
- Used when exact frame accuracy is required without re-encoding the entire 30-minute master file:
  1. Identifies keyframe timestamps $K_{\text{prev}} \le t_{\text{in}} < K_{\text{next}}$ and $K'_{\text{prev}} \le t_{\text{out}} < K'_{\text{next}}$ via `ffprobe`.
  2. Transcodes only the head slice $[t_{\text{in}}, K_{\text{next}}]$ using identical codec parameters (Profile, Level, Color Space, Bit Depth, Color Primaries, Transfer Characteristics).
  3. Stream copies $[K_{\text{next}}, K'_{\text{prev}}]$ directly via `-c copy`.
  4. Transcodes only the tail slice $[K'_{\text{prev}}, t_{\text{out}}]$.
  5. Concatenates the three segments losslessly.

---

## 6. Strict NLE Interchange Contracts (DaVinci, Premiere, CapCut)

Import failures in Premiere Pro and DaVinci Resolve are eliminated through an immutable pre-flight inspection and normalization contract:

```
Raw Media Assets
       │
       ▼
[Pre-Flight Inspector & Audio Normalizer]
  ├── Path URI Formatter (RFC 3986 file:/// URIs, POSIX slashes, Unicode intact)
  ├── Audio Channel Map Normalizer (Mono -> Dual Mono, 5.1 -> Stereo L/R, 48kHz sync)
  ├── Framerate Rationalizer (23.976 -> 24000/1001, 29.97 -> 30000/1001, 59.94 -> 60000/1001)
  └── Source Timecode Extractor (Preserves native BWF/MOV Start TC)
       │
       ├─────────────────────────┼─────────────────────────┐
       ▼                         ▼                         ▼
[DaVinci Resolve FCPXML 1.8]   [Premiere Pro FCP7 XML]   [CapCut Desktop JSON]
- Asset-clip spine model       - Explicit <audio> tracks  - Microsecond units
- Rational time units (e.g.    - <channelcount>2</...>    - `draft_content.json`
  `1001/30000s`)               - <samplerate>48000</...>  - `draft_meta_info.json`
- Linear timeMap for ramps     - Absolute project file    - Transform & speed maps
- Audio lane separation (-1)     references               - Matched canvas aspect
```

### 6.1 DaVinci Resolve Studio (FCPXML 1.8)
- **Schema:** Standalone FCPXML 1.8 writer (`src/modules/fcpxml_exporter.py`).
- **Timing:** Expressed as rational numbers (`start="1001/30000s"`, `duration="240240/30000s"`).
- **Retime Integrity:** Retimed clips use explicit linear `timeMap` relative to media origin:
  $$\text{start} = \frac{\text{in\_point}}{\text{speed}}$$
- **Media Linking:** Uses RFC 3986 `file:///` URLs with normalized forward slashes.

### 6.2 Adobe Premiere Pro (FCP7 XML / xmeml)
- **Schema:** Apple FCP 7 XML specification (`src/modules/timeline_builder.py`).
- **Channel Mismatch Prevention:**
  - Inspects each asset's audio channel layout using `ffprobe`.
  - Maps dual-channel stereo into paired `<audio><channelcount>2</channelcount>` clipitems with matching audio tracks.
  - Generates zero `<audio>` clipitems for silent clips, preventing "Media Offline / Missing Audio" warnings.
  - Forces sequence canvas geometry and sample rate (`48000 Hz`) to match target delivery rather than inheriting from the first clip.

### 6.3 CapCut Desktop (`draft_content.json`)
- **Schema:** Native desktop draft format (`src/modules/capcut_exporter.py`).
- **Microsecond Timing:** Converts all durations and offsets to integer microseconds ($1\text{s} = 1,000,000\,\mu\text{s}$).
- **Track Separation:** Dedicated video track (`type: "video"`), primary speech audio track, and BGM music track (`type: "audio"`) with ducking gain parameters.

---

## 7. Agent Token Optimization & MCP Macro-Tool Interface

### 7.1 The Token Problem
Dumping raw scene lists with bounding boxes, optical flow scores, and frame boundaries for 50 clips consumes **12,000+ tokens**, leads to context rot, and forces the LLM into arithmetic errors when computing cut intervals.

### 7.2 The Solution: 3 High-Level Macro-Tools (<500 Tokens Total)

```
┌────────────────────────────────────────────────────────────────────────┐
│                          LLM / AGENT LAYER                             │
│       Makes creative decisions: Genre, Pacing, Shot Tags, Mood        │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ Calls Macro-Tool (<300 tokens payload)
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                        FAST-MCP TOOL INTERFACE                         │
│  1. `curate_footage(input_dir, genre_preset, target_duration)`         │
│  2. `assemble_cadence_timeline(selected_shot_ids, soundtrack, pacing)` │
│  3. `export_nle_project(target_nle, export_proxies, render_preview)`   │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ Delegates execution to local engines
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                        LOCAL DETERMINISTIC ENGINES                     │
│  - DIS Optical Flow, Tenengrad Focus Guard, Librosa Beat Grid,         │
│  - Lossless Smart-Cut, FCPXML / Premiere / CapCut Serializers          │
└────────────────────────────────────────────────────────────────────────┘
```

### 7.3 Detailed Macro-Tool Definitions

#### Tool 1: `curate_footage`
- **Agent Input:**
  ```json
  {
    "input_dir": "E:/Projects/Wedding2026",
    "genre": "wedding_cinematic",
    "target_duration_sec": 120.0
  }
  ```
- **Local Engine Work:**
  - Scans `A-roll/` and `B-roll/`.
  - Runs Stage 1 Defect Excision and Stage 2 Golden Sub-Window Extraction.
  - Groups candidates into semantic clusters.
- **Agent Output (<350 tokens):**
  ```json
  {
    "total_raw_duration": "14m 20s",
    "curated_candidates_count": 18,
    "scenes": [
      {"id": "A01", "type": "speech", "speaker": "Groom", "summary": "Vows exchange emotional", "duration": 8.4, "quality": 0.94},
      {"id": "B04", "type": "broll", "tag": "Rings macro close-up, sharp", "duration": 4.0, "quality": 0.96},
      {"id": "B12", "type": "broll", "tag": "Bride walking down aisle, sunny", "duration": 5.2, "quality": 0.91},
      {"id": "B19", "type": "broll", "tag": "Guest tears reaction shot", "duration": 3.8, "quality": 0.89}
    ]
  }
  ```

#### Tool 2: `assemble_cadence_timeline`
- **Agent Input:**
  ```json
  {
    "selected_shot_ids": ["A01", "B04", "B12", "B19"],
    "soundtrack_path": "E:/Projects/Wedding2026/Music/vows_theme.mp3",
    "cadence_style": "emotional_buildup",
    "teaser_hook": true
  }
  ```
- **Local Engine Work:**
  - Snaps selected shots to Librosa musical bar/beat grid.
  - Assembles A-roll on primary dialogue track with automatic BGM ducking (-14dB).
  - Intersperses B-roll golden windows on downbeats during buildup/drop.
  - Generates 3-clip teaser hook at timeline head.
- **Agent Output (<150 tokens):**
  ```json
  {
    "timeline_id": "TL_WED_001",
    "total_cuts": 16,
    "timeline_duration": "1m 58s",
    "status": "TIMELINE_ASSEMBLED_SUCCESS"
  }
  ```

#### Tool 3: `export_nle_project`
- **Agent Input:**
  ```json
  {
    "timeline_id": "TL_WED_001",
    "targets": ["davinci_fcpxml", "premiere_xml", "capcut_draft"],
    "generate_preview_mp4": true
  }
  ```
- **Local Engine Work:**
  - Runs pre-flight media & audio URI validator.
  - Serializes FCPXML 1.8, Premiere FCP7 XML, and CapCut `draft_content.json`.
  - Runs headless 540p fast proxy render (15s via NVENC or CPU fallback).
- **Agent Output (<100 tokens):**
  ```json
  {
    "fcpxml_path": "E:/Projects/Wedding2026/output/timeline.fcpxml",
    "premiere_xml": "E:/Projects/Wedding2026/output/timeline.xml",
    "capcut_draft_path": "C:/Users/.../CapCut Drafts/Wedding2026",
    "preview_mp4": "E:/Projects/Wedding2026/output/preview_540p.mp4",
    "status": "EXPORT_COMPLETE"
  }
  ```

---

## 8. FreeCut Native In-App Fusion & Upstream Architecture

Rather than creating a disjointed floating modal or replacing FreeCut with a custom canvas, the system implements **Native In-App Fusion** via an **Isolated Extension Architecture** tracking `upstream: walterlow/freecut`.

### 8.1 Zero-Conflict Directory Structure
```
TeamTrau-AutoEdit/
├── backend/                     # Python 3.11+ AI Engine (CUDA, NVENC, FastMCP, Librosa)
│   ├── src/modules/             # MediaClassifier, GoldenCurator, SmartCutter, ProxyGenerator
│   └── pyproject.toml
│
└── frontend/                    # Cloned from `walterlow/freecut` (Tracks upstream git remote)
    ├── src/                     # 100% Unmodified Upstream FreeCut Code
    │   ├── features/            # Timeline, Preview, Media Library, Inspector, Export
    │   └── shared/              # Schemas, Stores, Utilities
    │
    └── src/extensions/teamtrau/ # ISOLATED TEAMTRAU EXTENSION (Zero upstream merge conflicts)
        ├── components/          # AI Toolbar Buttons, AI Inspector, Beat Ruler Overlay
        ├── hooks/               # useTeamTrauBridge (WebSocket/REST client to localhost:8000)
        └── index.ts             # Single-touch registration entrypoint
```

### 8.2 Seamless Upstream Maintenance
When the author of FreeCut pushes new features or bug fixes:
```bash
cd frontend
git fetch upstream
git merge upstream/main
```
Because custom AI features are fully isolated within `src/extensions/teamtrau/`, merges complete automatically with **zero merge conflicts**.

### 8.3 Native In-App Touchpoints
1. **Media Library Panel:**
   - Smart Role Badges on each card: `[A-roll (Vocal)]` (purple), `[B-roll (Visual)]` (green), `[Music]` (yellow with detected BPM).
   - Right-click context menu: 1-click manual role re-assignment (`A-roll` <-> `B-roll`).
   - Aesthetic score badges (Tenengrad sharpness + MobileNet/CUDA aesthetic score).
2. **Timeline Toolbar & Ruler:**
   - Added native buttons: `[Auto Rough Cut]`, `[Snap to Beat]`, `[Lossless Cut]`.
   - Timecode Ruler: Directly visualizes Librosa beat ticks, 4/4 downbeats, and musical section blocks (`INTRO`, `BUILDUP`, `DROP`, `OUTRO`).
   - Track Layout: V2 (B-roll cutaways), V1 (A-roll dialogue spine), A1 (speech audio), A2 (soundtrack).
3. **Inspector Sidebar:**
   - Dedicated "AI Curation" collapsible section displaying the **Golden Sub-Window (3s-8s)** with interactive In/Out drag handles.
4. **Export Modal:**
   - Direct export targets: DaVinci Resolve Studio (FCPXML 1.8), Adobe Premiere Pro (XML), CapCut Desktop (JSON), Lossless MP4 Slices.
5. **State Synchronization & Undo/Redo:**
   - AI operations mutate FreeCut's Zustand store directly (`useTimelineStore`), enabling full native `Ctrl+Z` (Undo) and `Ctrl+Y` (Redo) support.

---

## 9. 4K 60fps/120fps Hardware Optimization Engine (DJI & Sony Alpha)

To match and exceed the playback smoothness of DaVinci Resolve Studio and CapCut Desktop, the system implements 4 core GPU optimizations:

### 9.1 Background NVENC Ultra-Fast Editing Proxy
- **The Bottleneck:** 4K 60fps Long-GOP footage from DJI Pocket 4 / Mavic and 10-bit 4:2:2 from Sony Alpha causes severe decode lag during timeline scrubbing on RTX 30/40 series GPUs.
- **The Solution:** On import, the background engine spawns FFmpeg with `h264_nvenc` generating **720p/1080p All-Intra proxies (`-g 1` or `-g 12`)** at >300 fps (1-minute 4K clip converts in ~2 seconds).
- FreeCut scrubs the proxy with zero latency. When exporting to DaVinci or Premiere, the exporter automatically relinks to the **original master 4K media**.

### 9.2 Real-time WebGPU 3D LUT Viewport Preview
- Supports DJI D-Log M and Sony S-Log3 flat color profiles.
- Applies a 3D LUT via WebGPU fragment shader on the viewport canvas in real time (0ms CPU cost), providing vibrant, accurate colors without modifying source pixels.

### 9.3 High Frame Rate (120fps) Voice Safety Guard
- High frame rate clips (100fps - 240fps) undergo automatic VAD inspection:
  - If speech detected (`voice_ratio >= 0.15`): Playback speed is strictly locked to **1.0x** to prevent vocal pitch distortion.
  - If scenic B-roll without voice: Automatically tagged as `Slow-Mo Candidate` and conformed to **4x/5x cinematic slow-motion** (24fps/30fps).

### 9.4 Multi-Channel Camera Audio Preservation
- Scans all 2 to 4 discrete camera audio tracks (lavalier mic, shotgun ambient, -12dB safety track).
- Selects the cleanest voice SNR track for A-roll speech analysis, but preserves all native audio tracks when generating NLE XMLs.

---

## 10. Comprehensive 16-Point Safeguard Matrix

| # | Trap / Edge Case | Failure Mode Without Safeguard | Architectural Safeguard |
| :- | :--- | :--- | :--- |
| **1** | **Browser Sandbox Barrier** | FreeCut cannot read `E:\Footage\clip1.mp4` via `file://` | Local Media Streaming Server with HTTP Range Requests (`http://localhost:8000/stream/...`). |
| **2** | **Port Collision (8000 in use)** | Backend crashes with `Address already in use` | Dynamic port binding (8000-8010) + `.teamtrau_port` discovery. |
| **3** | **Duplicate File Names** | Multiple cards having `DJI_0001.MP4` overwrite proxies | Unique Asset ID: `SHA256(path + size + mtime)[:8]`. |
| **4** | **Audio DTS/PTS Delay** | Audio/video out of sync by 1-2 frames after `-c copy` | Flags: `-avoid_negative_ts make_zero -fflags +genpts`. |
| **5** | **120fps Speech Slowdown** | Dialogue sounds like a slowed-down monster voice | Voice Safety Guard: Lock 1.0x if `voice_ratio >= 0.15`. |
| **6** | **Micro-takes (< 1.5s)** | Defect pruning leaves tiny, uneditable 0.4s fragments | Dynamic window clamping: Discard fragments below 1.5s. |
| **7** | **Variable Frame Rate (VFR)** | Audio drifts out of sync over time in Premiere/Resolve | Automated VFR detection + fast CFR normalization cache. |
| **8** | **Long-GOP Scrubbing Lag** | 0.5s freeze per scrub click on 4K 60fps DJI files | NVENC All-Intra 720p/1080p proxy generated in background. |
| **9** | **Log Flat Color (D-Log/S-Log)** | Washed-out preview, AI aesthetic scoring penalized | Real-time WebGPU 3D LUT shader preview + Rec.709 norm. |
| **10** | **9:16 Vertical Video Rotation** | DJI Pocket vertical video displays sideways (rotated 90°) | DisplayMatrix metadata extraction + automatic canvas match. |
| **11** | **Corrupted Video Files** | Incomplete MP4 (missing `moov atom`) hangs pipeline | Hard 10s subprocess timeout + quarantine file isolation. |
| **12** | **Metronome Pacing** | Timeline cuts uniformly every 2.0s, feeling robotic | Cadence hierarchy (varied shot lengths by section). |
| **13** | **Multi-Track Audio Chaos** | Shotgun noise picked instead of lav mic; safety track lost | Multi-channel SNR probe + 100% channel preservation. |
| **14** | **Disk Space Explosion** | Intermediate caches fill drive C: causing OS crash | Cache strictly in `.autoedit_cache/` on project drive with LRU cap. |
| **15** | **Agent Race Conditions** | Agent MCP edits overwrite user's manual timeline trims | Action-based mutations with full Undo/Redo (`Ctrl+Z`). |
| **16** | **Agent Timecode Hallucinations** | Agent calculates floating-point seconds wrong -> gaps | Agent sends only IDs; Python backend computes all frames. |

---

## 11. Summary & Sign-off

This architecture establishes a world-class synergy:
- **FreeCut** provides the sleek, GPU-accelerated NLE frontend.
- **TeamTrau-AutoEdit** provides the heavy AI, CUDA/NVENC, Librosa, and NLE export engine.
- **FastMCP** provides autonomous agent control for Antigravity, Claude Code, and ChatGPT with <500 tokens.
- **The Upstream Extension Pattern** guarantees zero merge conflicts when FreeCut is updated.
