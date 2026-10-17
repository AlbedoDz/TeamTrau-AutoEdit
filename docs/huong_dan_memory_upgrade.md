# Hướng Dẫn Chi Tiết Hệ Thống Bộ Nhớ Nâng Cao (Memory Harness Pro Upgrade)

Tài liệu này hướng dẫn chi tiết từng bước (Step-by-Step) cách thức hoạt động của 5 giải pháp bộ nhớ nâng cao đã được viết thử nghiệm (Prototype) và kiểm thử thành công trên nhánh `feature/memory-harness-upgrade`.

---

## 1. Tổng Quan Kiến Trúc

Hệ thống nâng cấp bộ nhớ được thiết kế theo triết lý **Karpathy First-Principles** (không thư viện ngoài, tối ưu hiệu năng) và cơ chế **Poka-Yoke** (chống lỗi từ thiết kế). Toàn bộ mã nguồn thử nghiệm nằm tại tệp tin [.agent/scripts/test_memory_upgrade.py](file:///C:/Users/firef/Documents/antigravity/joyful-babbage/.agent/scripts/test_memory_upgrade.py).

---

## 2. Chi Tiết Từng Bước Hoạt Động (Step-by-Step)

### Giải Pháp 1: Đồ Thị Trạng Thái (State Graph Node Log)
*   **Mục đích:** Ghi lại lịch sử thực thi của Agent dưới dạng cây/đồ thị thay vì danh sách phẳng, giúp Agent tiếp theo hiểu rõ luồng quyết định.
*   **Cách hoạt động từng bước:**
    1.  Khởi tạo đối tượng `StateGraph` trỏ tới tệp tin `state.json`.
    2.  Khi Agent thực hiện một hành động (ví dụ: tạo file, sửa code), hàm `add_node` được gọi với các tham số: `node_id` (mã nút), `parent_id` (nút cha trực tiếp), `action` (hành động), và `affected_files` (các file bị tác động).
    3.  Hệ thống ghi nhận thời gian thực hiện (`timestamp`) và lưu cấu trúc liên kết nút cha-con vào file JSON.
    4.  Khi Agent mới bắt đầu, nó sẽ duyệt đồ thị này từ nút gốc (`parent_id = null`) để vẽ lại bản đồ lịch sử tác vụ.

### Giải Pháp 2: Thuật Toán Tìm Kiếm BM25 Cục Bộ (BM25 Local Search Engine)
*   **Mục đích:** Tìm kiếm bài học kinh nghiệm (`lessons.json`) chính xác dựa trên độ tương đồng từ khóa, khắc phục nhược điểm của so khớp chuỗi con thông thường.
*   **Cách hoạt động từng bước:**
    1.  **Tách từ (Tokenization):** Toàn bộ văn bản của bài học (chủ đề, nội dung bài học, thẻ tags) được đưa về dạng chữ thường và phân tách thành danh sách các từ đơn lẻ qua biểu thức chính quy `re.findall(r'\w+', text)`.
    2.  **Tính toán IDF (Inverse Document Frequency):** Hệ thống quét qua toàn bộ cơ sở dữ liệu bài học để đếm số lượng văn bản chứa từng từ. Từ nào càng xuất hiện phổ biến thì giá trị phạt (IDF) càng cao, từ nào hiếm gặp thì giá trị ưu tiên càng lớn.
    3.  **So khớp và xếp hạng (Scoring & Ranking):** Khi nhận câu truy vấn từ người dùng, thuật toán sẽ tách từ câu truy vấn đó và tính điểm cho từng tài liệu dựa trên tần suất xuất hiện của từ khóa (`TF`), có tính toán bù trừ theo độ dài trung bình của toàn bộ tài liệu trong hệ thống (`avg_doc_len`).
    4.  Sắp xếp danh sách tài liệu theo điểm số giảm dần và trả về kết quả khớp nhất.

### Giải Pháp 3: Theo Dõi Chi Tiết Thay Đổi File (File-Delta Line-Stat)
*   **Mục đích:** Đo lường quy mô thay đổi của code (thêm bao nhiêu dòng, bớt bao nhiêu dòng) để giúp Agent Coordinator đánh giá độ phức tạp.
*   **Cách hoạt động từng bước:**
    1.  Watcher ngầm chạy lệnh hệ thống: `git diff --numstat` thông qua module `subprocess`.
    2.  Lệnh này trả về kết quả dạng: `[số dòng thêm] [số dòng xóa] [đường dẫn file]`.
    3.  Hàm `get_git_file_deltas` phân tách chuỗi kết quả theo dòng và khoảng trắng để lấy các giá trị số nguyên.
    4.  Cập nhật thông tin chi tiết này vào trường `files_mapping` của `state.json` (ví dụ: `"src/main.py": { "added": 25, "deleted": 10 }`).

### Giải Pháp 4: Tự Động Giám Sát Ngưỡng Ngữ Cảnh (Context Compaction Check)
*   **Mục đích:** Cảnh báo và tự động kích hoạt nén dữ liệu khi kích thước file bộ nhớ tăng quá cao, tránh tràn ngữ cảnh Agent.
*   **Cách hoạt động từng bước:**
    1.  Hệ thống quét qua danh sách các file bộ nhớ đang hoạt động (`state.json`, `lessons.json`) và đọc kích thước tệp tin (tính bằng bytes).
    2.  Ước lượng số lượng Token theo tỷ lệ trung bình của định dạng JSON: `số_tokens = tổng_kích_thước_tính_bằng_bytes / 4`.
    3.  So sánh số lượng token ước lượng với ngưỡng giới hạn (`threshold`).
    4.  Nếu vượt ngưỡng, hàm trả về giá trị `should_compact = True`, từ đó watcher ngầm sẽ tự động gọi lệnh `memory_tool.py compress` để nén các file JSON thành một dòng duy nhất (loại bỏ khoảng trắng và xuống dòng), tiết kiệm token tối đa.

### Giải Pháp 5: Đồng Bộ Nhánh Ẩn (Orphan Branch Sync Protocol)
*   **Mục đích:** Chia sẻ bộ nhớ Agent xuyên suốt giữa các máy phát triển và các nhánh code mà không ảnh hưởng tới mã nguồn chính.
*   **Cách hoạt động từng bước:**
    1.  Watcher ngầm tự động cấu hình một remote Git tạm thời hướng tới kho lưu trữ ẩn.
    2.  Watcher chạy lệnh tạo nhánh độc lập hoàn toàn không có lịch sử chung với code chính: `git checkout --orphan agent-memory-harness`.
    3.  Các file trong thư mục `.agent/memory/` sẽ được tự động add và commit lên nhánh này.
    4.  Watcher thực hiện `git push` ngầm để lưu trữ bộ nhớ và `git pull` khi khởi chạy bootstrap để cập nhật trạng thái bộ nhớ mới nhất từ xa.

---

## 3. Cách Khởi Chạy Kiểm Thử & Benchmark

Để chạy thử nghiệm và kiểm tra tính đúng đắn của 5 giải pháp trên môi trường Windows 11, thực hiện các lệnh sau trong thư mục dự án:

1.  **Khởi chạy chương trình đo hiệu năng (Benchmark Script):**
    ```powershell
    uv run python .agent/scripts/test_memory_upgrade.py
    ```
    *Kết quả mong đợi:* Hệ thống hiển thị điểm BM25 cho từ khóa tìm kiếm, danh sách file thay đổi từ Git diff, và trạng thái handshake đồng bộ nhánh ẩn thành công.

2.  **Khởi chạy bộ kiểm thử tự động (Unit Tests):**
    ```powershell
    uv run pytest tests/test_memory_harness.py
    ```
    *Kết quả mong đợi:* Toàn bộ 5 ca kiểm thử đại diện cho 5 giải pháp vượt qua thành công (`5 passed`).
