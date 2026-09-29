# hermes-wikiskill

`hermes-wikiskill` (viết tắt `hws`) chạy vòng tự tiến hóa **WikiSkill**
(arXiv:2608.27454) trên máy chủ Hermes Agent.

Vòng lặp có bốn bước:
1. Agent làm các task có chấm điểm.
2. *Maintainer* chắt lọc trace thành một wiki lâu dài.
3. *Proposer* viết một skill ứng viên.
4. Cổng kiểm định (gate) **chỉ giữ** skill khi `R_val > R_best`; nếu không, skill bị gỡ về trạng thái cũ.

Bộ này dựa trên `wikiskill` 0.1.5 gốc (commit `cdc291c`, giấy phép MIT; ghi công ở
`NOTICE` và `LICENSE`). Nó giữ nguyên thuật toán và các lệnh của bản gốc, và sửa những
lỗi khiến bản gốc không chạy được trên Hermes v0.21.3. Chỉ có backend Hermes; các
backend Claude/Codex/Copilot của bản gốc đã được bỏ.

> Bản tiếng Anh: [README.md](README.md). Hướng dẫn cài từng bước (kể cả nhờ chính agent Hermes cài): [INSTALL.md](INSTALL.md).
> Phần **Hỏi đáp (FAQ)** ở cuối trả lời những câu hỏi hay gặp nhất.

## Khác gì so với bản gốc, và vì sao

Mỗi dòng là một lỗi của bản 0.1.5, đã kiểm chứng từ mã nguồn. Mỗi bản sửa đều có test
ghim lại trong `tests/` (gói `wikiskill==0.1.5` chỉ được import trong test để chứng minh lỗi
có thật ở bản gốc).

| ID | Bản gốc | hermes-wikiskill |
|---|---|---|
| D1 | Chạy task với `-t terminal,file,code_execution` nên Hermes không bao giờ liệt kê skill đang active (`<available_skills>` chỉ có khi bật công cụ skills). **Gate không thể chấp nhận skill nào.** | Mọi lượt chạy đều bật toolset `skills`. Sau mỗi lượt, framework kiểm tra prompt trong session đã xuất và **báo lỗi rõ (exit 3)** nếu skill không hiện ra. |
| D2 | Dry-run ghi trace `score:null`, làm lần chạy thật sau đó hỏng ("trace already exists"). | Dry-run không ghi gì. Trace thật không bao giờ bị ghi đè âm thầm. |
| D3 | `evolve --dry-run` ghi state giả, mục wiki và commit git. | `--dry-run` không ghi gì. Có test băm toàn bộ workspace trước và sau. |
| D4 | Bản vá sửa tay nằm trong site-packages, `pip install -U` là mất. | Bản sửa nằm trong mã nguồn của một gói riêng (`hermes-wikiskill`); `doctor` kiểm tra lại sau khi nâng cấp. |
| D5 | Chép nguyên `.env`, `auth.json`, `config.yaml` (token bot, mọi API key) vào từng workspace và không bao giờ làm mới. | Chỉ chép key của nhà cung cấp model, làm mới mỗi lần chạy, `chmod 600`, không ghi log. Config không chứa key. |
| D6 | Bộ cờ cố định, Hermes cũ báo `unrecognized arguments`. | Dò cờ từ `hermes chat --help`, có đường lùi (legacy) đã test. |
| D7 | Hermes khởi động lỗi vẫn bị chấm như 0.0 bình thường. | Khởi động lỗi là lỗi, không phải điểm. |
| D8 | Không có tập kiểm tra giữ riêng (held-out); baseline val của demo gốc đã là 0.8889 (gần như không còn gì để học). | Tập `test` chỉ dùng cho `eval`, không bao giờ được đưa cho maintainer/proposer/gate; vai trò nào chạm vào nó thì vòng đó bị hủy. Bench `house-report` mới còn chỗ để học. |
| D9 | Không có đường đưa skill đã được chấp nhận sang profile thật. | `promote` (sao lưu, biên nhận, từ chối trùng tên, luật chủ máy) và `demote` (khôi phục y hệt từng byte). |
| D10–D13 | Đường dẫn sandbox/đề xuất có thể thoát ra ngoài thư mục; `code_stdout` bỏ qua mã thoát; `bench` giữ sandbox cũ. | Giới hạn đường dẫn, bắt buộc exit 0, dựng lại sandbox. |
| D14 | Không có khóa workspace. | `flock` trên `<ws>/.hws.lock` (bận thì exit 75), `--max-parallel 1`, biến `HWS_ROLLOUT_LOCK` để chạy từng lượt một. |
| D15 | Bỏ qua mã trả về của git. | Kiểm tra mọi lệnh git, xác minh hash cây trước khi lưu state. |
| D16 | `compare` ghép cặp chỉ theo ID và ghi đè kết quả cũ. | So hash định nghĩa task, id so sánh là duy nhất. |
| D17 | `disabled_toolsets` của profile có thể âm thầm tắt `skills` lần nữa. | Mục `profile-toolsets` của `doctor` cùng với kiểm tra hiển thị mỗi lượt. |
| D18 | Maintainer không thấy phản hồi chấm điểm. | Phản hồi chấm điểm (kỳ vọng so với thực tế) chỉ cho tập TRAIN. Không bao giờ lộ đáp án val/test. |

## Yêu cầu

- Hermes Agent (đã test v0.21.3), lệnh `hermes` có trong `PATH`.
- Python ≥ 3.10 có `venv` (Ubuntu: `sudo apt install python3-venv`).
- Linux (macOS dùng được để phát triển và chạy test). Máy tham chiếu chỉ có 2 vCPU và
  khoảng 2 GB RAM; framework chạy từng lượt một nên một VPS nhỏ là đủ.
- Profile Hermes dùng để mượn key (mặc định `default`) phải có key nhà cung cấp chạy được.
  Chỉ key của nhà cung cấp đó được chép, không gì khác.
- Không cần thư viện Python nào ngoài thư viện chuẩn.

## Cài đặt

Trên máy đang chạy Hermes:

```bash
git clone https://github.com/vietdang7/hwiki.git ~/ops/hermes-wikiskill-src
bash ~/ops/hermes-wikiskill-src/install.sh --dry-run   # tùy chọn: chỉ kiểm tra và in kế hoạch
bash ~/ops/hermes-wikiskill-src/install.sh --yes
~/.local/bin/hws doctor --profile wikiskill
```

Thư mục clone chỉ là quy ước để các lệnh trong README này đúng đường dẫn; để ở đâu cũng
được. Xem [INSTALL.md](INSTALL.md) để có câu lệnh mẫu nhờ agent Hermes tự cài giúp.

`install.sh` làm lần lượt:

1. **Kiểm tra trước.** Kiểm tra Python, tìm `hermes` và đọc phiên bản từ metadata
   của gói (không bao giờ gọi `hermes --version`, vì lệnh đó ghi vào `~/.hermes`).
   Dò cờ `chat` trong một thư mục tạm `/tmp/hws-*`, rồi kiểm tra đĩa và RAM.
2. **Cài gói.** Tạo venv `~/ops/hermes-wikiskill/venv` và cài gói vào đó
   (pip, không editable).
3. **Lệnh.** Tạo symlink `~/.local/bin/hermes-wikiskill` và `~/.local/bin/hws`.
   Không bao giờ thay thứ gì ở đó mà nó không sở hữu.
4. **Profile.** Chạy `hws setup-profile` để tạo hoặc làm mới profile Hermes riêng
   **`wikiskill`**. Profile này không có gateway, không có token nhắn tin, chỉ có
   key của nhà cung cấp, bật toolset skills, và có skill vận hành `hermes-wikiskill`.
5. **Kiểm tra.** Chạy `hws doctor --json --profile wikiskill`, in bản tóm tắt và ghi
   thêm vào `~/ops/hermes-wikiskill/install.log`.

Tùy chọn:

| Cờ | Ý nghĩa |
|---|---|
| `--yes` | Không hỏi xác nhận (bắt buộc khi không chạy trong terminal). |
| `--prefix DIR` | Thư mục cài. Mặc định `~/ops/hermes-wikiskill`. Nếu dùng prefix khác, hãy chạy các lệnh `hws` với `HWS_HOME=<prefix>` để chúng tìm thấy workspaces. Biên nhận và bản sao lưu của `setup-profile`, `promote` và `demote` được tìm theo prefix của chính launcher đã cài; `HWS_HOME` sẽ ghi đè giá trị đó. |
| `--profile NAME` | Profile cần tạo. Mặc định `wikiskill`. |
| `--source-profile NAME` | Profile lấy key. Mặc định `default`. |
| `--dry-run` | Chỉ kiểm tra và in kế hoạch, không ghi gì. |
| `--i-am-owner` | Chủ máy duyệt cho một profile **đã có sẵn** mà hermes-wikiskill không tạo ra (bắt buộc với `default`). Nó **chỉ** cài skill vận hành vào `skills/` của profile đó, sau khi sao lưu bản cũ nếu có, và ghi biên nhận. `config.yaml`, `.env`, `auth.json` và `SOUL.md` của profile không bao giờ bị đụng. Hoàn tác: `uninstall.sh`, hoặc `hws setup-profile --profile NAME --remove --i-am-owner`. Xem [Duyệt của chủ máy](#duyệt-của-chủ-máy-profile-default). |

Chạy lại lần hai với cùng mã nguồn sẽ in `no changes` và thoát 0.

## Nâng cấp

Kéo mã nguồn mới rồi chạy lại trình cài:

```bash
git -C ~/ops/hermes-wikiskill-src pull
bash ~/ops/hermes-wikiskill-src/install.sh --yes
~/.local/bin/hws doctor --profile wikiskill
```

Gói chỉ được cài lại khi mã nguồn thay đổi. Workspace, biên nhận và skill trong
profile được giữ nguyên. Nâng cấp gói `wikiskill` gốc không ảnh hưởng gì, vì đây là
một gói riêng với venv riêng.

## Gỡ cài đặt

```bash
bash ~/ops/hermes-wikiskill-src/uninstall.sh --yes            # giữ workspaces, receipts, diagnostics
bash ~/ops/hermes-wikiskill-src/uninstall.sh --yes --purge    # xóa cả hai
bash ~/ops/hermes-wikiskill-src/uninstall.sh --dry-run        # chỉ xem kế hoạch
```

Trình gỡ làm như sau:

- Nó chỉ làm việc với một prefix có file `install-state.json` của trình cài. Không có
  file đó thì nó từ chối đụng vào venv hay workspaces ở đó. Nó cũng từ chối `/`,
  `$HOME` (hoặc thư mục cha của nó), và mọi prefix trùng với `~/.hermes` hay
  `~/ops/wikiskill`. `install.sh` áp dụng cùng luật prefix này.
- Nó xử lý **mọi** profile mà bất kỳ lần cài nào đã thiết lập. `install-state.json` giữ tất
  cả trong `profiles[]`, gộp qua các lần chạy, nên `--profile` của lần cài gần nhất không quan
  trọng. Nó cũng xử lý mọi profile còn biên nhận skill vận hành chưa hoàn tác trong
  `<prefix>/receipts/`. `--profile NAME` chỉ để kiểm tra: tên mà lần cài chưa từng thiết lập
  sẽ bị từ chối.
- Nó sao lưu vào `~/ops/hermes-wikiskill-backups/<ts>/`: các biên nhận, `diagnostics/`,
  log cài đặt, và file nén của profile do mình tạo. Với `--purge` thì có thêm
  `workspaces.tgz`. Chỉ chủ sở hữu đọc được, vì `.env` của profile có chứa key.
  `diagnostics/` chứa các file chỉ dành cho người vận hành, ghi các đường dẫn ngoài
  workspace (đọc được) của những lượt maintainer/proposer bị hủy; workspace chỉ ghi một mã định danh.
- Các profile do mình tạo bị xóa. Sau đó trình gỡ chờ 90 giây rồi kiểm tra lại rằng chúng
  vẫn không còn. Một profile được coi là của mình khi `.hws-profile.json` của nó ghi đúng tên
  và đường dẫn của profile đó. Bản sao tạo bằng `hermes profile create --clone-all` mang
  marker của một profile khác, nên bị coi là không phải của mình và được giữ nguyên. Marker từ
  trước v0.2.0 (không có tên/đường dẫn) chỉ được tính khi `install-state.json` liệt kê profile đó.
- Profile không do mình tạo, kể cả `default`, **không bao giờ** bị xóa. Chỉ skill vận
  hành mà `--i-am-owner` đã cài vào đó được hoàn tác, qua
  `hws setup-profile --profile NAME --remove --i-am-owner`: khôi phục bản sao lưu,
  hoặc xóa thư mục skill. Mọi chuỗi biên nhận được kiểm tra trước (`--dry-run`) khi chưa có
  gì thay đổi. Trình gỡ không bao giờ bị kẹt:
  - Nếu anh/chị đã xóa thư mục skill, hoặc đã đặt lại bản cũ của mình, các biên nhận được
    đánh dấu là đã hoàn tác.
  - Nếu skill đã bị sửa, bản đã sửa được lưu trước vào thư mục chỉ chủ sở hữu đọc được
    `<prefix>/backups/edited-<profile>-<ts>/`. Đường dẫn này được in ra và ghi vào biên
    nhận. Sau đó trình gỡ dừng, không xóa gì khác. Chạy lại đúng lệnh đó để hoàn tất; bản đã
    lưu vẫn được giữ.
  - Exit 4 (`uninstall.sh`: exit 1) chỉ còn ở ba trường hợp cần anh/chị quyết định. Ngoài ra không
    có gì thay đổi, và thông báo ghi đúng bước để gỡ vướng (phần Xử lý sự cố có một dòng cho mỗi trường hợp):
    - skill đã bị sửa (`matches no receipt`): bản đó được lưu như trên; chạy lại đúng lệnh đó;
    - ở đường dẫn skill là một symlink hoặc một file thường (`is not a plain directory`): không đi
      theo liên kết, không thay đổi gì. Hãy xóa nó (`rm -rf <đường dẫn>` chỉ xóa liên kết, không bao
      giờ xóa thứ nó trỏ tới), hoặc dời nó đi chỗ khác bằng `mv`, rồi chạy lại đúng lệnh đó;
    - bản sao lưu của bản cũ bị mất (`backup … is missing`): `rm -rf <thư mục skill>`, rồi chạy lại
      đúng lệnh đó.
- Sau đó nó xóa venv, hai symlink và file log/state cài đặt.
- Nó không bao giờ gọi `hermes profile delete` và không đụng đến gateway.

## Sử dụng

Workspace nằm ở `~/ops/hermes-wikiskill/workspaces/<ws>`. Đổi thư mục gốc bằng
`--ws-root DIR` hoặc biến môi trường `HWS_HOME`.

```bash
hws version
hws doctor --profile wikiskill            # thêm --live để chạy thử một lượt thật (tốn một lần gọi LLM)

hws init eff1                             # workspace với task train/val có sẵn của house-report
hws init mine --tasks /path/to/tasks.json # task riêng (schema gốc, chỉ split train/val)
hws status eff1                           # baseline, R_best, next_iter, lịch sử, skill đang active
hws status eff1 --json

hws evolve eff1 --iters 0                 # chỉ đo baseline: validation không có skill
hws evolve eff1 --iters 3                 # vòng next_iter..3: train -> maintain -> propose -> gate
hws evolve eff1 --iters 3 --dry-run       # in kế hoạch, không ghi gì
hws run-task eff1 <task_id>               # chạy một lượt

# đánh giá trên tập held-out (file test không bao giờ vào workspace)
hws eval eff1 --tasks ~/ops/hermes-wikiskill-src/benches/house-report/test.json --skills empty  --repeat 2 --tag test-empty
hws eval eff1 --tasks ~/ops/hermes-wikiskill-src/benches/house-report/test.json --skills active --repeat 2 --tag test-active

hws report eff1 --html /tmp/hws-dashboard.html   # một file HTML chạy offline (sáng/tối)
hws report eff1 --json
```

Chỉ chạy đánh giá trên tập test giữ riêng (hoặc `measure.sh`) **sau khi** đã tiến hóa xong workspace đó.
Khi `<ws>/eval/` đã tồn tại, maintainer của lần tiến hóa sau có thể vô tình gặp nó, chỉ cần một lệnh
`search_files` rộng, và vòng đó bị hủy. Muốn tiến hóa tiếp, hãy tạo workspace mới bằng `hws init`.

Các lệnh khác của bản gốc vẫn còn: `bench`, `gate`, `maintain`, `propose`, `reset`,
`compare`, `transfer`. Mọi lệnh có ghi dữ liệu đều lấy khóa workspace, và nhận
`--dry-run` và `--wait` (chờ thay vì thoát 75).

### Thí nghiệm đi kèm

`benches/house-report/` là một bench tất định (8 train, 6 val, 10 test giữ riêng).
Bộ chấm điểm của nó mã hóa các quy ước nội bộ mà model chỉ học được qua kinh
nghiệm có chấm điểm. Ba script sau chạy toàn bộ phép đo:

```bash
B=~/ops/hermes-wikiskill-src/benches/house-report
bash $B/run-effectiveness.sh eff1        # init, baseline (dừng nếu >= 0.5), evolve <= 4 vòng, đóng băng, đo
bash $B/measure.sh eff1 --reuse          # tính lại eval/effectiveness.json từ kết quả có sẵn (không tốn LLM)
bash $B/measure.sh eff1 --tag-prefix qa- # đo lại độc lập với tag mới
bash $B/stock-compare.sh                 # cùng bench trên bản gốc PyPI wikiskill==0.1.5
```

### Bộ kiểm thử nghiệm thu so sánh gì

`tests/acceptance/` chạy cùng 18 mã kiểm tra trên bản gốc (stock), bản đang cài
(current) và framework này. Một số mục cần cách làm riêng cho từng đích:
- **AC01** cài hoặc tìm từng đích theo cách riêng.
- **AC04** và **AC07** làm trên một bản sao với `current`, vì bản đó chỉ được đọc.
- **AC08** là N/A với stock và cài lại framework.
- **AC12** gỡ stock bằng pip và là N/A với `current`.

Trên stock và `current`, **AC10** (doctor), **AC15** (promote/demote) và **AC17**
(tập held-out) chỉ kiểm tra tính năng có hay không: chúng FAIL hoặc N/A vì tính năng
đó không tồn tại ở đó. Chúng không phải cùng một phép đo như trên framework.

Hermes luôn liệt kê skill thiết yếu `hermes-agent` của nó, nên skill này có trong
`skills_listed` của cả hai bên eval (không skill và đã tiến hóa). Chỉ bên đã tiến hóa
liệt kê các skill của workspace.

### Trò chuyện với skill vận hành

Profile `wikiskill` mang skill vận hành `hermes-wikiskill`. Skill này dạy agent trả
lời dựa trên kết quả lệnh thật: `hws status`, `hws report`, cách đọc `state.json`,
và luật phải có chủ máy duyệt.

```bash
hermes -p wikiskill chat -Q -q "Workspace wikiskill eff1 đang ở trạng thái nào?"
```

### Promote / demote

Skill đã được chấp nhận **chỉ** ảnh hưởng đến các cuộc chat Hermes bình thường sau
khi được promote vào một profile. Luôn xem trước bằng `--dry-run`:

```bash
hws promote eff1 --profile hws-test-demo --dry-run
hws promote eff1 --profile hws-test-demo            # chép skill đã được gate ACCEPT, có sao lưu + biên nhận
hws demote --profile hws-test-demo --last           # khôi phục thư mục skills y hệt từng byte
hws demote --profile default --last --i-am-owner    # hoàn tác y như vậy cho một lần promote vào `default`
```

`promote` hoạt động như sau:
- Chỉ chép những skill đã được gate chấp nhận.
- Từ chối nếu tên đã có trong profile đích, trừ khi thêm `--replace`.
- Từ chối mọi profile mà hermes-wikiskill không tạo ra (`default`, otherprofile,
  …), trừ khi thêm `--i-am-owner`. Profile `hws-test-*` và profile do mình
  tạo không cần cờ này. `demote` theo cùng luật.
- Từ chối skill có chứa symlink (một liên kết có thể kéo file bí mật như
  `~/.hermes/.env` vào profile).
- Ghi biên nhận vào `~/ops/hermes-wikiskill/receipts/`.

## Xử lý sự cố

| Hiện tượng | Ý nghĩa / cách xử lý |
|---|---|
| exit **3** | Trong lượt chạy đó agent không thấy skill đang active (D1/D17). Lượt đó không được chấm và gate không ra quyết định. `doctor` kiểm tra profile vận hành chứ không phải lượt chạy, nên hãy xem chính lượt chạy: `<ws>/runs/.../run.json` (`skills_expected` so với `skills_visible`, `skills_index_present`) và `session.jsonl` đã lưu (khối `<available_skills>`). Nguyên nhân hay gặp: frontmatter của chính skill ẩn nó (điều kiện `platforms`, `requires_*`); tên thư mục khác `name` trong frontmatter; một bản nâng cấp Hermes đổi định dạng danh mục. |
| exit **4** | Hermes không tương thích (thiếu cờ bắt buộc hoặc không dùng được). Chạy `doctor`, xem `hermes-version` và `hermes-flags`. |
| exit **75** | Một tiến trình khác đang giữ `<ws>/.hws.lock` (thông báo có ghi pid). Hãy chờ hoặc dùng `--wait`. Đừng xóa file khóa. |
| exit 2 | Sai cú pháp. Xem `hws <verb> --help`. |
| install.sh in `no changes` | Bình thường khi chạy lại: cùng mã nguồn, profile đã cập nhật, doctor OK. |
| install.sh: `refusing to overwrite ~/.local/bin/hws` | Tên đó đang thuộc về thứ khác. Tự đổi tên hoặc xóa; trình cài không bao giờ ghi đè file lạ. |
| install.sh: `doctor reported failing checks` | Các mục lỗi được in ngay phía trên và ghi vào `~/ops/hermes-wikiskill/install.log`. |
| install.sh / uninstall.sh: `refusing prefix …` (exit 2) | Prefix là `/`, `$HOME` hoặc thư mục cha của nó, hoặc trùng với `~/.hermes` / `~/ops/wikiskill`. Chọn `--prefix` khác. |
| install.sh: `exists but … install-state.json does not` | Venv đó không do trình cài này tạo. Chọn `--prefix` khác. |
| uninstall.sh: `install-state.json is missing` | Không có gì ở đó do `install.sh` cài; trình gỡ từ chối xóa. |
| uninstall.sh / `setup-profile --remove`: exit **4** (uninstall.sh: exit 1), `matches no receipt` | Skill vận hành trong một profile không do mình tạo đã bị sửa sau khi cài. Bản đã sửa vừa được lưu vào thư mục chỉ chủ sở hữu đọc được mà thông báo in ra (`<prefix>/backups/edited-…`); ngoài ra không có gì thay đổi. Chạy lại đúng lệnh đó (`bash ~/ops/hermes-wikiskill-src/uninstall.sh --yes`, hoặc `hws setup-profile --profile NAME --remove --i-am-owner`): skill sẽ được khôi phục hoặc xóa theo biên nhận. |
| uninstall.sh / `setup-profile --remove`: exit **4** (uninstall.sh: exit 1), `is not a plain directory` | Ở chỗ thư mục skill vận hành của một profile không do mình tạo (với `default`: `~/.hermes/skills/hermes-wikiskill`) đang là một symlink hoặc một file thường. Không có gì bị đi theo hay thay đổi. Hãy xóa nó bằng lệnh `rm -rf <đường dẫn>` mà thông báo in ra (với symlink, lệnh này chỉ xóa liên kết, không xóa thứ nó trỏ tới), hoặc dời nó đi chỗ khác (`mv <đường dẫn> <chỗ khác>`) nếu muốn giữ. Sau đó chạy lại đúng lệnh đó: các biên nhận được đánh dấu là đã hoàn tác. Bản cũ đã được sao lưu không được đặt lại; nó vẫn nằm trong `<prefix>/backups/`. |
| uninstall.sh / `setup-profile --remove`: exit **4** (uninstall.sh: exit 1), `backup … is missing` | Không tìm thấy bản sao lưu của bản có trước khi cài hermes-wikiskill. Nếu không cần bản đó, hãy `rm -rf` thư mục skill mà thông báo ghi, rồi chạy lại đúng lệnh đó. |
| uninstall.sh: thư mục profile `reappeared` | Có thứ gì đó (gateway hoặc dashboard) vẫn đang ghi vào profile. Cần điều tra; trình gỡ không thử lại mù quáng. |

Với cả ba dòng exit 4, `uninstall.sh` in cùng một dòng đầu (`changed since it was installed, or its backup
is missing`), kèm thông báo của `hws` trong ngoặc; phần trong ngoặc cho biết đó là dòng nào.

Các mục kiểm tra của `doctor`:
`python`, `hermes-binary`, `hermes-version`, `hermes-flags`, `profile`,
`profile-toolsets`, `profile-credentials`, `operator-skill`, `framework-skills`,
`skills-visibility`, `workspace-locks`, `disk`, `ram`, `live-run` (chỉ khi có
`--live`) và `pass-env` (một cảnh báo, chỉ khi đặt `HWS_PASS_ENV`; công tắc đó dành
cho bản giả lập khi test). `ok: true` nghĩa là không mục nào hỏng; các mục `warn`
nên đọc qua.

## Kỳ vọng trung thực

- Chỉ có cải thiện với **các loại task lặp lại và có chấm điểm**. Một skill học được
  trên một bench không tự làm chat hằng ngày thông minh hơn.
- Một đề xuất bị từ chối, hay một vòng `no_action`, là gate đang làm đúng việc.
- **Maintainer và proposer được giám sát, không bị nhốt.** Chúng chạy bằng chính user
  Unix của anh/chị. Cách giới hạn là phát hiện: vòng đó bị coi là không hợp lệ (không
  chạy gate, skill được khôi phục) nếu một phiên làm một trong các việc sau:
  - gõ một đường dẫn nằm ngoài workspace;
  - chạm vào file eval hay file test giữ riêng;
  - dùng các hash val đã niêm phong để dò đáp án.

  Đây không phải sandbox của hệ điều hành. Đừng để file test giữ riêng hay bộ sinh bench
  nằm trong workspace.
- **Giới hạn đã biết của bộ phát hiện** (ghi rõ ở đây, chưa sửa trong bản này):
  - *Báo nhầm:* maintainer grep một trace train trong workspace có trích một đường dẫn ngoài
    workspace sẽ làm vòng đó bị hủy.
  - *Báo nhầm:* một git identity cục bộ ghi vào `wiki/.git/config` làm vòng đó bị hủy, vì nó bị
    tính là thay đổi metadata git.
  - *Bỏ sót:* truy cập thư mục cha hay thư mục gốc mà chỉ nằm trong chuỗi ký tự của code, dạng
    `~user`, hoặc dùng escape `\/` thì bộ phát hiện truy cập ngoài workspace không bắt được.

  Một lần hủy nhầm chỉ tốn một vòng. Còn việc bỏ sót là lý do con số trên tập test giữ riêng,
  chứ không phải R_val, mới là kết quả đáng tin.
- **Các đường dẫn bị gắn cờ của một vòng bị hủy được lưu ở đâu.** Bên trong workspace, một vòng bị hủy chỉ
  ghi các đường dẫn ngoài workspace bị gắn cờ dưới dạng digest. Lịch sử, ghi chú wiki và `confinement.json`
  chỉ chứa digest, nên một vai trò đọc các ghi chú đó về sau sẽ không bị hủy thêm lần nữa. Các đường dẫn
  đọc được chỉ dành cho người vận hành, trong `<prefix>/diagnostics/<ws-id>/`.
- **Điều gì xảy ra với một lượt maintainer hay proposer hỏng.** Nếu một lượt chạy vai trò bị hủy, gặp lỗi,
  hoặc bị kill rồi được tiếp tục bằng `hws reset` hay lần `evolve` kế tiếp, toàn bộ workspace được đưa về
  trạng thái trước lượt đó, từng file một. Trong thư mục riêng của lượt chạy, chỉ những bản ghi do chính
  harness viết được giữ lại (mỗi bản ghi được đối chiếu với một sha256 lưu ngoài workspace); mọi thứ vai trò
  viết vào đó đều bị xóa.
- **Retro-audit có thể báo `laundering-attempt`.** `python -m hermes_wikiskill.heldout <ws>` gán kết luận này cho
  một lượt vai trò về sau có lời gọi công cụ nêu tên một file mà bước dọn dẹp sau một lượt hỏng trước đó đã
  xóa, rồi thoát với mã 1. Nó chỉ báo cáo: file đó đã bị xóa từ trước, và không kết quả gate hay eval nào
  thay đổi.
- **Hoàn tác skill vận hành khôi phục những gì.**
  - Được khôi phục: bản `hermes-wikiskill` cũ của anh/chị, gồm file, symlink và quyền của
    file/thư mục (kể cả quyền ghi cho nhóm).
  - Bị xóa nếu rỗng sau khi hoàn tác: `~/.hermes/skills/` và `~/.hermes/profiles/`, nếu trước đó
    chưa có.
  - Không được khôi phục: thời gian sửa file và chủ sở hữu file.
  - Không được đưa lại: bản cũ nếu chính anh/chị đã xóa thư mục skill, hoặc thay nó bằng một
    liên kết hay một file. Bản sao lưu của nó vẫn
    nằm trong `<prefix>/backups/`, cùng mọi bản đã sửa được lưu trong quá trình gỡ.
- **Tin con số trên tập test giữ riêng hơn R_val.** Các hash val đã niêm phong vẫn có thể
  bị dùng để dò đáp án, và bộ phát hiện việc đó chỉ là heuristic. Một maintainer cố ý có thể
  phần nào chỉnh skill cho khớp tập val. Tập test giữ riêng không thể ảnh hưởng đến kết
  quả: nó không bao giờ được đưa cho maintainer, proposer hay gate, và vai trò nào chạm vào
  nó thì vòng đó bị hủy. Vì vậy con số của nó là con số không thiên lệch.
- Kết quả đo trên một máy tham chiếu (Hermes v0.21.3, `deepseek-v4-flash`) với bench
  house-report đi kèm (tập test giữ riêng, skill đã tiến hóa so với không có skill, mỗi
  bên ≥ 2 lần lặp). Bộ skill đã tiến hóa đạt
  **1.0** ở cả hai lần lặp, bộ không có skill đạt **0.0** ở cả hai, nên **Δ = +1.0**
  trên 10 task giữ riêng.
  - Một skill được chấp nhận (val 0.0 → 1.0), và cả 20 phiên test bên đã tiến hóa
    đều nạp nó bằng `skill_view`.
  - Không lượt nào bên đã tiến hóa thoát khỏi sandbox. Có một lượt bên không có
    skill thoát ra: lượt đó bị đánh dấu, tính 0 điểm và bị loại khỏi Δ trừ task bị
    đánh dấu (Δ này cũng là +1.0).
  - Hai lần chạy trước cho cùng Δ.

  Đây là kết quả hẹp, trên dữ liệu tổng hợp: nó cho thấy vòng lặp học được một quy ước
  mà model không tự đoán ra, chứ không phải chat hằng ngày thông minh hơn. Tự chạy lại bằng
  `benches/house-report/run-effectiveness.sh`.
- Chi phí: khoảng US$0.003 cho mỗi lượt chạy trên `deepseek-v4-flash` (ước tính).
  Một vòng gồm khoảng một lượt cho mỗi task train, cộng một lượt maintainer, một
  lượt proposer, và một lượt cho mỗi task val. Chi phí thật nằm trong
  `hws report <ws> --json` → `costs` (chỉ của workspace đó: các lượt chạy,
  maintainer/proposer và eval của nó). Chi phí của lần chạy tham chiếu:
  - tiến hóa (baseline + một vòng): US$0.096;
  - đánh giá test (40 lượt): US$0.128;
  - tổng: US$0.224.

  Trung bình mỗi lượt test tốn khoảng US$0.002 và 12 giây khi có skill đã tiến hóa,
  US$0.004 và 38 giây khi không có skill.
- Không có gì được dùng trong công việc hằng ngày cho đến khi đủ hai điều: gate
  **ACCEPT**, rồi chủ máy duyệt **promote**.

## Duyệt của chủ máy: profile default

Với các giá trị mặc định, trình cài không cài gì vào profile `default` đang chạy
thật. Sau khi chủ máy đã xem xét bằng chứng (nội dung skill, kết quả gate, mức chênh
lệch trên tập held-out), các lệnh sau sẽ bật nó:

```bash
# 1. chỉ skill vận hành -> ~/.hermes/skills/hermes-wikiskill (không bao giờ đụng config.yaml/.env/auth.json)
bash ~/ops/hermes-wikiskill-src/install.sh --yes --profile default --i-am-owner

# 2. xem trước, rồi promote các skill đã được chấp nhận của một workspace
hws promote eff1 --profile default --dry-run
hws promote eff1 --profile default --i-am-owner

# hoàn tác, theo thứ tự ngược lại
hws demote --profile default --last --i-am-owner                  # 2: khôi phục thư mục skills từ bản sao lưu
hws setup-profile --profile default --remove --i-am-owner         # 1: khôi phục hoặc xóa skill vận hành
```

Bước 1 sao lưu `~/.hermes/skills/hermes-wikiskill` nếu đã có, và ghi biên nhận vào
`<prefix>/receipts/` (mặc định `~/ops/hermes-wikiskill/receipts/`). `uninstall.sh` tự hoàn tác
bước 1, bất kể lần cài gần nhất dùng `--profile` nào: nó hoàn tác mọi biên nhận skill vận hành
chưa được hoàn tác:
- Thư mục skill đã bị xóa, hoặc bản cũ của anh/chị đã được đặt lại, được tính là đã hoàn tác.
- Skill đã bị sửa thì được lưu trước vào `<prefix>/backups/edited-default-<ts>/` (chỉ chủ sở hữu
  đọc được). Sau đó lệnh trả về exit 4 và `uninstall.sh` dừng trước khi xóa thứ khác. Chạy lại
  đúng lệnh đó là hoàn tất.

`hws setup-profile --remove` cũng trả về mã khác 0 khi skill vận hành của mình vẫn còn mà không
tìm thấy biên nhận.
Nếu cài bằng `--prefix` khác, hãy chạy lệnh đó với `HWS_HOME=<prefix>`. `uninstall.sh`
**không** hoàn tác bước 2: hãy chạy `demote` trước.

## Hỏi đáp (FAQ)

### 1. "Tôi muốn sử dụng wikiskill này thì phải làm sao?"

Có hai cách dùng hằng ngày.

- **Hỏi qua Hermes** trong profile riêng `wikiskill`. Profile này có skill vận hành
  `hermes-wikiskill`, nên agent biết cách đọc trạng thái thật:
  ```bash
  hermes -p wikiskill chat -Q -q "Workspace wikiskill eff1 đang ở trạng thái nào?"
  hermes -p wikiskill chat          # phiên tương tác
  ```
- **Gõ lệnh trực tiếp**:
  ```bash
  hws status eff1                          # baseline, R_best, lịch sử gate, skill đang active
  hws evolve eff1 --iters 3                # học thêm (tốn tiền LLM, chạy từng lượt một)
  hws report eff1 --html /tmp/hws-dashboard.html
  ```

Việc gì chạy ở đâu:
- Mọi thứ chạy trên chính máy đang chạy Hermes.
- Mỗi lượt chạy có `HERMES_HOME` riêng, nằm ngoài `~/.hermes`: task chạy trong `/tmp/hws-rollouts/r-*/home`,
  maintainer/proposer trong `<ws>/.hws-roles/r-*` (tạo mới mỗi lượt, xóa sau khi xuất session).
- Các lượt chạy nối tiếp nhau (khóa workspace và `HWS_ROLLOUT_LOCK`), để máy nhỏ (2 vCPU,
  2 GB RAM) cũng chạy được.

Chi phí mỗi lượt khoảng US$0.003 (ước tính). Thời gian và chi phí thực đo trên máy tham chiếu:
- Tiến hóa trên bench house-report (baseline val + một vòng: 8 task train,
  maintainer, proposer, 6 task val): US$0.096.
- Đánh giá trên tập test giữ riêng (2 bên × 2 lần lặp × 10 task = 40 lượt): US$0.128.
  Trung bình 12 giây/lượt khi có skill và 38 giây/lượt khi không có skill.
- Tổng cho workspace đó: US$0.224. Chi phí thật của workspace của anh/chị luôn xem được bằng
  `hws report <ws> --json` → `costs`.

Skill học được **chưa** dùng trong chat hằng ngày của anh/chị cho đến khi được
promote (xem câu 3).

### 2. "Việc cài wikiskills này có xung đột với những skill đang có hiện tại không?"

Không.

Trình cài **chỉ ghi** vào:
- `~/ops/hermes-wikiskill/**` (venv, log, workspaces, receipts);
- `~/.local/bin/hermes-wikiskill` và `~/.local/bin/hws`;
- profile riêng `~/.hermes/profiles/wikiskill/**`;
- thư mục tạm `/tmp/hws-*`.

Nó **không bao giờ đụng** đến:
- `~/.hermes/config.yaml`, `~/.hermes/.env`, `~/.hermes/auth.json`;
- `~/.hermes/skills/**` (các skill hiện có của anh/chị);
- các profile khác mà anh/chị đang có;
- gateway và dashboard;
- venv của Hermes;
- bản `wikiskill` gốc nếu đã cài (ví dụ ở `~/ops/wikiskill`, lệnh `wikiskill`): hai bản
  chạy song song được.

Cách ly:
- Skill mới học nằm trong `<ws>/skills/active`. Chúng chỉ được nạp vào
  `HERMES_HOME` riêng của workspace khi chạy thí nghiệm.
- Profile `wikiskill` không có token bot nào. Nếu gateway có phục vụ nó, cũng không
  có kênh nhắn tin nào để gửi đi.

Khi promote:
- Nếu profile đích đã có skill cùng tên, `promote` **từ chối**, trừ khi thêm `--replace`.
- Mỗi lần promote đều sao lưu thư mục skills và ghi biên nhận.
- `hws demote --profile <p> --last` khôi phục thư mục skills **y hệt từng byte**. Với profile không do hermes-wikiskill
  tạo (kể cả `default`; trừ `hws-test-*`), lệnh cần thêm `--i-am-owner`, giống `promote`:
  `hws demote --profile <p> --last --i-am-owner`; thiếu cờ này lệnh bị từ chối và không ghi gì. Nếu thư mục skills của profile đã
  thay đổi giữa promote và demote (ví dụ phiên chat ĐẦU TIÊN của profile khiến Hermes chép skill đi kèm vào, bộ đếm
  `.usage.json` của skill khác tăng, hoặc chủ máy thêm skill), demote vẫn gỡ đúng skill đã promote nhưng báo
  `identical: false` trong biên nhận thay vì che giấu.

### 3. "Vậy khi tôi giao một công việc thì wikiskill được kích hoạt khi nào?"

Không tự động. Một skill học được chỉ tham gia công việc thường ngày sau đủ hai bước:

1. Gate **ACCEPT** nó trong một workspace (`R_val > R_best` trên tập validation).
2. Chủ máy xem xét và chạy `hws promote <ws> --profile <p>`. Với profile không do
   hermes-wikiskill tạo (kể cả `default`), phải thêm `--i-am-owner`.

Sau đó Hermes làm như với mọi skill khác:
- Khi toolset `skills` được bật, prompt hệ thống liệt kê tên và mô tả skill trong
  `<available_skills>`.
- Khi task anh/chị giao khớp với mô tả đó, agent tự gọi `skill_view` để đọc toàn
  văn skill.

Vì vậy skill chỉ "kích hoạt" khi task khớp mô tả của nó. Riêng ở chế độ một lượt
(`-Q`), agent chỉ nạp skill khi skill mang kiến thức hoặc quy ước mà nó chưa có.

Trước khi promote, skill chỉ được dùng trong các lượt chạy của chính workspace đó
(`evolve`, `gate`, `eval`).

### 4. "Khi cài wikiskill bạn có cài đủ hết 8 skills không?"

Không, và đó là cố ý. Bản gốc có 8 file `SKILL.md`:

| Skill gốc | Loại | hermes-wikiskill |
|---|---|---|
| `wikiskill-maintainer`, `wikiskill-proposer` | 2 vai trò của vòng lặp | Đóng gói trong package. Chỉ được chép vào `HERMES_HOME` riêng của từng lượt maintainer/proposer (`<ws>/.hws-roles/r-*`, xóa sau lượt chạy). Không bao giờ vào profile thật. |
| `wikiskill-evolve` | Hướng dẫn vận hành | Được thay bằng skill `hermes-wikiskill`, cài vào profile `wikiskill` (và vào `default` chỉ khi chủ máy chạy lệnh duyệt ở trên). |
| `verify-output-readback`, `spec-literal-execution`, `search-miss-binary`, `script-exec-blocked`, `trace-harness-launch-failure` | 5 skill ví dụ | **Không cài.** Chúng được học trên task và model của tác giả gốc. Cài thẳng vào sẽ bỏ qua gate trên chính task của anh/chị và luật chủ máy phải duyệt. |

Bản gốc khi cài theo hướng dẫn của nó cũng chỉ đưa 2 skill vai trò vào workspace demo;
nó không nhập skill nào vào kho skills thật.

## Giấy phép

MIT. Dựa trên wikiskill gốc (commit `cdc291c`); ghi công ở `NOTICE`.
