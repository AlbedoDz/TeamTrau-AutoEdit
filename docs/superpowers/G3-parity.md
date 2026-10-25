# Gate G3 — Feature parity: PySide6 GUI vs TeamTrau Studio (2026-10-25)

**Sources**
- PySide6 inventory: `src/ui/**` (main_window, media_browser, scene_grid, inspector_panel, timeline_view, export_panel, settings, agent console).
- Studio: `teamtrau/main` at `6b036685`, with Phase 3 panels Ingest, Curation, Sequence, Deliver, Agent and Engine & Jobs.
- Acceptance screenshots are in `docs/superpowers/handoffs/p3*.png`.

**Status key:** Done = available in the Studio and verified live on the real workspace. Partial = available but reduced. Missing = not in the Studio yet.

## Workflow and stages

| PySide6 feature | Studio | Status |
|---|---|---|
| Rough Cut stage (ingest + defect pruning) | Ingest → Curate (job `curate`) | Done |
| Sequence stage (cadence assemble) | Sequence → Assemble (job `assemble`) | Done |
| "Run All" one-click | — | Missing (two clicks today) |
| Stop / cancel a running job | Engine & Jobs → Cancel (every job kind) | Done |
| Stage availability gating | Buttons are disabled until their inputs exist (folder, session) | Done |
| Progress per stage | Jobs list with progress and message | Done |

## Media browser

| PySide6 feature | Studio | Status |
|---|---|---|
| Role folders A-roll / B-roll / Music | Ingest folder picker plus the classify table with folder/role mismatch | Done |
| Content-role classification | Ingest table (role, confidence, speech, reasons) | Done |
| Probe info per file (resolution, fps, codec) | FreeCut media library shows it; the Ingest table does not | Partial |
| Soundtrack BPM / sections | Sequence → Analyze soundtrack | Done |
| Genre recommender banner | — | Missing |
| CameraRole combo (cam_a / drone / …), saved only in the project | — | Missing (the engine never used it) |

## Scene grid → Curation panel

| PySide6 feature | Studio | Status |
|---|---|---|
| Golden-take cards with thumbnail and score | Curation grid (virtualised) | Done |
| Include / exclude a take | Toggle plus P / X hotkeys, PATCH to the engine session | Done |
| Role override | Role select, PATCH `content_role` | Done (new: PySide6 had no ContentRole override) |
| Min-score filter, role filter, sort | Yes | Done |
| Include all / exclude all / exclude dead air | — | Missing |
| Badges: trimmed, long shot, dead air, deflicker | Only golden window and defect chips | Partial |
| Inspector: sharpness, aesthetic, motion, crop window, VLM fields | Composite score only | Partial |
| VLM tagging | — | Missing |
| Send takes to timeline | One undo step (new; PySide6 re-ran the sequence) | Done |

## Inspector settings

| PySide6 feature | Studio | Status |
|---|---|---|
| Genre preset | Ingest genre select (`/v1/presets`) | Done |
| Cadence profile | Sequence cadence select | Done |
| Long-shot and max-scene limits, defect-pruning toggles, minimum take length | — | Missing |
| Duration mode, AI toggles, reframing, subtitles, Ken Burns, speed ramping, watermark, teaser | — (subtitles exist in Deliver only) | Missing |

## Timeline

| PySide6 feature | Studio | Status |
|---|---|---|
| Bar grid / beats / sections on the ruler | Beat-grid overlay plus "Add bars as markers" (one undo step) | Done |
| Manual editing (cut, trim, move, undo) | The full FreeCut editor | Done (far beyond PySide6) |
| Real-time playback with audio | FreeCut player, GPU decode, render cache, P4.5 pooled-element fix | Done (PySide6 had no audio) |
| Multi-variant timelines (variant pills) | — | Missing |
| Snap-error lock badge, clip sub-window text | — | Missing |

## Export / Deliver

| PySide6 feature | Studio | Status |
|---|---|---|
| Deliver settings (canvas, fps, fit, color, master, subtitles, audio) | Deliver form | Done |
| NLE export (FCPXML / Premiere / OTIO / CapCut) | Deliver formats; FCPXML and CapCut verified live | Done |
| NVENC master render | `h264_nvenc` verified with ffprobe (P3.5) | Done |
| Pre-flight / source-mismatch warning | Pre-flight grouped report with a blocking override | Done |
| Variant list (deliver several timelines) | — | Missing |
| Deliverables list and open folder | Result card plus guarded Open Folder | Done |

## Settings and agent

| PySide6 feature | Studio | Status |
|---|---|---|
| Agent console | Agent tab with streamed thoughts and tool cards, cancel, history | Done |
| AI endpoint settings (API key, ping) | — (the agent runs in heuristic mode without a key) | Missing |
| Vietnamese UI | Full `vi` locale | Done (new) |
| Offline use | Electron offline, local fonts | Done (new) |
| Cross-platform | Electron + Python engine (Windows verified) | Done on Windows; macOS/Linux in Phase 5 |

## Summary
- **Done:** 27.
- **Partial:** 3 — probe info in Ingest, scene badges, the inspector detail fields.
- **Missing:** 12. Grouped:
  - one-click Run All;
  - genre recommender;
  - camera role;
  - bulk include/exclude;
  - VLM tagging;
  - pipeline advanced options (long-shot limits, defect toggles, minimum take, duration mode, AI toggles, reframing, Ken Burns, speed ramping, watermark, teaser);
  - multi-variant timelines and their deliver list;
  - snap-error badge;
  - AI endpoint settings.

**Architect recommendation:** close the gaps that matter to daily editing in one Studio task, P3.7, before freezing PySide6:
1. Bulk include/exclude plus the scene badges and inspector details.
2. An "Advanced" section in Sequence exposing the pipeline options the engine already accepts.
3. AI endpoint settings, so the agent can use a real LLM.
4. Run All.

Leave these for later: multi-variant timelines, VLM tagging, the genre recommender and camera role.
