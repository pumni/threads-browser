# Bàn giao Threads Browser — Hướng dẫn vận hành và tình trạng hiện tại

**Ngày chốt thông tin:** 19/10/2026  
**Repository:** https://github.com/pumni/threads-browser  
**Nhánh bàn giao:** `main` (khách hàng ghi lại mã commit thực tế bằng `git rev-parse HEAD` sau khi nhận bản bàn giao).  
**Mốc mã nguồn Auto V3 đã merge:** `75820ebc90141dc898614425b36f8711199372da`, từ [PR #53](https://github.com/pumni/threads-browser/pull/53). Tài liệu này được thêm sau mốc đó, nên mã HEAD của `main` sau khi merge tài liệu sẽ khác.  
**Trạng thái bàn giao:** **ĐÃ BÀN GIAO MÃ NGUỒN / ĐẠT KIỂM THỬ KỸ THUẬT; CHƯA ĐẠT NGHIỆM THU VẬN HÀNH AUTO TRÊN THREADS THẬT.** Không khẳng định hệ thống sẵn sàng chạy không giám sát.

> Tài liệu dành cho kỹ thuật và người vận hành của khách hàng. Mọi thao tác trình duyệt thật phải do người đã được cấp quyền thực hiện trên máy của mình. Không cần và không được gửi tài khoản, mật khẩu, cookie, profile trình duyệt hoặc nội dung Threads thật cho bên phát triển.

## 1. Phạm vi được bàn giao

- Ứng dụng Python CLI chạy với `uv`, Playwright và Chromium **có giao diện** trên desktop Windows.
- `tb add`, `tb ls`, `tb login`: đăng ký alias cục bộ, xem trạng thái và đăng nhập Threads thủ công bằng profile riêng.
- `tb run`: phiên đọc Home/For You ngắn, chọn bài theo **từ khóa người vận hành lưu**, không phải Auto V3.
- `tb session`: phiên đọc nhiều lượt Detail/Back, vẫn dùng cấu hình relevance có từ khóa, với ngân sách hữu hạn.
- `tb auto` (**Auto V3**): đọc Home/For You và phân loại **ý định tuyển dụng/tìm việc bằng tiếng Việt**, không yêu cầu cấu hình từ khóa. Ba nhóm tuyển dụng chính: thông báo có cấu trúc; lời tuyển dạng hội thoại có công việc nhận diện được; recruiter trực tiếp tiếp nhận ứng viên/CV cho đối tác. Ý định tìm việc rõ ràng của chính tác giả là nhóm riêng.
- Khi điều kiện xác minh cho phép, Auto chỉ thực hiện **Home → Detail → Back**. Có kiểm tra actor, permalink, tác giả, nội dung Home hoàn chỉnh, xác minh lại ngay trước Detail, chống đọc trùng và checkpoint. Không tự like, follow, bình luận, đăng bài, trả lời hoặc gửi tin nhắn. Không dùng LLM/API bên ngoài để chấm điểm ngữ nghĩa.

**Không nằm trong phạm vi đã nghiệm thu:** công cụ tìm kiếm toàn bộ Threads theo từ khóa; cam kết thấy mọi bài tuyển dụng; tự liên hệ nhà tuyển dụng; chạy nền 24/7; Task Scheduler tự động; vượt qua challenge, rate limit hoặc đăng nhập hộ khách hàng.

## 2. Điều kiện cài đặt

- Máy Windows với **desktop tương tác đang đăng nhập**, kết nối mạng và khả năng mở Chromium có giao diện; không chạy Auto khi desktop đã logoff.
- Cài sẵn `git` và `uv`, có quyền truy cập repository. Gói yêu cầu **Python >=3.13, <3.15**, Playwright được khóa tại **1.63.0** trong `pyproject.toml`; `uv sync --locked` sử dụng lockfile của dự án.
- Có tài khoản Threads do khách hàng quản lý và được phép sử dụng. Mỗi alias dùng một profile Chromium cục bộ độc lập.

Lần đầu cài trên một máy mới (đường dẫn dưới đây chỉ là **ví dụ**, tùy chọn nơi lưu code):

```powershell
git clone https://github.com/pumni/threads-browser.git D:\Apps\threads-browser
Set-Location D:\Apps\threads-browser
git switch main
git pull --ff-only origin main
git rev-parse HEAD
uv sync --locked
uv run playwright install chromium
uv run tb --help
```

Khi cập nhật bản đã cài: **đợi tiến trình và Chromium hiện tại đóng hẳn**, kiểm tra `git status --short`, bảo toàn mọi thay đổi cục bộ, rồi dùng `git pull --ff-only origin main`. **Không dùng `git reset --hard`, `git clean` hay xóa profile/checkpoint** để cập nhật.

## 3. Tạo và xác minh tài khoản cục bộ

Xem các alias đã có; chỉ tạo alias mới nếu trên máy chưa tồn tại:

```powershell
uv run tb ls
uv run tb add demo-local
uv run tb login demo-local
```

`demo-local` là alias **ví dụ**, không phải thông tin tài khoản bàn giao. Trong Chromium, khách hàng **tự đăng nhập và tự hoàn tất challenge**. Sau khi kiểm tra được tài khoản, **đóng cửa sổ Chromium của lệnh login** để trả profile lock; `uv run tb ls` cần cho thấy alias ở trạng thái `VERIFIED`. Nếu alias đã có, **không chạy lại `tb add`**.

Dữ liệu riêng mặc định lưu tại `%LOCALAPPDATA%\ThreadsBrowser` (có thể cấu hình bằng `THREADS_BROWSER_DATA_ROOT`). Thư mục này có thông tin đăng nhập/profile và checkpoint; **không đưa vào Git, không nén gửi khách hàng qua chat, không dùng chung giữa các máy/tài khoản**. Không để hai CLI cùng sử dụng một profile.

## 4. Chạy thử Auto V3 — ưu tiên trước khi bàn giao nghiệm thu

**Phải có người ngồi giám sát**, quan sát Chromium và PowerShell. Không chạy một lần vô hạn (`uv run tb auto demo-local` không giới hạn chu kỳ).

Thử **1 chu kỳ thành công tối đa**, sử dụng alias VERIFIED thực tế:

```powershell
uv run tb auto demo-local --max-cycles 1 --uat-diagnostics
$LASTEXITCODE
```

Sau khi đánh giá lượt 1, có thể chủ động thử 2 chu kỳ:

```powershell
uv run tb auto demo-local --max-cycles 2 --uat-diagnostics
$LASTEXITCODE
```

- Auto duyệt Home/For You, đọc và phân loại bài hiện trên feed, rồi có thể mở Detail và Back khi đủ điều kiện. Không bắt buộc có từ khóa lưu trước.
- **Mỗi phiên browser/cycle** giới hạn tối đa 20 lần cuộn, 100 bài quan sát riêng biệt, 5 lượt Detail và 10 phút thao tác browser. Đây là **giới hạn**, không phải mục tiêu phải dùng hết.
- `--max-cycles` đếm **chu kỳ hoàn thành**, không nhất thiết bằng số `UAT attempt`: một phiên không hoàn chỉnh có thể được retry hữu hạn theo stop code. Ví dụ 2 chu kỳ thành công có thể có 3 attempt.
- `--uat-diagnostics` chỉ xuất các mã phase, trạng thái, số đếm đã cho phép; cờ này **bắt buộc có** `--max-cycles`.
- Kết thúc: kiểm tra `$LASTEXITCODE`, checkpoint, trạng thái Back và Chromium do CLI khởi chạy **đã đóng hoàn toàn**.
- Không mở đồng thời trình duyệt dùng cùng profile; không chọn `--detail`, `--show-post-text` hoặc chụp/export nội dung bài khi tạo tài liệu nghiệm thu.

Các tín hiệu cần ghi **chỉ ở dạng tổng hợp**: `eligible`, `detail`, `scrolls`, `phase=COVERAGE`, `read_in_progress`, `phase=CHECKPOINT`, mã dừng, exit code, xác nhận browser đã đóng. `SESSION_BUDGET_EXHAUSTED` có thể là một chu kỳ hoàn thành nếu coverage và checkpoint hợp lệ; **không đồng nghĩa đã đọc được bài tuyển dụng**. `eligible=0` hoặc `detail=0` **không đủ để nghiệm thu đường Home→Detail→Back trên Threads thật**.

## 5. Chạy các chế độ khác khi khách hàng cần

`tb run` và `tb session` là **luồng relevance dựa trên từ khóa**, khác với Auto V3. Thiết lập từ khóa cục bộ trước khi dùng:

```powershell
uv run tb terms demo-local "đang tuyển" "tuyển dụng"
uv run tb run demo-local
uv run tb session demo-local --max-runtime-seconds 600 --max-scrolls 8 --max-total-posts 40 --max-detail-reads 4
```

Chỉ chạy `tb session` khi có người giám sát và cần nghiệm thu nhiều Detail/Back. Các cờ ngân sách của `tb session` **không phải** cờ của `tb auto`; không tự chuyển lệnh sang chạy vô hạn.

**Quan trọng về Scheduler:** file `scripts/windows/run-scheduled.ps1` hiện gọi **`tb run` theo từ khóa**, **không gọi Auto V3**. Sự tồn tại của wrapper **không có nghĩa Task Scheduler đã được bật hoặc được phép bật**. Chưa phê duyệt lịch chạy, retry tự động hoặc unattended Auto.

## 6. Xử lý mã dừng / sự cố tại máy

| Mã hoặc dấu hiệu | Thao tác an toàn của người vận hành |
| --- | --- |
| `ACCOUNT_NOT_VERIFIED` | Kiểm tra alias bằng `tb ls`, đăng nhập/xác minh bằng `tb login`. |
| `LOGIN_REQUIRED` | Dừng Auto; đăng nhập lại thủ công trên chính profile. |
| `ACCOUNT_BUSY` | Kiểm tra phiên CLI/Chromium khác đang dùng profile; không mở song song. |
| `CHALLENGE_REQUIRED` | Dừng, tự giải quyết challenge; không tự động vượt chặn. |
| `RATE_LIMITED` | Dừng, không retry liên tục; chờ đánh giá thủ công. |
| `DETAIL_ROOT_AMBIGUOUS`, `DETAIL_BODY_UNVERIFIED` | Không coi là đọc Detail thành công; ghi **mã lỗi và số đếm**, đối chiếu issue #47. |
| `SESSION_COVERAGE_INCOMPLETE`, `READ_IN_PROGRESS` | Không bỏ qua bài chưa đọc đủ; không tự sửa checkpoint. Đối chiếu issue #23. |
| `BROWSER_CLEANUP_FAILED` | **Ngừng sử dụng profile ngay.** Kiểm tra và xác nhận tiến trình Chromium do lần chạy đó đã tắt; không chạy lại khi còn nghi ngờ. |
| `CYCLE_TIMEOUT`, `ACTOR_MISMATCH` | Dừng; kiểm tra trạng thái máy/tài khoản và mã dừng trước khi quyết định thử lại. |

**Chỉ khi đã tự xác nhận managed Chromium đóng hoàn toàn** và profile còn bị cleanup guard chặn, người vận hành mới dùng:

```powershell
uv run tb recover demo-local
```

CLI yêu cầu gõ xác nhận `CLOSED`; **không xác nhận khi còn tiến trình chưa đóng**. Nếu guard hỏng hoặc không chắc chắn, chuyển kỹ thuật hỗ trợ; **không xóa/sửa file lock, profile hoặc checkpoint bằng tay**.

## 7. Tình trạng kiểm thử và nghiệm thu tại thời điểm bàn giao

| Hạng mục | Trạng thái và bằng chứng |
| --- | --- |
| Auto V3 classifier, test unit/synthetic Chromium | **Engineering PASS**, [PR #53 đã merge](https://github.com/pumni/threads-browser/pull/53). |
| CI trước merge Auto V3 | [Actions 39163661539](https://github.com/pumni/threads-browser/actions/runs/39163661539): Windows **448 passed**, Ubuntu **445 passed / 3 skipped**. |
| CI của `main` ngay sau merge Auto V3 | [Actions 39165543455](https://github.com/pumni/threads-browser/actions/runs/39165543455): **SUCCESS** Windows/Ubuntu tại commit `75820ebc90141dc898614425b36f8711199372da`. |
| Đăng nhập/đọc/chuyển Detail trên Threads thật | **Chưa hoàn tất xác minh V3 live positive path.** Synthetic test không thay thế nghiệm thu thực tế. |
| Auto chạy nền/Task Scheduler | **CHƯA ĐƯỢC PHÊ DUYỆT / PHẢI TẮT**. |
| Windows cleanup | **Rủi ro chưa đóng**: một số CI cũ từng timeout ở `CLOSE_CONTEXT` / `BROWSER_CLEANUP_FAILED`; CI xanh không phải RCA. |

**Dữ liệu UAT tổng hợp do người vận hành cung cấp (không kèm SHA chạy tại máy):** hai chu kỳ đã hoàn thành ghi nhận `new=27` và `new=21`, `eligible=0`, `detail=0`, coverage hoàn tất và checkpoint lưu được. Một attempt trung gian có `relevant=1` nhưng `read_in_progress=1`; coverage chưa hoàn tất, checkpoint không lưu, Detail không mở và hệ thống retry hữu hạn. Đây là **bằng chứng fail-closed an toàn**, **không chứng minh classifier đã đọc đúng hoặc bỏ sót bao nhiêu bài tuyển thật**. Do không có SHA của cài đặt cục bộ trong log, **không được coi các số này là đo lường độ chính xác của đúng bản V3 trên main**.

## 8. Giới hạn / lỗi đang mở

1. **[Issue #23](https://github.com/pumni/threads-browser/issues/23) — P1 Home coverage/final-scroll:** trạng thái `READ_IN_PROGRESS` hoặc vùng chưa quan sát hết có thể chặn Detail/checkpoint. Phải kiểm tra tiến trình coverage và phân biệt lookahead với bài thật sự chưa đọc xong; **không bỏ chốt coverage chỉ để tăng số Detail**.
2. **[Issue #47](https://github.com/pumni/threads-browser/issues/47) — P1 Detail identity/root:** có bằng chứng UAT trước đây về lượt Detail thứ hai `ROOT_AMBIGUOUS`; chưa có nghiệm thu nhiều lượt Detail thật thành công ổn định.
3. **[Issue #50](https://github.com/pumni/threads-browser/issues/50) — Ship gate:** engineering đã merge nhưng điều kiện nghiệm thu thực tế, vận hành/soak và quyết định phát hành chưa đóng.
4. **Classifier dựa trên mẫu và tập từ vựng nghề giới hạn:** có thể bỏ sót tin tuyển dụng tiếng Việt viết tự do, cách gọi nghề mới, bài liệt kê nhiều vị trí, bài chủ yếu dẫn tới biểu mẫu hoặc bài không có cấu trúc được hỗ trợ. Không cam kết 100% recall/precision. Không nên thêm regex riêng cho từng bài; cần đánh giá theo nhóm bài đại diện và nhãn do khách hàng xác nhận cục bộ.
5. **Nguồn Home/For You là feed cá nhân hóa, không phải search toàn Threads:** không gặp bài tuyển trong một lượt quét không đồng nghĩa classifier sai. Lặp nhiều lượt `eligible=0` cũng không chứng minh hệ thống bắt đúng bài tuyển.
6. **Windows cleanup intermittent, chưa có RCA:** từng xuất hiện trên nhiều bài test Chromium giả lập; phải xác minh shutdown thật và có RCA hoặc ngoại lệ vận hành được người sở hữu phê duyệt rõ ràng trước khi cho phép chạy unattended.
7. **Threads UI có thể thay đổi:** xác minh permalink/tác giả/body là cố ý fail-closed. Không sửa để click bài có danh tính không chắc chắn.

## 9. Checklist nghiệm thu của khách hàng (chưa đánh dấu là đã đạt)

- [ ] Khách hàng nhận code, ghi mã `git rev-parse HEAD`, cài `uv sync --locked` và Chromium thành công.
- [ ] Tự xác minh alias Threads trên desktop; chỉ một phiên/profile hoạt động tại một thời điểm.
- [ ] Chạy có giám sát `tb auto <alias> --max-cycles 1 --uat-diagnostics`, xác nhận hành vi đọc-only, mã thoát và Chromium tự đóng.
- [ ] Quan sát **ít nhất một bài tuyển/tìm việc hợp lệ thật sự được mở Detail và Back** với tác giả, permalink, nội dung phù hợp; không cung cấp nội dung thô vào báo cáo.
- [ ] Xác nhận không mở nhầm bài rõ ràng là repost, hỏi thông tin, đã hết hạn, khóa học hoặc sai tác giả, trên mẫu UAT thực tế.
- [ ] Xác nhận coverage, checkpoint/dedupe và các mã dừng an toàn với tối thiểu một lượt hoàn tất và một lượt gặp điều kiện không hoàn tất nếu phát sinh.
- [ ] Kiểm tra Chromium/process-tree đã đóng sau kết thúc; thống nhất phương án xử lý rủi ro Windows cleanup và các issue P1 còn mở.
- [ ] Kiểm tra rollback dựa trên **commit/version được phê duyệt**, không xóa dữ liệu; ghi nhận trách nhiệm, thời hạn xử lý tồn đọng và quyết định GO/NO-GO chính thức.
- [ ] **Task Scheduler vẫn tắt** cho đến khi có phê duyệt riêng bằng văn bản về tần suất, quyền truy cập, stop/retry, máy đang thức và desktop đã đăng nhập.

## 10. Rollback, bảo mật và đầu mối tài liệu

Trước rollback: dừng mọi CLI, chờ Chromium đóng, ghi lại phiên bản đang dùng. Kỹ thuật của khách hàng dùng commit/tag **được phê duyệt**, checkout trong working tree sạch rồi `uv sync --locked` và `uv run playwright install chromium`. **Không xóa checkpoint, profile, dữ liệu tài khoản hoặc chỉnh file khóa**; rollback code **không rollback dữ liệu đã ghi**. Nếu schema checkpoint của bản cũ không tương thích, dừng và chuyển người quản lý mã nguồn. Xem [OPERATIONS_WINDOWS.md](OPERATIONS_WINDOWS.md).

Tài liệu liên quan: [README.md](../README.md) (lệnh CLI), [OPERATIONS_WINDOWS.md](OPERATIONS_WINDOWS.md) (vận hành/khôi phục), [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md) (gate phát hành), [issue #23](https://github.com/pumni/threads-browser/issues/23), [issue #47](https://github.com/pumni/threads-browser/issues/47), [issue #50](https://github.com/pumni/threads-browser/issues/50).

**Bảo mật bàn giao:** chỉ giao **mã nguồn + tài liệu + liên kết CI/issue** qua kênh đã phân quyền. Khách hàng tự dùng tài khoản Threads riêng. Không đưa username/handle thực, post URL, bài viết thật, số điện thoại, email, ảnh màn hình, DOM, transcript PowerShell, cookie hay profile vào GitHub, tài liệu hoặc báo cáo chung. Có thể trao đổi **số đếm tổng hợp và mã dừng**. 

**Kết luận bàn giao:** Đã có mã nguồn và kiểm thử kỹ thuật cho **pilot đọc-only có giám sát**; **chưa đủ bằng chứng để nghiệm thu vận hành đầy đủ, bảo đảm chất lượng tìm bài tuyển dụng thật hoặc kích hoạt chạy tự động định kỳ**. Các mục ở checklist cần người phụ trách khách hàng ký xác nhận riêng.

