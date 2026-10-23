# Kế Hoạch Triển Khai Hoàn Chỉnh: Studio 3-Stage Pipeline & Rhythmic Cadence Engine

## 1. Mục Tiêu & Quyết Định Thiết Kế (Đã Đồng Thuận Qua /grill-me)

Quy trình biên tập video trong **TeamTrau AutoEdit Desktop Studio** được thiết kế lại toàn diện từ lúc Import đến khi xuất file Deliverable theo **3 Giai đoạn Tuần Tự (3-Stage Studio NLE Pipeline)**:

```
[1. Ingest & Organize] ➔ [2. Rough Cut / A-Roll (BẮT BUỘC)] ➔ [3. Sequence Edit (Genre & Beat)] ➔ [4. Deliver / Export]
```

### Các Quyết Định Thiết Kế Đã Thống Nhất:
1. **Mô hình tương tác trên GUI (Flexible Dual-Mode)**:
   - **Chế độ Phân Chặng (Staged Step-by-Step)**:
     - Nút `🔨 1. Run Rough Cut (Clean Takes)`: Nhập liệu, lọc bỏ khuyết tật, bóc tách Golden Takes, nạp timeline Rough Cut Stringout.
     - Nút `✨ 2. Auto-Edit Sequence (Genre & Beat)`: Dựng sequence thành phẩm khớp nhịp theo thể loại yêu cầu.
   - **Chế độ 1-Click (⚡ Auto Run All)**: Chạy tự động trọn gói từ đầu đến cuối một lần duy nhất cho người dùng thích tự động hóa.
2. **Xử lý Cảnh Quay Dài (>15s và >30s)**:
   - Cảnh đẹp dài >15s (drone, gimbal tracking, hành động liên tục) được cấp phát **slot câu nhạc lớn (4 bars $\approx$ 8s hoặc 8 bars $\approx$ 16s)**, không bao giờ bị ép băm nát ở 2s.
   - Quét cửa sổ vàng (Golden Sub-Window) dựa trên hàm mục tiêu tối ưu độ mượt chuyển động và thẩm mỹ.
   - Nếu source shot cực dài (>30s) và có nhiều đoạn đẹp, thuật toán cho phép trích xuất **tối đa 2 phân đoạn** xuất hiện ở các vị trí phù hợp trong bài nhạc.
3. **Trải nghiệm kiểm tra Rough Cut (Review UX)**:
   - Ngay khi Giai đoạn 2 hoàn thành, GUI **tự động chuyển tab sang `Scene Quality & Review`** hiển thị danh sách Golden Takes (với toggle Bật/Tắt).
   - Đồng thời nạp biến thể timeline **`rough_cut` (Chronological Stringout)** lên Multi-Track Timeline và Program Monitor để người dùng bấm Play kiểm tra trực quan ngay lập tức.
4. **Động cơ nhịp điệu biến thiên (Hierarchical Rhythmic Cadence Engine - HRCE)**:
   - Phân cấp nhịp lý: Phách (Beat) $\rightarrow$ Ô nhịp (Bar = 4 beats) $\rightarrow$ Câu nhạc (Phrase: 2-bar, 4-bar, 8-bar).
   - Biến thiên tiết tấu theo cung bậc cảm xúc (Tension & Release): Intro (4 bars) $\rightarrow$ Buildup (2 bars $\rightarrow$ 1 bar) $\rightarrow$ Drop (1/2 bar $\rightarrow$ 1 bar) $\rightarrow$ Outro (4-8 bars).
   - Khóa chặt 100% điểm cắt vào Downbeat Librosa, sai số thời gian $< 0.03s$ (< 1 frame).

---

## 2. Luồng Dữ Liệu Chi Tiết (Full Execution Flow)

```mermaid
flowchart TD
    subgraph S1 ["GIAI ĐOẠN 1: INGEST & ORGANIZATION"]
        A[Footage Folders] --> B[ffprobe Metadata: FPS, Duration, Rotation]
        B --> C[VFR -> CFR Normalization Pass]
        C --> D[Camera Role Tagging: cam_a, cam_b, drone, action_cam, b_roll]
        D --> E[PySceneDetect Adaptive Cut Discovery]
        E --> F[Dual Dead-Air / Freeze Filter & 64-bit dHash Dedup]
    end

    subgraph S2 ["GIAI ĐOẠN 2: ROUGH CUT & A-ROLL CURATION (BẮT BUỘC)"]
        F --> G[RoughCutCleaner: Quét DIS Optical Flow & Tenengrad Sharpness]
        G --> H[Cắt Bỏ Triệt Để Khuyết Tật:<br/>- Rung máy bấm nút Start/Stop (Head/Tail Wobble)<br/>- Rung giật mạnh (>80px/frame) & Whip Pan bừa bãi<br/>- Mất nét (Focus Hunting) & Tối mắt đọc]
        H --> I[Trích Xuất Phân Đoạn Vàng (CleanedSegment / Golden Takes)]
        I --> J[Phân Hạng A-Roll (Cốt lõi) & B-Roll (Phụ trợ)]
        J --> K[Chấm Điểm Đa Tầng: Sharpness, Aesthetic MobileNetV3, Motion Energy]
        K --> L[Lưu Trữ Curated A-Roll Pool]
        L --> M[GUI: Tự Động Chuyển Tab Scene Review & Nạp Timeline Rough Cut Stringout]
    end

    subgraph S3 ["GIAI ĐOẠN 3: AUTO-EDIT SEQUENCE BY GENRE & CADENCE"]
        L --> N[Nhận Curated A-Roll Pool Đã Sạch Lỗi]
        Soundtrack[Nhạc Nền Soundtrack] --> O[Librosa Phân Cấp Nhịp: Beat -> Bar -> Phrase]
        O --> P[Song Sections: INTRO, BUILDUP, DROP, OUTRO]
        
        GenreMatrix[Ma Trận 23 Thể Loại & Cadence Profile] --> Q[Dynamic Cadence Engine]
        O & P & GenreMatrix --> Q
        
        N & Q --> R[Content-Aware Timeline Synthesizer]
        R --> S{Độ Dài & Tiềm Năng Cảnh}
        S -->|Shot Dài >15s| T[Cấp phát 4 bars (~8s) hoặc 8 bars (~16s)<br/>Trích xuất Golden Sub-Window chuyển động mượt]
        S -->|Shot Cực Dài >30s| U[Cấp phát tối đa 2 phân đoạn ở 2 đoạn nhạc khác nhau]
        S -->|Shot Vừa 4-14s| V[Cấp phát 2 bars (~4s) hoặc 1 bar (~2s)]
        S -->|Shot Ngắn <4s / B-Roll| W[Cấp phát 1 bar (~2s) hoặc 1/2 bar (~1s)]
        
        T & U & V & W --> X[Khóa Chặt 100% Ranh Giới Cắt Vào Downbeat Librosa]
        X --> Y[Tích Hợp: Speed Ramping, Smart 9:16 Reframe, Exposure Guard, Subtitles]
        Y --> Z[Sinh 3 Biến Thể: Beat-Driven, Cinematic, Social Teaser]
    end

    subgraph S4 ["GIAI ĐOẠN 4: DELIVER & EXPORT"]
        Z --> AA[Timeline Ready & Preview Hoạt Động Trên Program Monitor]
        AA --> AB[🚀 Export: DaVinci FCPXML, Premiere XML, CapCut Draft, Master MP4 NVENC]
    end

    S1 --> S2
    S2 --> S3
    S3 --> S4
```

---

## 3. Thiết Kế Giao Diện GUI & Điều Khiển Người Dùng

### 3.1. TopBar Stepper 4 Bước
Thanh chỉ báo quy trình ở góc trên cửa sổ chính:
```
[1. Ingest & Organize] ➔ [2. Rough Cut (Cleaned)] ➔ [3. Sequence Edit] ➔ [4. Deliver]
```
- Khi mở app hoặc import folder: Bước 1 sáng (`● 1. Ingest & Organize`).
- Khi chạy xong Rough Cut: Bước 2 sáng (`✔ 1. Ingest  ➔  ● 2. Rough Cut`).
- Khi hoàn tất Sequence Edit: Bước 3 sáng (`✔ 1. Ingest  ➔  ✔ 2. Rough Cut  ➔  ● 3. Sequence Edit`).
- Khi render hoặc export deliverables: Bước 4 sáng (`✔ 4. Deliver`).

### 3.2. Cụm Nút Điều Khiển Trong `InspectorPanelWidget`
Dưới chân Smart Inspector có cụm 3 nút hành động phân cấp:
1. `🔨 1. Run Rough Cut (Clean Takes)`:
   - Chạy Ingest $\rightarrow$ RoughCutCleaner.
   - Tạo Golden Takes, tự động chuyển tab sang `Scene Quality & Review`, nạp timeline `rough_cut` lên canvas.
2. `✨ 2. Auto-Edit Sequence (Genre & Beat)`:
   - Lấy Golden Takes từ Bước 1, kết hợp thể loại đã chọn (Preset 23 subgenres) và bản nhạc soundtrack.
   - Sinh sequence khớp nhịp biến thiên (Master + 3 biến thể).
3. `⚡ Auto Run All (1-Click Workflow)`:
   - Nút to nổi bật (Primary Action) chạy mượt mà xuyên suốt cả 3 giai đoạn.

### 3.3. `SceneGridWidget` (Hiển Thị Trực Quan Golden Takes)
- **Huy hiệu nhận diện trực quan**:
  - 🟢 `[Golden Take]`: Phân đoạn sạch đẹp.
  - 🟡 `[Trimmed: Wobble 0.8s]`: Đã cắt bỏ đoạn rung bấm nút.
  - 🔴 `[Excluded: Shake/Focus]`: Đoạn lỗi bị loại.
  - 🟣 `[Long Shot >15s]`: Cảnh dài chất lượng cao.
- **Tương tác**: Cho phép người dùng tick/untick từng cảnh trước khi bấm `✨ 2. Auto-Edit Sequence`.

### 3.4. `TimelineViewWidget` (Chuyển Đổi Variant & Vạch Nhịp Bar/Phrase)
- Danh sách Variant pills:
  - `[🔨 Rough Cut Stringout]`: Toàn bộ cảnh sạch theo thứ tự thời gian quay.
  - `[✨ Master Sequence]`: Bản dựng chính khớp nhịp biến thiên theo thể loại.
  - `[⚡ Beat-Driven]`: Biến thể dồn dập, giật beat.
  - `[🎬 Cinematic]`: Biến thể điện ảnh thư thái, shot dài >15s tỏa sáng.
  - `[📱 Social Teaser]`: Biến thể dọc viral 30s.
- Ruler hiển thị vạch nhịp: Đánh dấu rõ vạch Ô nhịp (Bar) và vạch Câu nhạc (Phrase 2-bar, 4-bar, 8-bar).

---

## 4. Kế Hoạch Chỉnh Sửa Mã Nguồn Chi Tiết

### Component 1: Pipeline Engine & Service Layer (`src/core/service.py`)
- **[MODIFY]** Tái cấu trúc hàm `_execute_pipeline`:
  - Đưa `RoughCutCleaner` thành Stage 2 bắt buộc cho mọi luồng dựng.
  - Thêm tham số `run_until_stage: int | None = None` vào `submit_job()`:
    - Nếu `run_until_stage=2`: Chạy đến hết Rough Cut, tạo `rough_cut` timeline rồi dừng lại để user review.
    - Nếu `run_until_stage=None` hoặc 3: Chạy tiếp Stage 3 (Sequence Edit theo thể loại & nhịp điệu).
  - Kết nối dữ liệu: `cleaned_golden_scenes` từ Stage 2 là đầu vào duy nhất cho Visual Scoring và Timeline Synthesis của Stage 3.

### Component 2: Core Domain & Types (`src/core/types.py`)
- **[MODIFY]** Thêm `CadenceProfile` enum:
  - `FAST_SYNCOPATED` (1/2 bar đến 2 bars)
  - `BALANCED_NARRATIVE` (1 bar đến 4 bars)
  - `LINGERING_CINEMATIC` (2 bars đến 8 bars)
  - `VIRAL_BURST` (1/2 bar đến 1 bar)
- **[MODIFY]** Thêm `MusicalBarGrid` dataclass:
  - `tempo_bpm: float`
  - `bar_timestamps: list[float]` (danh sách thời điểm bắt đầu từng ô nhịp / downbeat)
  - `phrase_2bar: list[float]`
  - `phrase_4bar: list[float]`
  - `phrase_8bar: list[float]`
- **[MODIFY]** Mở rộng `ScoredScene`:
  - Thêm trường `golden_segment: CleanedSegment | None` và `is_golden_take: bool = True`.

### Component 3: Cấu Hình & Ma Trận Thể Loại (`src/core/config.py` & `src/core/genre_presets.py`)
- **[MODIFY]** `PipelineConfig`:
  - `enable_rough_cut_pruning: bool = True` (mặc định bật bước lọc thô).
  - `cadence_profile: str = "balanced_narrative"`.
  - `extended_shot_threshold_sec: float = 12.0` (ngưỡng nhận diện cảnh dài >15s).
  - `max_scene_len_sec: float = 16.0`.
  - `allow_multi_segment_for_long_shots: bool = True` (cho phép trích xuất tối đa 2 đoạn nếu shot >30s).
- **[MODIFY]** `SubGenrePreset`:
  - Cập nhật toàn bộ 23 thể loại với `cadence_profile`, `target_bar_multiples` (ví dụ Cinematic = `[2, 4, 8]`, Extreme Sports = `[0.5, 1, 2]`), `allow_extended_shots = True`.

### Component 4: Phân Cấp Nhịp Lý Âm Nhạc (`src/modules/audio_analyzer.py`)
- **[MODIFY]** Thêm phương thức `extract_musical_bar_grid(...) -> MusicalBarGrid`.
- **[MODIFY]** Cải tiến `generate_variable_cut_grid(...)`:
  - Thay thế bước lọc subsampling phẳng `if interval >= req_interval` bằng bộ phân bổ tiết tấu động theo `CadenceProfile` và `SongSection` bám sát các mốc Downbeat.

### Component 5: Dựng Chuỗi Khớp Nhịp & Cảnh Dài (`src/modules/edit_timeline.py` & `src/modules/variant_synthesizer.py`)
- **[MODIFY]** Nâng cấp `select_scenes_for_cuts()` và `build_edit_timeline()`:
  - Nhận diện shot dài $\ge 15.0s$: Cấp phát slot 4 bars (~8s) hoặc 8 bars (~16s).
  - Nếu shot $>30.0s$ có nhiều đoạn đẹp: Cho phép trích xuất tối đa 2 đoạn xuất hiện ở 2 vị trí thích hợp trên timeline.
  - Thuật toán `extract_optimal_subwindow(scene, slot_duration)`: Quét sliding window chọn vùng chuyển động mượt và thẩm mỹ cao nhất.
  - Luân phiên thời lượng cảnh (ngắn - vừa - dài), loại bỏ hoàn toàn hiện tượng cắt đều 2s.
- **[MODIFY]** Cập nhật `MultiVariantTimelineSynthesizer`:
  - Thêm biến thể `rough_cut` bên cạnh `master`, `beat_driven`, `cinematic`, `social_teaser`.

### Component 6: Giao Diện Desktop Studio (`src/ui/`)
- **[MODIFY]** `src/ui/main_window.py`:
  - Cập nhật `_update_workflow_stepper` thành 4 bước.
  - Thêm slot `_on_run_rough_cut()`, `_on_run_sequence()`, `_on_run_full_auto()`.
  - Khi hoàn thành Stage 2: Tự động chuyển tab `self.tab_widget.setCurrentWidget(self.scene_grid)`, nạp timeline `rough_cut` lên player.
- **[MODIFY]** `src/ui/widgets/inspector_panel.py`:
  - Thiết kế cụm 3 nút: `🔨 1. Run Rough Cut`, `✨ 2. Auto-Edit Sequence`, `⚡ Auto Run All`.
- **[MODIFY]** `src/ui/widgets/scene_grid.py`:
  - Thêm cột và huy hiệu trạng thái `[Golden Take]`, `[Trimmed]`, `[Excluded]`, `[Long Shot]`.
- **[MODIFY]** `src/ui/widgets/timeline_view.py`:
  - Thêm pill `Rough Cut` trong variant bar và vẽ vạch chia ô nhịp (bar/phrase) trên ruler.

---

## 5. Kế Hoạch Xác Minh & Kiểm Thử (Verification Plan)

### Automated Test Suite
Chạy kiểm thử bằng môi trường ảo `.venv` cô lập:
```powershell
# 1. Kiểm thử Stage 2: Rough Cut Defect Pruning & Golden Takes
.venv\Scripts\python.exe -m pytest tests/test_rough_cut_cleaner.py -v --tb=short

# 2. Kiểm thử Stage 3: Nhịp lý âm nhạc & Phân cấp câu nhạc
.venv\Scripts\python.exe -m pytest tests/test_audio_structure.py tests/test_genre_presets.py -v --tb=short

# 3. Kiểm thử phân bổ shot dài >15s và không bị cắt đều 2s
.venv\Scripts\python.exe -m pytest tests/test_extended_shots_and_cadence.py -v --tb=short

# 4. Kiểm thử giao diện: Stepper 4 bước, nút phân chặng, tab switching
.venv\Scripts\python.exe -m pytest tests/test_gui_shell.py tests/test_inspector_and_player.py tests/test_timeline_view.py -v --tb=short

# 5. Kiểm thử tích hợp toàn bộ chuỗi 3 giai đoạn (Integration E2E)
.venv\Scripts\python.exe -m pytest tests/test_pipeline_e2e.py tests/test_service_adapter.py -v --tb=short

# 6. Chạy toàn bộ 207+ regression tests
.venv\Scripts\python.exe -m pytest tests/ -v --tb=short

# 7. Kiểm tra code styling và quy chuẩn Ruff
.venv\Scripts\python.exe -m ruff check src tests main.py
```

### Tiêu Chuẩn Nghiệm Thu Cụ Thể (Acceptance Criteria)
1. **Rough Cut Gate**: Toàn bộ scene đưa vào Sequence Edit đều là Golden Takes đã qua làm sạch lỗi.
2. **Loại Bỏ Cắt Đều**: Độ lệch chuẩn thời lượng shot $\sigma \ge 1.2s$. Không lặp lại thời lượng 2s đơn điệu.
3. **Bảo Tồn Shot Dài >15s**: Shot 20s-30s được cấp phát 8-16s (4-8 bars) và chọn đúng phân đoạn chuyển động mượt nhất.
4. **Trải Nghiệm GUI Mượt Mà**: Chạy phân chặng rõ ràng, tự động chuyển tab Scene Review khi xong Rough Cut, Program Monitor phát được cả Rough Cut lẫn Master Sequence.
5. **Không Phát Sinh Lỗi Hồi Quy**: 100% pass trên toàn bộ test suite hiện có, 0 lỗi linter ruff.
