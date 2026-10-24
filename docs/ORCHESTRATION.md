# Điều phối Antigravity 2 từ Claude Code: cách làm thực tế (2026-10-24)

Tài liệu này mô tả **đúng những gì đã chạy** trong phiên Claude Code "Auto-curation rough cut review". Không có suy đoán. Chỗ nào chưa xác minh được thì ghi rõ "KHÔNG XÁC MINH ĐƯỢC". Mọi bí mật (CSRF token, GitHub token) đều đã che.

---

## 1. Công cụ dùng để giao việc cho AG2

**Không dùng:** `agy`, pywinauto, computer use, hay bất kỳ MCP server nào.

**Có dùng:** CLI chính thức `agentapi` mà Antigravity 2 cài kèm, gọi qua một script PowerShell do tôi viết.

### 1.1 Tìm ra `agentapi`
- File `C:\Users\firef\.gemini\antigravity\bin\agentapi.bat` có nội dung:
  ```bat
  @echo off
  "C:\Users\firef\AppData\Local\Programs\antigravity\resources\bin\language_server.exe" agentapi %*
  ```
- Lệnh `agentapi.bat --help` in ra:
  ```
  Usage: agentapi <command> [args]
    get-conversation-metadata <conversation_id>
    new-conversation [--model=<flash_lite|flash|pro>] [--title=<title>] [--profile=<profile>] <prompt>
    send-message [--title=<title>] <recipient_id> <content>
  ```
- `agentapi` cần 3 biến môi trường. Tên biến được tìm bằng cách grep chuỗi trong `language_server.exe`, và đã kiểm chứng bằng thông báo lỗi khi thiếu biến:
  - `ANTIGRAVITY_LS_ADDRESS`: địa chỉ language server của AG2 đang chạy. Khi thiếu, lỗi trả về là `{"error": "ANTIGRAVITY_LS_ADDRESS is not set"}`.
  - `ANTIGRAVITY_CSRF_TOKEN`: token nằm trong dòng lệnh của tiến trình `language_server.exe --subclient_type hub ... --csrf_token <…>`. Khi thiếu, lỗi trả về là `rpc error: code = Unauthenticated desc = unauthenticated: missing CSRF token`.
  - `ANTIGRAVITY_PROJECT_ID`: ID project AG2 gắn với workspace TeamTrau. Khi thiếu, lỗi trả về là `project_id is required when providing project_env_config`. Giá trị `ecfb0b37-3969-456b-aa52-e46d304162bf` được đọc từ `~/.gemini/antigravity/agyhub_summaries_proto.pb` và được xác nhận lại bằng trường `"projectId"` trong `get-conversation-metadata`.

### 1.2 Script cầu nối `.claude/orchestrator/ag2-send.ps1`
Script làm 5 việc:
1. Tìm tiến trình `language_server.exe` có `--subclient_type hub`.
2. Đọc `--csrf_token` từ dòng lệnh của tiến trình đó, và lấy các cổng loopback mà nó đang nghe (`Get-NetTCPConnection`).
3. Đặt 3 biến môi trường trên **chỉ trong tiến trình của script**.
4. Gọi `language_server.exe agentapi …`.
5. Thử lần lượt từng cổng. Thực tế: cổng 61990 là API, còn 61989 trả về `error reading server preface: EOF`.

Các chế độ của script:

| Chế độ | Lệnh agentapi bên dưới |
|---|---|
| `-PromptFile <file> -Title <t> -Model pro` | `new-conversation --model=pro --title=<t> "<message>"`. Message có dạng `Workspace: E:\antigravity\TeamTrau-AutoEdit. Read the file '<file>' and follow its instructions exactly. …` |
| `-Check` | `get-conversation-metadata 00000000-…`, chỉ để kiểm tra kết nối và xác thực |
| `-StatusOf <conversationId>` | `get-conversation-metadata <id>` |
| `-MessageTo <conversationId> -MessageFile <file>` | `send-message <id> "<nội dung file>"` |

### 1.3 Các lệnh đã chạy trong phiên này (trích nguyên văn)

**Lần thử đầu, KHÔNG thành công.** Gửi qua Antigravity **IDE** (bản cũ, không phải AG2):
```powershell
Get-Content -Raw -Encoding utf8 $p | & "$env:LOCALAPPDATA\Programs\Antigravity IDE\bin\antigravity-ide.cmd" chat -m agent -r "Follow the instructions in the attached text exactly." -
```
Lệnh trả về exit 0, nhưng không có bằng chứng nào cho thấy agent trong IDE đã chạy task. Commit T2-fix sau đó là do người dùng **dán tay** vào AG2.

**Hai lần bị auto mode chặn:**
- Gọi `agentapi get-conversation-metadata` với CSRF token tự gán: bị chặn với lý do "Credential Exploration".
- Ghi file script lần đầu: bị chặn với lý do "Create Unsafe Agents".
- Sau đó người dùng chọn: chuyển phiên sang chế độ Manual, cho phép ghi `.claude/settings.local.json`, rồi quay lại Auto.

**Các lệnh thành công** (chạy bằng tool PowerShell của Claude Code):
```powershell
pwsh -NoProfile -File .claude\orchestrator\ag2-send.ps1 -Check
# -> OK: Antigravity 2 agent API reachable on port 61990
pwsh -NoProfile -File .claude\orchestrator\ag2-send.ps1 -PromptFile .claude\orchestrator\T4.prompt.md -Title "TeamTrau T4" -Model pro
# -> {"response":{"newConversation":{"prompt":"Workspace: …T4.prompt.md…","conversationId":"55356580-5602-4a34-ac73-7266a3cb3e6c"}}}
```
Lệnh `-PromptFile` thành công cho các task sau:

| Task | conversationId |
|---|---|
| T4 | `55356580-…` |
| T5 | `2f7ea47c-…` |
| T6 | `393b5f33-…` |
| T7 | `f0342430-…` |
| S0.1 | `930b53f0-…` |
| S0.2 | `de1263e2-…` |
| S0.3 | `1edfb824-…` |
| S0.4 | `48a027ca-…` |

Lần gửi T4 đầu tiên thất bại vì thiếu project id. Lỗi trả về là `project_id is required…`; sau đó tôi thêm `-ProjectId`.

**Do người dùng dán tay vào AG2** (không do tôi gửi): T0, T1, T1b, T2, T2-fix, T3.

**Gửi tin nhắn tiếp vào một conversation đang chạy** (gỡ treo S0.3):
```powershell
pwsh -NoProfile -File .claude\orchestrator\ag2-send.ps1 -MessageTo 1edfb824-8490-4be3-9b42-e8882e4a33d5 -MessageFile .claude\orchestrator\S0.3-nudge.md
# -> {"response":{"sendMessage":{"recipientId":"1edfb824-…","content":"Architect note for S0.3: …"}}}
```

---

## 2. Cấu hình liên quan (nội dung thật)

| File | Nội dung liên quan |
|---|---|
| `.claude/settings.json` (trong project) | **Không tồn tại.** |
| `.claude/settings.local.json` | Chỉ có một quy tắc: `"allow": ["PowerShell(pwsh -NoProfile -File .claude\\orchestrator\\ag2-send.ps1:*)"]`. Nhờ quy tắc này, lệnh gửi task chạy được khi phiên ở chế độ Auto. |
| `.mcp.json` | **Không tồn tại.** |
| `~/.claude.json` → `mcpServers` | Toàn cục có 3 server: `blender` (uvx mcp-for-blender), `caveman` (caveman-mcp.exe), `autoflowcut` (node …\AutoFlowCut\…\mcp-server\index.js, kết nối đang lỗi). **Không có server nào liên quan Antigravity.** Không có mục mcpServers riêng cho project TeamTrau. |
| `CLAUDE.md` (trong project) | **Không tồn tại.** File toàn cục `~/.claude/CLAUDE.md` chỉ nói về CodeGraph, không nhắc tới Antigravity. |
| `AGENTS.md` | Là luật chung cho mọi agent: bootstrap bằng `memory_tool.py`, danh sách 18 subagent dùng qua `invoke_subagent`, các lệnh bị cấm. **Không có cơ chế điều khiển AG2 từ bên ngoài.** Mỗi prompt tôi gửi đều yêu cầu AG2 đọc file này trước. |
| `.agent/scripts/` | `memory_tool.py`, `test_memory_upgrade.py`, `watcher.py`. Đây là bộ nhớ dự án dành cho agent. Grep `agentapi`, `send-message`, `new-conversation`: **không thấy**. |
| `.agent/subagents/` | 18 file prompt (coder, tester, planner…). Đây là prompt cho subagent bên trong Antigravity, **không phải** kênh để Claude điều khiển AG2. |
| `~/.gemini/antigravity/mcp/` | Các MCP server mà AG2 dùng: blender, chrome_devtools, cinai, codebase-memory, codegraph, filmcraft. Claude **không** dùng những server này để giao việc. |

---

## 3. Luồng dữ liệu

```
Claude (phiên Claude Code)
  1. Viết prompt: .claude/orchestrator/<Task>.prompt.md   (git bỏ qua)
  2. Chạy ag2-send.ps1 -PromptFile …  ->  agentapi new-conversation (HTTP/gRPC loopback 127.0.0.1:61990)
                                              │
Antigravity 2 (desktop, language_server hub)  ▼
  3. Tạo conversation mới trong project ecfb0b37…, workspace E:\antigravity\TeamTrau-AutoEdit
  4. Agent đọc file prompt, sửa code, chạy test và lệnh trên máy, rồi commit git
  5. Ghi HANDOFF: docs/superpowers/handoffs/HANDOFF_<Task>.md trong cùng commit cuối
                                              │
Claude                                        ▼
  6. Một tiến trình nền (Bash run_in_background) poll git mỗi 30–60 giây:
       git log … -- docs/superpowers/handoffs/HANDOFF_<Task>.md   (hoặc grep message commit)
     Khi thấy commit thì tiến trình thoát, và Claude Code nhận task-notification.
  7. Review: so file test khoá với bản trích từ plan (diff từng byte), đọc git diff,
     tự chạy pytest / full suite / ruff / vitest / build, xem ảnh chụp màn hình.
  8. Kết quả GO thì gửi task tiếp theo (bước 1). Kết quả FIX thì gửi prompt sửa (new-conversation hoặc send-message).
```

- **Kết quả trả về qua:** commit git, file HANDOFF, báo cáo spike `docs/spikes/*.md`, ảnh `docs/spikes/img/*`. `agentapi` **không** trả về câu trả lời của agent; stdout chỉ có `conversationId`.
- **Biết task đã xong bằng cách:** có commit chứa file HANDOFF. Không có callback nào từ AG2.
- **Biết agent bị treo bằng cách:**
  - Đọc thời gian sửa file và mấy dòng cuối của `~/.gemini/antigravity/brain/<conversationId>/.system_generated/logs/transcript.jsonl` và `…/tasks/task-<n>.log` (che token khi in).
  - Từ S0.4, watcher tự báo khi transcript mới nhất **im lặng quá 45 phút**.
- `get-conversation-metadata` **không** chứa trạng thái running/idle. Nó chỉ có workspace, branch, `projectId`, `createdAt`, cấu hình.

---

## 4. Model AG2 đang dùng

- Khi gửi, tôi chọn `--model=pro`. `agentapi` chỉ cho 3 lựa chọn: `flash_lite`, `flash`, `pro`.
- Metadata của conversation S0.3 ghi `"planModel": "MODEL_PLACEHOLDER_M322"`. File `~/.gemini/antigravity/antigravity_state.pbtxt` ghi `last_selected_agent_model: MODEL_PLACEHOLDER_M318`.
- **KHÔNG XÁC MINH ĐƯỢC tên model thật** đứng sau "pro" hay sau các mã M322/M318. AG2 chỉ lộ mã placeholder. Người dùng từng nói plan v1 do "Gemini Flash 3.8 chế độ high" viết; điều này chưa được kiểm chứng từ dữ liệu.
- Với các task dán tay (T0–T3), model là model đang chọn trong giao diện AG2 lúc đó. Không biết được từ phía Claude.

---

## 5. Điểm yếu và giới hạn

1. **Phụ thuộc chi tiết nội bộ không công khai.** Tên biến môi trường, cờ `--subclient_type hub`, cách lấy CSRF token từ dòng lệnh tiến trình, và project id lấy từ file `.pb` đều có thể đổi khi AG2 cập nhật. Khi đó script hỏng mà không báo trước.
2. **Đụng tới thông tin xác thực.** Script đọc CSRF token của một tiến trình khác. Auto mode của Claude Code đã chặn việc này hai lần; nó chỉ chạy được nhờ quy tắc cho phép người dùng đặt trong `settings.local.json`.
3. **Không có kênh trả kết quả.** `agentapi` không trả nội dung và không báo xong. Claude phải poll git và đọc log nội bộ trong `brain/`, vốn là định dạng không chính thức.
4. **Không biết trạng thái agent.** Không phân biệt được agent đang chạy, đang chờ người dùng duyệt lệnh, hay đã kẹt. S0.3 đã bị treo khoảng 1,5 giờ vì script đo của chính nó bị treo.
5. **Phụ thuộc AG2 đang mở.** Language server phải đang chạy, và cổng được cấp động.
6. **Chất lượng báo cáo của AG2 không tự tin được.** Đã gặp các trường hợp:
   - Ghi `DEVIATIONS: none` dù đã phát hiện OpenCV 5 thiếu `CascadeClassifier` (T2).
   - SHA commit trong HANDOFF không khớp `git log` (S0.1, S0.2).
   - Ghi sai phiên bản Chromium (S0.1).
   - Không commit script đo (S0.1).
   - Đánh dấu mọi mục tiêu là "đạt" dù số liệu cho thấy không đạt (S0.3).

   Vì vậy Claude luôn tự chạy lại test và đọc diff, không tin báo cáo suông.
7. **Không chạy song song an toàn.** Hai conversation cùng commit vào một working tree hay một branch sẽ xung đột. Các spike vì thế được chạy tuần tự.
8. **Rò rỉ bí mật.** `get-conversation-metadata` trả về `gitOriginUrl` có chứa GitHub PAT, vì URL remote `origin` đang nhúng token. Người dùng đã được báo và hẹn thu hồi sau. Không được dán output metadata thô vào tài liệu hay chat.
9. **Dán tay vẫn cần cho một số trường hợp.** Đó là khi script không dùng được (AG2 đóng, API đổi), hoặc khi người dùng muốn tự theo dõi trong giao diện AG2.

---

## Phụ lục: file liên quan
- `.claude/orchestrator/ag2-send.ps1`: script cầu nối.
- `.claude/orchestrator/*.prompt.md`: prompt từng task. `S0.3-nudge.md` là tin nhắn gỡ treo.
- `.claude/settings.local.json`: quy tắc cho phép chạy script.
- `docs/superpowers/handoffs/HANDOFF_*.md`: báo cáo do AG2 viết.
- `docs/superpowers/plans/2026-10-23-auto-curation-v2-antigravity-prompts.md`: MASTER PROMPT (luật R1–R12).
