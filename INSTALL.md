# Installing hermes-wikiskill

*(Tiếng Việt ở nửa sau của file này: [Hướng dẫn cài đặt](#hướng-dẫn-cài-đặt-tiếng-việt).)*

`hermes-wikiskill` teaches a [Hermes Agent](https://github.com/NousResearch/hermes-agent) new skills
from your own graded, repeated tasks, and keeps a skill **only if it measurably raises the score**.
The installer is non-destructive. It **never** touches:
- your `~/.hermes/config.yaml`, `.env` or `auth.json`;
- your existing skills and profiles;
- your gateway or the Hermes installation itself.

## 1. Before you start

Check on the machine where Hermes runs:

```bash
command -v hermes          # Hermes Agent is installed and on PATH (tested with v0.21.3)
python3 --version          # 3.10 or newer
python3 -m venv --help >/dev/null && echo venv-ok   # Ubuntu: sudo apt install python3-venv
command -v git
```

Your `default` Hermes profile must already work with an LLM provider. The installer copies **only**
that provider's key into a new, separate profile named `wikiskill`. If your key lives in another
profile, add `--source-profile NAME` to the install command.

## 2. Install (4 commands)

```bash
git clone https://github.com/vietdang7/hwiki.git ~/ops/hermes-wikiskill-src
bash ~/ops/hermes-wikiskill-src/install.sh --dry-run    # checks + plan, writes nothing
bash ~/ops/hermes-wikiskill-src/install.sh --yes
~/.local/bin/hws doctor --profile wikiskill             # expect: every check "ok"
```

The installer creates:
- a venv in `~/ops/hermes-wikiskill/venv`;
- the commands `~/.local/bin/hermes-wikiskill` and `~/.local/bin/hws`;
- a separate Hermes profile `wikiskill`. It has no gateway, no messaging tokens and only the
  provider key, and it holds the operator skill `hermes-wikiskill`.

Running it again is safe. It prints `no changes`.

If `hws` is "not found", add `~/.local/bin` to your `PATH`, or call `~/.local/bin/hws`.

## 3. Or: let your Hermes agent install it

Paste this into a Hermes chat that has the terminal tool enabled:

```text
Please install hermes-wikiskill for me, following its INSTALL.md exactly:
1. git clone https://github.com/vietdang7/hwiki.git ~/ops/hermes-wikiskill-src
   (if that folder already exists, run `git -C ~/ops/hermes-wikiskill-src pull` instead)
2. bash ~/ops/hermes-wikiskill-src/install.sh --dry-run   and show me the plan
3. bash ~/ops/hermes-wikiskill-src/install.sh --yes
4. ~/.local/bin/hws doctor --profile wikiskill            and show me the result
Rules: do NOT use --i-am-owner or --replace, do NOT edit anything under ~/.hermes yourself,
do NOT restart any gateway. If a step fails, stop and show me the exact error.
```

## 4. Check that it works

```bash
hermes -p wikiskill chat -Q -q "Is hermes-wikiskill healthy? Run the doctor and summarize."
```

The agent in the `wikiskill` profile carries the operator skill. It answers from real command output
(`hws doctor`, `hws status`, `hws report`) instead of guessing.

## 5. First experiment (optional, costs roughly US$0.1–0.3)

The bundled `house-report` bench teaches a company report format that the model cannot guess. It
proves the learning loop works on your host:

```bash
bash ~/ops/hermes-wikiskill-src/benches/house-report/run-effectiveness.sh eff1
hws status eff1
hws report eff1 --html /tmp/hws-dashboard.html     # open it in a browser (works offline)
```

On the reference host, held-out test tasks went from **0.0 without the skill to 1.0 with it**.

## 6. Teach it your own recurring task

Write a JSON list of tasks. Each task has:
- an `id`;
- a `split`: `train` to learn from, `val` for the gate;
- a `prompt`;
- optional input files in `sandbox`;
- a `grader`.

```json
[
  {"id": "train-01", "split": "train", "title": "Order total 01",
   "prompt": "Read orders.csv and write the total to result.txt as: TOTAL: <number>",
   "sandbox": {"orders.csv": "id,amount\nA,100\nB,250\n"},
   "grader": {"type": "exact", "file": "result.txt", "expected": "TOTAL: 350"}},
  {"id": "val-01", "split": "val", "title": "Order total 02",
   "prompt": "Read orders.csv and write the total to result.txt as: TOTAL: <number>",
   "sandbox": {"orders.csv": "id,amount\nC,40\nD,60\n"},
   "grader": {"type": "exact", "file": "result.txt", "expected": "TOTAL: 100"}}
]
```

The graders are:

| Grader | Passes when |
|---|---|
| `exact` | the file equals the expected text |
| `contains` | the file contains a `needle` |
| `json_field` | a field in a JSON file has the expected value |
| `code_stdout` | a script exits 0 and prints the expected output |

Use several tasks per split; the bundled bench uses 8 train and 6 val. You can write val answers
in plain text: `hws init` **seals** them (hashes them), so the agent can never read them.

```bash
hws init mytask --tasks ~/my-tasks.json
hws evolve mytask --iters 0      # baseline: score with no skills
hws evolve mytask --iters 3      # learn (costs money; add --dry-run to preview)
hws status mytask                # r_best, ACCEPT/rejected per iteration, active skills
```

A rejected iteration or a `no_action` iteration is normal. It means the proposed skill did not
raise the score, so the gate threw it away.

## 7. Use a learned skill in everyday chats (owner decision)

Learned skills stay inside their workspace until **you** promote them. Always preview first, and try
a throwaway profile before the real one:

```bash
hws promote mytask --profile hws-test-try --dry-run
hws promote mytask --profile hws-test-try

hws promote mytask --profile default --dry-run
hws promote mytask --profile default --i-am-owner   # backup + receipt; refuses name clashes
hws demote  --profile default --last --i-am-owner   # undo: skills dir restored byte for byte
```

Optional: your main agent can also get the operator skill, so it can answer questions about
workspaces:

```bash
bash ~/ops/hermes-wikiskill-src/install.sh --yes --profile default --i-am-owner   # only adds one skill
hws setup-profile --profile default --remove --i-am-owner                         # undo
```

## 8. Upgrade and uninstall

```bash
git -C ~/ops/hermes-wikiskill-src pull && bash ~/ops/hermes-wikiskill-src/install.sh --yes

bash ~/ops/hermes-wikiskill-src/uninstall.sh --dry-run        # preview
bash ~/ops/hermes-wikiskill-src/uninstall.sh --yes            # keeps workspaces
bash ~/ops/hermes-wikiskill-src/uninstall.sh --yes --purge    # removes workspaces too
```

Run `demote` before uninstalling if you promoted skills. The uninstaller:
- backs everything up to `~/ops/hermes-wikiskill-backups/` first;
- never deletes a profile it did not create;
- stops rather than delete the operator skill if you edited it. It saves your edited copy first and
  prints the path. Run the same command again to finish.

## 9. Troubleshooting

| You see | Meaning | Do |
|---|---|---|
| exit 75 / `workspace busy` | Another run holds the workspace | Wait, or add `--wait`. Never delete lock files. |
| exit 3 | The agent could not see the active skills in that run | `hws doctor --profile wikiskill`. Check the skill folder name matches `name:` in its `SKILL.md`. |
| exit 4 | This Hermes version is not compatible | `hws doctor` and look at `hermes-version` / `hermes-flags`. |
| exit 2 | Wrong command syntax | `hws <verb> --help` |
| `refusing to overwrite ~/.local/bin/hws` | Something else already owns that name | Rename or remove it yourself. |
| `no changes` | Already installed and up to date | Nothing to do. |

The full reference is in [README.md](README.md) (English) and [README.vi.md](README.vi.md) (Vietnamese,
with an FAQ).

---

# Hướng dẫn cài đặt (tiếng Việt)

`hermes-wikiskill` giúp agent Hermes **học skill mới từ chính những việc lặp lại, có chấm điểm của
anh/chị**. Nó chỉ giữ skill khi điểm **tăng thật**.

Trình cài **không bao giờ** đụng vào:
- `config.yaml`, `.env`, `auth.json`;
- các skill và profile đang có;
- gateway;
- bản cài Hermes.

## 1. Chuẩn bị

Chạy trên máy đang chạy Hermes:

```bash
command -v hermes          # đã cài Hermes Agent (đã test với v0.21.3)
python3 --version          # 3.10 trở lên
python3 -m venv --help >/dev/null && echo venv-ok   # Ubuntu: sudo apt install python3-venv
command -v git
```

Profile `default` của Hermes phải đang dùng được một nhà cung cấp LLM. Trình cài **chỉ chép key của
nhà cung cấp đó** sang một profile riêng tên `wikiskill`. Nếu key nằm ở profile khác, thêm
`--source-profile TÊN` vào lệnh cài.

## 2. Cài đặt (4 lệnh)

```bash
git clone https://github.com/vietdang7/hwiki.git ~/ops/hermes-wikiskill-src
bash ~/ops/hermes-wikiskill-src/install.sh --dry-run    # chỉ kiểm tra + in kế hoạch, không ghi gì
bash ~/ops/hermes-wikiskill-src/install.sh --yes
~/.local/bin/hws doctor --profile wikiskill             # mọi dòng phải là "ok"
```

Chạy lại lần nữa vẫn an toàn: nó báo `no changes`. Nếu gõ `hws` báo không tìm thấy, thêm
`~/.local/bin` vào `PATH` hoặc gõ đường dẫn đầy đủ `~/.local/bin/hws`.

## 3. Hoặc nhờ chính agent Hermes cài giúp

Dán đoạn sau vào một cuộc chat Hermes có bật công cụ terminal:

```text
Hãy cài hermes-wikiskill cho tôi, làm đúng theo INSTALL.md của nó:
1. git clone https://github.com/vietdang7/hwiki.git ~/ops/hermes-wikiskill-src
   (nếu thư mục đã có thì chạy `git -C ~/ops/hermes-wikiskill-src pull`)
2. bash ~/ops/hermes-wikiskill-src/install.sh --dry-run   rồi cho tôi xem kế hoạch
3. bash ~/ops/hermes-wikiskill-src/install.sh --yes
4. ~/.local/bin/hws doctor --profile wikiskill            rồi cho tôi xem kết quả
Luật: KHÔNG dùng --i-am-owner hay --replace, KHÔNG tự sửa gì trong ~/.hermes,
KHÔNG khởi động lại gateway. Bước nào lỗi thì dừng lại và cho tôi xem nguyên văn lỗi.
```

## 4. Kiểm tra

```bash
hermes -p wikiskill chat -Q -q "hermes-wikiskill có khỏe không? Chạy doctor rồi tóm tắt giúp tôi."
```

## 5. Thử nghiệm đầu tiên (tùy chọn, khoảng US$0.1–0.3)

Chạy bộ bài mẫu đi kèm để xem vòng học hoạt động trên máy của anh/chị:

```bash
bash ~/ops/hermes-wikiskill-src/benches/house-report/run-effectiveness.sh eff1
hws status eff1
hws report eff1 --html /tmp/hws-dashboard.html     # mở bằng trình duyệt, chạy offline
```

## 6. Dạy việc riêng của anh/chị

Viết một file JSON gồm các bài có đáp án. Mẫu ở mục 6 phần tiếng Anh ở trên.
- `split: "train"` là bài để học, `split: "val"` là bài để kiểm tra.
- `hws init` sẽ **niêm phong** đáp án của bài `val`, nên agent không nhìn trộm được.

Sau đó chạy:

```bash
hws init viec1 --tasks ~/viec-cua-toi.json
hws evolve viec1 --iters 0      # đo điểm ban đầu, chưa có skill
hws evolve viec1 --iters 3      # cho học (tốn tiền; thêm --dry-run để xem trước)
hws status viec1                # r_best, vòng nào ACCEPT, skill nào đang dùng
```

## 7. Đưa skill vào dùng hằng ngày (chủ máy quyết định)

```bash
hws promote viec1 --profile default --dry-run          # xem trước
hws promote viec1 --profile default --i-am-owner       # có sao lưu + biên nhận
hws demote  --profile default --last --i-am-owner      # hoàn tác y hệt như cũ
```

Nên thử trước trên một profile nháp tên `hws-test-...`. Profile nháp không cần `--i-am-owner`.

## 8. Nâng cấp và gỡ cài đặt

```bash
git -C ~/ops/hermes-wikiskill-src pull && bash ~/ops/hermes-wikiskill-src/install.sh --yes
bash ~/ops/hermes-wikiskill-src/uninstall.sh --yes            # gỡ, giữ lại các workspace
bash ~/ops/hermes-wikiskill-src/uninstall.sh --yes --purge    # gỡ sạch cả workspace
```

Nếu đã `promote` thì `demote` trước khi gỡ. Trình gỡ:
- tự sao lưu vào `~/ops/hermes-wikiskill-backups/`;
- không bao giờ xóa profile mà nó không tạo ra.

Chi tiết đầy đủ và phần hỏi đáp: [README.vi.md](README.vi.md).
