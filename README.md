# POSS 场地抢位程序

在本地网页中选择多个账号、Activity、场馆和时段；到放位时间，各通道用独立会话并行尝试。
每个账号在每个目标日期、每种 Activity 抢到一笔即停止该 Activity；同一账号的其他 Activity 和其他账号继续抢。不同账号不会重复抢同日期、重叠时段的同一球场。

## Mac 本地部署

1. 安装 Python 3.12 或更新版本、Git，并在项目目录安装依赖：

   ```bash
   git clone git@github.com:Oxidaner/polyu-poss-booking.git
   cd polyu-poss-booking
   python3 -m venv .venv
   .venv/bin/python -m pip install -r requirements.txt
   .venv/bin/python poss_ui.py --no-browser
   ```

2. 在本机浏览器打开 `http://127.0.0.1:8765/`。服务只监听 `127.0.0.1`；终端按 `Ctrl+C` 停止 UI。首次使用在「账号与自动抢位」填写 NetID 和密码并保存，程序会验证登录、读取余额；密码写入本地 `.env`，不会由「一键上传 config.json」上传。
3. 选择 Facility、Activity、Center、Area，再**点击时间表中的时段**加入「并行抢位目标」。切换下拉框本身不会改变已有目标；要更换场地，先移除旧目标，再添加新场地的时段。确认列表中的日期、场馆和时间后点「保存目标」，写入本地 `config.json`。上传按钮也会先保存当前列表，再上传配置。
4. 需要 Mac 执行抢位时，点击「启动本地程序并等待放位」，或在另一个终端运行 `.venv/bin/python monitor.py --wait`。UI 服务和后台抢位进程相互独立；关闭网页不会停止抢位进程，Mac 需保持开机、联网。只用 Mac 管理 GitHub 配置时，运行 UI 即可，不要再启动本地抢位，以免与云端同时用相同账号抢同一目标。UI 中显示后台进程 PID；需要提前停止时，在终端执行 `kill -TERM <PID>`。

保存账号时，程序会用独立会话逐个登录 POSS，并读取各账号的 Deposit Balance。账号行显示可登录状态、余额和香港时间；余额读取失败会显示原因，不会当作 0。点击「验证账号并刷新余额」可手动重新读取；重启页面服务后，之前的验证结果需手动刷新。该检查只读取登录、预订和充值页面，不发送预订请求。

只选 Badminton 就只抢 Badminton，无须配置 Fitness。目标卡片的「抢位账号」可以选具体账号，指定后只有该账号会尝试该场地和时段；默认「自动分配」会优先把不同目标分给不同账号，未成功的账号仍可补抢其他自动分配目标。程序会避免两个账号抢同一日期、重叠时段的同一球场。各账号能否都抢到仍取决于候选场地数量和 POSS 的实际结果。

「任意 Area」会自动遍历该时段已预备的球场，不必逐一添加具体 Area。也可以只添加指定 Area；同一球场同时出现在两类目标中时，只会提交一次。

放位时刻由 `config.json` 的 `booking.lead_days`（默认提前 7 天）与 `booking.trigger_time`（默认香港时间 08:30）决定。例如抢 10 月 27 日的场地，提前 7 天就是 10 月 20 日 08:30 放位。程序不会把过期目标自动改到另一天；启动前请核对目标日期。`--dry-run` 只检查请求准备过程，不会下单。

`.env` 保存登录和通知凭据，不能提交到仓库；`config.json` 保存目标和放位设置。UI 的「真实下单测试」只针对手动选中的单笔目标，和自动并行抢位是两种独立操作。可以通过本地 `monitor.py` 或 GitHub Actions 运行自动抢位，避免两端同时抢同一组账号与目标。

## GitHub Actions + Cloudflare 自动部署

云端分两部分：Cloudflare Worker 每天香港时间 **08:15** 请求 GitHub `workflow_dispatch`；GitHub Actions 读取默认分支的 `config.json`，登录、预检并等到 **08:30** 放位。Cloudflare Worker 不运行 Python 抢位程序，GitHub 工作流也没有 `schedule` 定时触发。

1. 将代码和未来日期的 `config.json` 放入**私有** GitHub 仓库的 `main` 分支。本地 UI 的「一键上传 config.json」只上传目标配置；源码、README 和工作流的修改仍需用 Git 提交推送。不要上传 `.env`、本地 `state.json`、`bookings.json` 或 `dumps/`。云端工作流会自己生成并写回 `state.json` 和 `bookings.json`；后者含预约详情，所以仓库必须保持私有。
2. 在 GitHub 仓库 `Settings → Secrets and variables → Actions → Repository secrets` 添加 `POSS_NETID`、`POSS_PASSWORD`、`POSS_NETID_2`、`POSS_PASSWORD_2`（当前工作流启动前会检查这四项）。按需添加 `PUSHPLUS_TOKEN`、`SERVERCHAN_KEY`、`SMTP_HOST`、`SMTP_USER`、`SMTP_PASS`、`MAIL_TO`；邮件端口可设为仓库变量 `SMTP_PORT`。工作流已设置 `contents: write`，内置的 `GITHUB_TOKEN` 会写回状态。账号密码只放 Secrets，不能放在 `config.json`。
3. 在 GitHub 创建一个限定为**这个仓库**的 fine-grained personal access token，仓库权限给 `Actions: Read and write`。这是 Cloudflare 调用 `workflow_dispatch` 的凭据，和上一步的 POSS 账号 Secrets 是两套不同凭据。GitHub API 允许用工作流文件名 `monitor.yml` 触发，要求工作流支持 `workflow_dispatch` 和指定分支 `main`。
4. 在 Cloudflare Workers 中首次打开 Workers 页面，完成账号的 `workers.dev` 子域初始化。在**项目根目录**登录并部署现有的 `cloudflare/worker.mjs`：

   ```bash
   npx --yes wrangler@4.151.0 login
   npx --yes wrangler@4.151.0 deploy --config cloudflare/wrangler.jsonc
   npx --yes wrangler@4.151.0 secret put GITHUB_DISPATCH_TOKEN --config cloudflare/wrangler.jsonc
   ```

   把第 3 步的 token 输入最后一条命令的交互提示，不要写进源码或 `wrangler.jsonc`。Worker 只有 `scheduled()` 处理器，`workers_dev` 设为 `false`，不提供公开 HTTP 路由。Cron `15 0 * * *` 使用 **UTC**，对应香港时间每天 08:15。Cloudflare 新增或修改 Cron 后可能需要最多约 15 分钟传播；部署后在 Workers 的 Triggers 查看是否只有这一个 Cron，并确认 secret `GITHUB_DISPATCH_TOKEN` 已配置。不要重复建立另一个每日触发器。
5. 在 GitHub `Actions → POSS Booking Backup` 查看运行。需要验证链路时可手动 `Run workflow`，但该工作流使用 `--today-only`，如果今天不是配置目标的放位日，会正常结束且不下单。正常放位日检查 Cloudflare Logs 是否记录 `GitHub workflow dispatch accepted`，随后检查 GitHub run 是否从 `workflow_dispatch` 启动；GitHub 的 `Wait for release and book` 步骤会显示预热与抢位结果，`poss-dumps` artifact 保存 14 天，可下载查看 `booking_attempts.jsonl`。若启动延迟到宽限期后，会记录 `missed_release_window`；不要把“已触发”当作“已抢到”。

以后改日期或场地：在 UI 添加/移除目标，确认列表后点击「一键上传 config.json」；若提示与远端相同，请先核对列表是否真的变化、是否只改了上方下拉框。Cloudflare Worker 不需要为每次配置变化重新部署，下一次触发读取 GitHub `main` 的最新配置。若修改 `cloudflare/worker.mjs` 或 `cloudflare/wrangler.jsonc`，需要重新运行上述 `wrangler deploy` 命令；单纯把源码推到 GitHub 不会更新已部署的 Worker。

## 放位时的流程

1. 提前登录并保持会话；距离放位约 3 分钟和约 1 分钟时刷新连接。预热 TLS/网络错误按 30 秒重试，不退出等待进程。
2. 放位前从 POSS 时间表收集目标时段的球场编号，核对编号属于所选场馆；即使当时显示不可订也可预备。预检结果在 UI 中逐目标显示，编号按日期、场馆、时段写入 `state.json`。若随后时间表请求失败且缓存仍有效，可继续使用缓存。
3. 预检发现某目标的球场后，按球场数和可用账号数分配请求通道，每块球场对应一条通道；例如 7 块球场、2 个可用账号分为 4 条和 3 条。同一目标指定了账号时只在该账号内分配，指定了具体 Area 时只有一条通道。尚无缓存时先启动各账号的预检通道，再按实时预检结果补足通道；已预备编号可直接发送第一步请求，避免到点再等时间表。第一步读取超时为 45 秒；慢响应不会提前判失败。
4. 同一球场的最终提交不会重复发送；不同球场的首轮请求与最终提交可由同账号并行发送，POSS 判定账号的预约上限。缓存编号失效或与别的通道冲突时尝试换球场。每个账号的同一日期、同一 Activity 确认成功后，已分配给其他球场的首轮仍可完成，后续重试停止。其他 Activity 和账号继续。每块场地的状态分别保存，重启后跳过已成功或待确认的 Activity。
5. 最终提交超时或部分结果待确认时，只读查询 `My Record` 核对具体 Area；最终提交超时仍标记待核对。收到 `unknown_result` 时立即按该球场失败继续其他目标；日志明确标记本次核查已跳过。确认成功的其他 Activity 可继续。

时间表的临时网络故障可在放位窗口内重查，连续失败时逐渐放慢并加入随机延迟；遇到 `Retry-After` 时遵从服务器的等待要求。第一步请求若已经发出但响应丢失，程序把该通道标为未知，不盲目重发。
最终提交收到无法识别的结果页时，立即把这块球场记为失败并继续同账号其他目标，但不重复提交同一球场。随后可在 POSS `My Record` 核对是否实际成功；云端任务结束时会独立刷新预约记录。最终提交的网络超时仍保持待确认状态。

## 看结果与排障

- `dumps/booking_attempts.jsonl`：逐球场实时记录请求是否开始、阶段、HTTP 状态和耗时、缓存来源及可识别错误。第一步发出前会单独写入 `sending / first_step` 事件；成功或结果不明后立即写入结果，不等其他通道结束。
- `dumps/booking-local.log`：从网页启动的本地进程日志；网页也显示是否仍在运行。
- `state.json`：保存逐账号、逐目标预检状态、已预备球场编号、确认成功和最终提交待确认状态。若进程在最终提交后中断，UI 会标记该账号的该 Activity 待核对；重启不会自动重复提交。不保存密码或会话 Cookie。

UI 每约 3 秒刷新一次逐目标的预检状态、已确认/待确认球场数、失败阶段及请求耗时。统计来自本次运行的结构化日志；若程序尚未启动或放位日已过，会分别显示待启动或放位已过。

`no_vacancy` 表示查询时无可订球场，不足以单独证明被抢先；`read_timeout` 表示响应等待超时；`slot_taken` 表示 POSS 明确拒绝某块球场；`unknown` 表示请求可能已被处理，需到 POSS 的 `My Record` 核查。日志不会写入密码、Cookie、CSRF 或完整结果页。
HTTP 拒绝或结果不明时，可在对应事件中查看 `response_hint`（固定词匹配，涵盖预约上限与余额不足；`unclassified` 表示无法识别）、`confirmation_form`、`content_type`、`body_bytes`、`body_sha256` 和 `redirect_target`；跳转后的结果页另记 `result_page`。相同页面指纹有助于比较多账号是否遇到同一种响应，但不能单凭指纹断定拒绝原因。日志不记录响应原文、完整 URL 或跳转参数。

## Apple 日历同步

在 Mac「日历」中建立一个名为「POSS 预约」的 **iCloud** 日历，并在 iPhone 上启用同一 Apple 账户的 iCloud 日历。GitHub Action 每天抢位步骤结束后分别登录各账号，分页读取 POSS `My Record`，将当前有效的未来预约写入仓库的 `bookings.json`（包含账号、场馆、球场、日期、时段和核实时间）。取消与转让的旧预约不计入有效列表；账号读取失败时保留上次记录并标记错误，不用不完整结果覆盖。Action 失败的原因可在运行日志查看，列表中不会写入密码或 Cookie。

`calendar_sync.py` 每天 09:00（当前 Mac 时区为香港同区时间）用本机 `.env` 中**账号 1**的凭据直接登录 POSS、核实 `My Record`，再同步到专用日历；不读取 GitHub 的 `bookings.json`，账号 2 不进入日历。网络或会话异常会重试，未能完整核实时不更改日历。脚本会复用已有预约事件，删除已取消、已转让以及先前误同步的账号 2 未来事件；过去的事件保留。先运行 `.venv/bin/python calendar_sync.py --dry-run` 预览，再运行 `.venv/bin/python calendar_sync.py` 写入。macOS 首次操作日历可能要求允许自动化访问。后台任务配置见 `com.oxidaner.poss-calendar.plist`；其中的绝对路径属于原 Mac，换目录或另一台 Mac 使用前需改为实际路径。日志位于 `dumps/calendar-sync.log`。Mac 需保持开机、联网并登录用户账户。GitHub Action 继续单独维护各账号的 `bookings.json`，但不会影响本机日历。

回归测试：`.venv/bin/python -m unittest discover -s tests -q`。
