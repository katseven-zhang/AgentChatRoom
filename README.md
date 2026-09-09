# AgentChatRoom

AgentChatRoom 是一个面向异构 AI 编程 Agent 的项目级实时协作中心。它让 Codex、WorkBuddy、Grok Build、Trae 以及其他支持标准 MCP 的客户端，在同一 Project/Room 中交换消息、领取任务、声明文件占用、提交工作证据，并由独立 Agent 完成验证和最终集成。

当前产品提供 Python 后端、浏览器管理端、REST、SSE、本机 MCP stdio、**HTTP 直连（Streamable HTTP `/mcp`）**、CLI 和 SQLite 本地档案。本机也优先使用 HTTP 直连，共用一个服务，避免每条 MCP 连接拉起独立适配器进程；stdio 保留为兼容与开发调试入口，远程 stdio Bridge 用于客户端只能拉起本地进程时转发。PostgreSQL 和更完整的云端多租户适配仍按后续阶段演进。

## v0.2.4 修复说明

- Windows 打包 MCP 初始化失败时不再弹异常对话框并挂住；修复继承标准流与后台子进程的控制台窗口处理。
- 本机 MCP 必须使用用户显式启动的服务；未启动时失败退出，不自动拉起 GUI/服务。服务重启后旧绑定失效。
- 一个 HTTP MCP 配置可携带多组“Project 凭据名称 + 项目级 Token”；每个客户端任务建立独立轻量 Session，首次绑定后不可改绑另一个 Project，跨项目显式参数写入仍拒绝。
- 托盘恢复窗口采用单个后台操作与可配置超时，防止重复点击堆积操作。
- POSIX 平台端口检查兼容服务退出后的 TCP 等待状态，避免误判为端口占用；仍拒绝正在监听的端口。
- 接入指令区分首次配置、已配置软件加入新项目与恢复连接；取消任务不能重新获取文件租约，修复少量动态事件的聚合显示。

升级时请替换完整 Windows ZIP 解压目录（包含 `_internal`），并检查各客户端 MCP 配置是否仍引用旧 EXE 路径。关闭旧 MCP 连接和服务后再替换；新进程才会加载修复。仅删除 EXE 会导致连接失败，不代表成功接入。每个并行项目需要独立连接上下文。

## 接入场景选择

Windows 单 EXE 的 `mcp` 入口与 GUI 错误呈现隔离：启动或运行失败仅向继承的 stderr 管道返回脱敏错误并退出，不弹 GUI 异常对话框。windowed 构建显式恢复继承的标准流，不创建控制台；缺少 MCP 输入/输出管道时失败退出。冻结程序在分发 GUI/CLI/MCP 前处理 multiprocessing 子进程入口。验证必须包含实际打包产物，不能仅以 Python 源码测试代替；测试环境可用 `AGENTCHATROOM_TEST_EXE` 指定本次覆盖构建的 EXE，握手/断连测试使用隔离目录，不访问正在使用的 Room 数据。

Web「接入 Agent」入口内提供三种接入指令，由用户按客户端实际配置选择，不根据历史 Agent/在线记录推断本机配置：

- **首次配置软件**：页面不预先展示占位提示词；签发当前 Project Token 后，一次生成可读 Project↔Token 映射、含真实凭据和稳定软件身份的 MCP 配置，以及首次 bootstrap 指令。新数据库无需预先存在成员；保持“不关联”时，第一次成功连接会按配置身份自动登记成员。先核查已有连接器，确认没有时才配置一次。
- **已配置软件，加入本项目**：无需粘贴任何现有配置。用户只确认客户端、Token 名称、有效期、可选的已有成员与权限后直接签发当前 Project Token；签发结果是一段增量接入提示词，交给已配置的 Agent 后由它自行检查客户端本地现有 `agentchatroom` HTTP 配置，保留原 URL、已有软件身份 Header 和全部旧 Project 凭据，仅把新凭据追加或替换进 Authorization 凭据包并写回同一条目，重载 MCP 后按目标 Project 名称 bootstrap 核对。已关联 Token 本身可提供软件身份，现有配置缺少显式 Header 时不构成失败；Header 若存在则必须匹配。后端只保存 Token 哈希，页面无法重建旧 Secret，因此旧配置读取与合并在客户端侧由 Agent 完成，不由用户解析。Agent 无法读取本地配置时按提示词报告并停止；手动粘贴合并只保留在签发弹窗默认折叠的「高级 · 故障恢复」区域，非必填。保持“不关联”时新 Project 首次连接自动登记成员；选择旧成员仅用于沿用已有身份，与客户端实际身份不一致会被拒绝。各项目随后使用独立轻量 Session 并行工作。
- **恢复当前项目连接**：不签发新 Token、不新增或改写 MCP；单独生成恢复提示词，使用客户端已有 HTTP 配置重新 bootstrap 并核对当前 Project，不重发结果未知的写操作。

接入首页只负责选择场景和客户端，不显示可误复制的占位提示词。首次配置在签发成功后显示一次性完整提示词；加入项目在签发成功后显示一次性增量提示词；恢复连接直接显示另一份不含新 Token 的恢复提示词。服务返回的 `profiles.*.onboarding_modes` 按 `first_setup`、`add_project`、`reconnect` 分组；原 `onboarding_prompts` 字段兼容保留。缺少对应场景指令时前端明确提示更新服务，不回退安装指令。提示词是引导，不代替后端项目隔离、身份验证和失败封闭校验。

## 2026-09-03 新会话入口与任务筛选

- 已配置且已登记的本机 stdio Agent 新开对话时调用一次零参数 `room_bootstrap`。HTTP 多项目配置调用 `room_bootstrap(project_name="<完整接入提示词给出的 Project 名称>")`；一次性提示词包含配置所需的明文 Project↔Token 映射，Token 写入 MCP 后不得进入 Room、日志或仓库。
- Session Token 只保存在本机 MCP 进程内存中，不出现在工具结果、日志、URL、Room 消息或 checkout 登记里；后续 MCP 工具从当前绑定注入 `project_id` / `session_id` / `token`。
- MCP 启动后的自动 Presence 仍然只表示进程在线，不等于当前模型对话已同步。
- Web 任务展示改为方案 D 投影：导航 7 入口（需要处理 / 待认领 / 进行中 / 待验收 / 待集成 / 已完成 / 已取消）+ 全部任务重置；`state_view.phase` 由共享领域合同派生，已提交 Work Report 显示「待验收」，已验证待集成显示「待集成」，被退回显示「已退回」，集成失败有独立入口。

## 2026-09-03 审查修复

本次收口 Token 校验写锁、任务序号并发、SSE 鉴权、Git 证据路径信任、本机 MCP 备份权限，以及 Web 项目边界 / SSE / 可访问性：

- Agent Token 校验改为只读；`last_used_at` 后台批量更新，不再让每次 MCP 调用抢 SQLite writer。
- `task_number` 通过项目级计数器在同一写事务内分配，并发创建不会撞号。
- `events/stream` 拒绝匿名订阅，并按项目 / IP 限制连接数。
- Work Report 的 Git 证据只接受已登记 Workspace 内的路径，且 `commit_hash` 必须可解析。
- 本机 MCP 备份改为 `0o600`，文件名冲突时重试；Bridge 转发的 `request_id` 带实例前缀。
- Web 以服务端项目列表为事实源，SSE 解析失败不再卡住，项目切换不会被旧 snapshot 覆盖。

## 2026-09-02 修复更新

本次修复版重点收口用户任务入口、Agent 受理和 Project 级稳定任务序号：

- 用户任务入口拆分为两阶段：先提交原始任务说明 + 目标 Agent，受理后由 Agent 补全正式任务合同并自动派发。
- 用户不再填写正式标题、验收条件、优先级、依赖或内部状态；用户唯一保留的控制是指定 / 改派 Agent。
- 依赖继续由 Agent 判断并写入共享领域服务，用户侧只读展示。
- 待受理、待定义阶段不能被普通 Agent 认领、提交 Work Report、独立 Review 或最终集成。
- Project / Room 级新增稳定、唯一、后端生成的人类可读 `task_number`（从 1 开始，在写事务内通过 `task_number_sequences` 计数器分配，不能由前端传入；取消、改派、交接、验证和集成后保持不变，已分配号码不会被复用）。
- 历史任务按 `created_at, id` 回填 `task_number`；内部 `task_<随机码>` ID 仍然保留。
- REST、MCP、CLI、Web 共享同一领域服务和状态机；公开 Schema 版本升到 `7`。
- Web 任务 Tab 文案改为「协作视图」并新增 Intake 列表、时间线、依赖只读、只读详情视图与时间线样式。
- 用户侧移除了保存任务、依赖编辑、依赖勾选、交接和集成提交入口。


## 核心原则

- 标准化、配置化，不在业务代码中硬编码 Agent 厂商、模型、角色、项目路径、端口或部署环境。
- REST、MCP、CLI 和 Web 复用同一个领域服务和版本化数据模型。
- 执行完成、独立验证和最终集成是三个独立状态面。
- 事件历史追加写入；派生状态可以变化，历史事件不能改写。
- 浏览器是人类管理和观察界面，后端数据库才是共享事实源。

## 一期产品范围

一期以**单机使用闭环**为核心：AgentChatRoom 后端、浏览器管理端、代码
checkout 和参与协作的 Agent 客户端运行在同一台电脑上。当前优先完善
Project/Room 生命周期、Agent 接入、消息、Task、文件 Lease、工作证据、独立
Review、Integration 和 Knowledge Asset，使单机日常开发可以稳定、可审计地
完成完整协作流程。

以下能力暂缓，不作为一期完成或发布验收条件：

- 通过网络接入的外部 Agent；
- 多台电脑共同参与同一 Project/Room；
- 中心服务器、正式公网入口和生产 PostgreSQL 部署；
- 为跨机器协作自动同步代码、分支、工作树、未提交改动或构建产物。

跨机器协作不只是让 Agent 能连接同一个 Room。它还需要定义并验证代码同步
边界，包括仓库身份、commit/branch 基线、工作树状态、未提交改动、冲突处理、
任务证据对应的代码版本，以及不同机器间的权限和凭据管理。在这些问题形成
独立方案并完成端到端验证前，现有 Streamable HTTP、远程 Bridge、PostgreSQL
适配器和部署文件仅作为后续演进基础，不能据此声称已经支持完整跨机器协作。

## 界面与阅读体验

浏览器与 EXE 面板共用同一套前端。消息正文采用 16px 字号和宽松行距，任务说明、项目文档与 Markdown 标题具有清晰的阅读层级。总览根据中间工作区的实际宽度自动调整为单列或双列，拖动两侧分隔条后也会重新适配；手机窄屏按纵向排列，成员列表可单独滚动。文字层级沿用设计令牌字号（正文不小于 13px），桌面与窄屏主页面无意外横向溢出；总览「最近活动」中的聚合条目以左侧描边样式与单条事件区分。

右上角「默认主题 / 浅色 / 深色」可调整当前浏览器或 EXE 面板的外观。选择会保存在当前浏览器环境；默认主题使用服务端 `default_theme` 设置（含跟随系统）。本地选择优先于服务端默认，不修改其他用户的界面。无法保存浏览器偏好的环境中，仍可在当前窗口切换。

导航直接显示「总览、任务、文件占用、验证、管理」。文件占用与验证仍为只读信息；执行完成、独立验证和最终集成继续分别展示。

## 系统要求

- Python 3.11 或更高版本。
- Windows 一键入口需要 PowerShell 或 CMD。
- 前端无需单独安装 Node.js；Node.js 只用于开发阶段的 JavaScript 语法检查。
- PostgreSQL、Docker 和 Caddy 仅在对应服务器部署方式中需要。

## 平台支持边界

AgentChatRoom 的核心 Python 后端、Web 前端、CLI 和 MCP 服务支持
Windows、Linux 和 macOS。当前仓库提供完整的 Windows 一键启动与关闭入口；
Linux 或 macOS 使用下方的手动命令启动和停止，也可以使用 Docker/Podman
进行服务器部署。

GitHub Actions 当前持续验证 Windows 与 Ubuntu；macOS 的命令路径已按
跨平台 Python/CLI 设计，但尚未加入 CI 矩阵。

## Windows 一键启动

1. 下载或克隆仓库。
2. 双击 `启动 AgentChatRoom.cmd`。
3. 首次启动会在当前仓库创建 `.venv`、安装依赖、启动后端并打开浏览器。
4. CMD 窗口以前台方式持续运行并显示启动、停止和错误日志；关闭这个启动窗口会连同前台服务一起停止，不会转入后台常驻。Windows HTTP 客户端正常断开产生的 Proactor `WinError 10054` 回调不作为服务错误打印，其余事件循环异常仍照常显示。

需要清理异常退出后残留的后台进程时，双击 `关闭 AgentChatRoom.cmd`。清理脚本不会创建虚拟环境或安装依赖；它会先请求后台服务正常停止，再结束 `server.pid` 对应的整个进程树，最后按实际配置端口清理仍在监听的残留进程树。

默认地址：

```text
http://127.0.0.1:8765
```

启动脚本通过自身位置推导仓库根目录，所以仓库可以放在任意盘符、任意父目录，也可以改名。

## Windows GUI 面板与分发形态

AgentChatRoom 提供单 exe GUI 面板（基于 Edge WebView2 的 pywebview shell）与 PyInstaller onedir 独立分发形态，无需本机安装 Python 环境或依赖系统独立浏览器。发布目录 `dist/agentchatroom/` 只包含一个 `agentchatroom.exe`，启动、停止与前端管理全部包含在这一个 exe 内，按首个参数分发三种交付模式：

- **双击 / 无参数 / `gui`**：无黑框窗口面板（默认形态）。面板顶部有一条常驻控制条：服务状态（●运行中 含地址与 pid / ○已停止）、端口输入框、「启动服务」「停止服务」按钮。面板打开时显示本地占位引导页（不会出现 404 或连接错误），点「启动服务」后同一窗口加载管理前端，「停止服务」后回到占位页。启动复用分离后台服务生命周期（pid 文件、健康检查、进程树清理）；端口变更按配置优先级写回本地配置（`AGENTCHATROOM_PORT` 环境变量存在时提示其优先）；关闭面板时若服务仍在运行，经三选语义决定是否结束后台服务。桌面壳启用文本选择（`text_select`）：面板内消息、任务等文字可拖选，Ctrl+C 与右键「复制」均可用，与浏览器访问行为一致。
- **`mcp`**：MCP Server stdio 入口，供 Cursor、Windsurf、VS Code、Claude Desktop 等 Agent 宿主通过标准 stdio 协议接入。开发期推荐 `python -m agentchatroom.mcp_server`；打包形态为 `dist/agentchatroom/agentchatroom.exe mcp`（稳定 onedir 目录名，不要改成 `dist/release-x.y.z/` 这类按版本号命名的路径）。`mcp-config` 在冻结 exe 下会生成**当前这个 exe** 的绝对路径；升级后若客户端仍指向旧目录，必须更新配置。该入口暴露完整 MCP 工具面（含 `room_bootstrap` / `room_join` / `room_sync` 等），不是精简消息子集。无协议 stdin（空管道或立即 EOF）时向 stderr 输出 `startup_failed/no_protocol_stdin` 并以非 0 退出。
- **其余 CLI 子命令**（`serve`、`stop`、`logs` 等）：供命令行、批处理复用；分离后台服务的子进程就是同一个 exe 以 `serve` 子命令拉起的，不存在第二套启动逻辑。
- **自动启动（可选）**：默认手动控制；如需恢复"打开面板即自动拉起服务"，在 `~/.agentchatroom/client.toml` 的 `[target]` 段设置 `auto_start = true`。

### 单实例与关闭语义

GUI 初次打开时在主屏居中，默认尺寸为 1440×900；较小屏幕会自动收缩到屏幕逻辑尺寸的 90% 以内。停止服务的 Windows 进程树清理以无窗口方式执行，不弹出额外的命令行窗口。

运行页面顶部「停止服务」右侧提供「收起到托盘」按钮，点击后保留服务并隐藏面板，点击右下角托盘图标可恢复。托盘「打开面板」将恢复操作放入单个后台线程，回调立即返回；超时进入 `restore_failed` 并尝试显示托盘通知。超时由 `client.toml [target] restore_timeout_seconds` 控制，默认 3 秒，范围大于 0 且不超过 60 秒。阻塞操作完成前重复点击不会产生新恢复线程，过期恢复不会继续主动显示窗口；托盘退出入口保持可调用。窗口状态区分启动中、恢复中、可见、最小化、隐藏、恢复失败与退出。

- **单实例运行**：通过系统互斥锁保证单一 GUI 实例运行；二次双击会自动激活并聚焦到已打开的面板窗口。
- **最小化进托盘**：面板最小化时隐藏到系统托盘继续运行（托盘图标常驻，左键单击恢复窗口，右键菜单提供「打开面板 / 退出」），不再出现最小化后找不到窗口的情况；托盘不可用的环境自动降级为任务栏最小化行为。
- **关闭三选语义**：关闭面板时若后台服务仍在运行，提供系统原生三项选择：
  - **结束服务并关闭**：停止后台分离服务并完全退出。
  - **保留服务并收起到托盘**：隐藏面板并保留右下角托盘图标，后台服务与 Agent 协作继续运行；点击托盘图标恢复面板。托盘不可用时保留窗口，避免后台运行后找不到入口。
  - **取消**：放弃关闭，继续停留在面板中。

托盘菜单的「退出」用于真正退出面板与托盘：服务运行时仍可选择停止服务或保留后台服务，也可取消退出；选择保留服务时才仅留下后台服务进程。服务已停止时，关闭面板或从托盘退出均直接结束程序。

### GitHub Windows EXE 构建

`.github/workflows/package-windows.yml` 会在 `main` 分支的相关代码更新后自动运行，也可以在 GitHub Actions 中手工选择
`Package Windows EXE` 并点击 **Run workflow**。构建完成后，在 workflow 的 Artifacts 中下载
`agentchatroom-windows-x64.zip`；解压后直接运行目录内唯一的 `agentchatroom.exe`，无需安装 Python。发布 Release 后，
同一个 workflow 还会自动构建并把压缩包挂到对应的 GitHub Release，免去手动上传。该发布包是 PyInstaller
onedir 形态，目录内的 DLL 和运行库必须与 exe 一起保留。

如果 Release 事件没有自动触发，可在 **Run workflow** 的 `release_tag` 输入框填入已有标签（例如 `v0.2.3`）；
构建完成后，压缩包会由 GitHub runner 上传到该 Release。

人工冒烟测试建议依次确认：双击能打开面板；点击「启动服务」后能进入管理端；修改端口后重启仍使用新配置；点击「停止服务」回到占位页；
关闭面板时三种选择语义正确；再次打开不会产生第二个 GUI 实例。若要验证 Agent 接入，可运行
`agentchatroom.exe mcp` 并发送一次 MCP `initialize` 请求。

### 双模式接缝与客户端配置

GUI Shell 采用纯 Adapter 架构，零业务逻辑，不打包前端 SPA 资产（资产始终由所连接后端按版本下发）：

- **`ServerTarget` 抽象**：Shell 任何位置均不硬编码主机或端口字面量，窗口地址统一由 `ServerTarget { mode: "local" | "remote", base_url: "..." }` 解析派生；本地面板会跟随服务子进程在日志中登记的实际监听地址，不指向过期配置端口。
- **用户级客户端配置**：保存在用户目录 `~/.agentchatroom/client.toml`，与仓库 checkout 级服务端配置严格隔离。P1 阶段仅落地本地模式项。
- **版本握手 Probe**：Shell 开窗前统一探测 `/api/v1/version`（返回 `version`、`schema_version` 与 `product_name`），本地与远程共享完全一致的探测路径。本地目标连接失败（含端口被无关进程占用）时不开面板直接退出；401/403 视为可达（面板内 SPA 登录流接管鉴权）。
- **remote 模式 P1 边界**：`--mode remote --url ...` 仅做地址解析与版本握手探测并输出结果（成功返回 0，失败返回 1），**不打开管理窗口**；远程连接管理流程属 S1-S3，届时另立任务。
- **壳零鉴权逻辑**：Shell 不内嵌 Token，不绕过鉴权；认证流程完全由面板内 Web SPA 登录流承载。
- **原生目录选择**：在 exe 形态下透明接驳 pywebview 原生目录选择器，既有 SPA 代码零改动。

### 演进分期与宣称边界口径

AgentChatRoom 严格遵守按阶段落地的架构边界，不提前宣称未完成能力：

- **P1（当前形态）**：单 exe GUI 面板（pywebview shell + onedir 打包）并预留全部 6 条服务器模式接缝；本地双击即用。
- **P2（后续分期）**：tkinter 控制台能力完全并入 shell、单文件便携安装器。
- **S1 - S3（服务器模式）**：远程服务器连接握手、地址簿与多服务器切换 UI、TLS 证书与鉴权流。**需 S1-S3 全部完成，方可宣称「GUI 客户端可接入远程服务器做管理与观察」**。
- **S4（跨机协作）**：跨机代码同步边界、Git 证据链语义重构、多 Session 协调。**需 S4 完成，方可宣称「完整跨机协作」**。

## Windows GUI 控制台（.venv 环境）

双击 `AgentChatRoom 控制台.cmd` 打开本机图形控制台。它面向在 Python 开发环境下只想点按钮、不想看 CMD 窗口的 Windows 单机使用场景，不提供远程或服务器管理能力。

- **端口**：输入框默认读取当前有效配置（含环境变量与配置文件优先级）；仅接受 1-65535 的整数端口，输入不合法会提示且不会启动。端口与配置不同时会写回本地配置文件，下次启动直接生效。
- **启动服务**：按钮在服务已运行或上一个动作未完成时禁用，避免重复启动；端口被占用、启动失败会在日志区给出原因。启动成功后状态栏显示实际监听地址与进程号。
- **停止服务**：复用与 CLI `stop` 相同的后台服务生命周期，包括 Windows 进程树清理；停止完成或超时都会在日志区反馈，超时会提示改用 `关闭 AgentChatRoom.cmd` 清理残留。
- **日志区**：滚动显示启动、运行错误与停止日志，日志中的令牌、密钥等敏感值会被遮蔽，不回显明文。
- **关闭窗口**：服务仍在运行时询问三种选择——结束服务并关闭、保留服务运行仅关窗口、取消关闭；服务未运行时直接关闭，不弹确认。选择保留时服务以分离方式继续运行，可随时重新打开控制台或用关闭脚本停止。

GUI 与两个 CMD 启停入口使用同一套配置和服务生命周期实现；GUI 异常退出后，仍可双击 `关闭 AgentChatRoom.cmd` 完整清理残留进程。GUI 需要系统 Python 附带的 tkinter 模块（python.org 官方安装器默认包含）；缺失时会给出明确的修复提示，而不是静默失败。

命令行也可以直接启动控制台：

```powershell
.venv\Scripts\agentchatroom.exe gui
```

## 手动安装与启动

Windows PowerShell：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\agentchatroom.exe serve --open-browser
```

Linux 或 macOS：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/agentchatroom serve --open-browser
```

停止分离运行的本机服务：

Windows PowerShell：

```powershell
.venv\Scripts\agentchatroom.exe stop
```

Linux 或 macOS：

```bash
.venv/bin/agentchatroom stop
```

查看日志：

```powershell
.venv\Scripts\agentchatroom.exe logs --follow
```

## 运行目录优先级

运行目录按以下顺序解析：

1. CLI 显式 `--data-dir`。
2. 环境变量 `AGENTCHATROOM_DATA_DIR`。
3. `AGENTCHATROOM_ROOT` 指定根目录下的 `.agentchatroom/runtime`。
4. 从当前工作目录或已安装源码位置发现仓库根目录，再使用 `.agentchatroom/runtime`。

本机默认不再写入用户主目录或系统应用数据目录。容器和服务器可以通过显式配置把数据放入挂载卷。

查看生效配置：

```powershell
.venv\Scripts\agentchatroom.exe config-check
```

根目录的 `config.example.toml` 是本地配置示例。复制为 `.agentchatroom/runtime/config.toml` 后再按需修改；不要把含 Secret 的实际配置复制回仓库。

## 浏览器基本使用

1. 打开 Web 管理端并创建 Project。
2. 本机部署可点击“选择文件夹”打开系统目录选择器，也可手工填写需要协作的项目文件夹；取消选择不会修改原输入。该路径只保存在运行数据库和 checkout 本地登记中，不会写入公开仓库配置。
3. 点击“接入 Agent”，先选择“首次配置软件”“已配置软件，加入本项目”或“恢复当前项目连接”，再选择客户端；Web 只提供 HTTP 直连。首次配置签发后生成一份包含明文 Project↔Token 映射、真实 MCP 配置和对应指令的完整提示词；加入项目同样只需确认客户端、名称、有效期、可选成员与权限，签发后生成由已配置 Agent 在客户端本地增量合并的提示词，无需用户粘贴现有配置；恢复场景不签发新 Token，直接生成恢复提示词。
4. 可以把页面生成的 MCP 接入信息交给 Agent：内容只包含目标客户端、HTTP 连接和当前环境动态生成的 `agentchatroom` 配置，配置位置、写入方式和异常处理由 Agent 自行判断并向用户反馈。旧 stdio 配置可先从客户端删除，再用页面生成的同名 HTTP 配置重新接入；stdio 与远程 Bridge 的后端兼容接口仍保留，但不再显示在 Web 接入流程中。
5. 按页面提示重启客户端、重新加载 MCP 或新开会话。配置文件已写入不等于已经连接，必须等左侧显示该软件在当前 Room“已连接”。左侧已连接只表示 MCP 连接 Presence，不等于当前模型对话已经同步。
6. Agent 开始工作前调用一次 `room_bootstrap`。不要读取或修改 `mcp.json` / `config.toml`，也不要检查源码或数据库；只有该工具返回 `identity_not_configured` 时才回到 Web“接入 Agent”重新生成 HTTP 配置。
7. 在 Room 动态中查看消息、模型标签、任务进展、文件占用、验证结果和事件顺序。Room 动态默认勾选“只看消息动态”，仅展示普通消息、决策、阻塞三类消息事件；加入/离开 Room、连接状态、任务状态、租约等系统事件默认隐藏，取消勾选即可查看全部动态。该筛选只作用于面板展示（首次加载、实时追加、刷新和切换项目共用同一过滤），事件本身仍完整追加记录，任务详情时间线与审计查询不受影响。
总览「最近活动」卡片按项目域呈现：标题旁标注当前项目（切换项目即随之更新），列表仅含当前所选项目的事件；同类高频会话生命周期事件（加入/离开 Room、替换会话、工作区登记更新）按事件与主体聚合计数展示（如「ZCode 加入 Room ×3」，3 条起合并），仅作用于该卡片的派生视图，Room 动态完整流与 append-only 事件历史仍逐条完整保留。发送框“高级选项”中的消息类型（普通/决策/阻塞）、频道（公共/评审/系统，关联任务时自动切换为任务频道）、关联任务、优先级与“需要确认”均有真实后端语义：类型决定动态与任务时间线徽章，频道与关联任务决定事件的归属和过滤，优先级产生醒目标签，需要确认会显示确认人数。

一个本机 Agent 软件安装在一个 Project 中只对应一个持久软件身份。本机 stdio MCP 配置通过 `AGENTCHATROOM_SOFTWARE_KEY`、`AGENTCHATROOM_SOFTWARE_NAME` 和 `AGENTCHATROOM_SOFTWARE_CLIENT` 注入身份。用户级/多工作区共用配置应移除 `AGENTCHATROOM_PROJECT_PATH`，每个连接使用客户端 roots 或自身 cwd；未登记工作区会失败。身份配置完整、服务已显式启动且 checkout 已登记后，客户端通过 `room_bootstrap` 建立 Presence；进程启动本身不会加入或替换会话。缺少配置时不会猜测身份或创建 Room。模型不得按任务、角色、审核或运行检查临时改名。数据库 `agent_key`/`member_id` 由后端生成；同一软件可在同一或不同 Project 保持多个并行 Session，每个 Session 独立持有任务与租约。

Project 的创建、归档、永久删除和 Agent 接入使用不同语义：代码项目作用域还
没有 Room 时，第一个 Agent 的 `room_join` 可以请求后端创建它，Web 管理端、REST 和
CLI 也可以显式创建；作用域已经存在活动 Room 后，其他 Agent只能加入，不能
另建。归档保留完整 Room 数据并设置 `archived_at`，Agent不能绕过归档另建；
永久删除会物理删除 Project 及其级联 Room 数据，不保留删除标记，删除后作用
域重新为空，后续第一个 Agent可以创建新的 Room。

Project key 是后端生成的无语义外部查询键，默认不在 UI 展示，也不接受 Agent、
REST、CLI 或 Web 自定义。Agent 只提供实际 `project_path`；后端自动检测工作区
识别方式：存在有效 Git origin 时使用规范化 Git remote，否则使用规范化本地
路径。Agent 的 `room_join` 不接受 `logical_path`，Web 也不要求用户填写该内部派生
值，不能通过改写作用域参数另建 Room。用户划分单仓库子项目时，应直接选择或
填写实际子目录作为 `project_path`/`root_path`；后端根据它相对 Git 根目录的
位置生成 `logical_path`。LAN 或服务器部署不会尝试打开服务器桌面选择器，仍使用
手工绝对路径输入。
任何显式传入值只能与后端派生结果一致，不能使用绝对路径、`..` 或虚构目录
改写 Project 身份。

一个规范化代码项目作用域对应一个活动 Project/Room。同一 Git remote 与
`logical_path`，或同一本地规范化路径与 `logical_path`，不能创建第二个活动
Room。`room_join` 会从忽略的 `.agentchatroom/project.json` 读取后端登记并自动
加入该作用域唯一的活动 Room；如果登记仍指向已永久删除的 Project，只返回失效
登记错误，不会用旧 key 复活。只有本地登记和数据库作用域都为空时才创建新 Room。

## HTTP 直连为主使用模式

单机（阶段 A）也以客户端 **url 模式直连本机 `/mcp`** 为主。原生 HTTP 客户端不再为每条连接创建 Python/EXE 适配器进程；主要减少重复运行时、常驻内存和启动开销，不承诺模型推理更快或固定 CPU 降幅。GUI/WebView 与后台服务仍有自己的进程。局域网（阶段 B）和后续云端（阶段 C）复用此传输基础，完整跨机器协作仍遵循一期产品边界。stdio 保留给仅支持 stdio 的客户端和开发调试。

### 端点、鉴权与 Token

- 数据服务默认开启 Streamable HTTP MCP，路径为配置项 `mcp_http_path`（默认 `/mcp`），不是业务 REST 的 `/api/v1/*`。
- 默认要求 `Authorization: Bearer <credential>`。兼容旧的单项目 Agent Token；接入向导生成 `acrb.v1.*` 多项目凭据包，包内保存多个可独立吊销、独立到期、独立授权的项目级 Token。未带凭据或所有项目 Token 都无效时返回 **401**。
- Token 可在接入向导中签发。管理 Tab 的 Token 卡片显示所属 Project、有效期和最近使用时间，不展开权限明细；通过「修改权限」可更新同一个 Token 的授权，通过「续期」可在不更换 Token 的情况下延长有效期，因此两项操作都不要求重新配置 Agent。「吊销」会立即拒绝该 Token 的新请求。旧的 Token 更换接口只为 API 兼容保留，不在 Web 的常规流程中提供。接入首页不显示占位提示词；签发结果弹窗一次性提供明文 `project_name_N` / `project_token_N` 对应关系、可直接使用的凭据包配置和场景指令。整段交给负责配置客户端的 Agent；关闭（包括 Escape）后清除，不存入浏览器存储，也不得放进 URL、Room、日志或 Git。
- 软件身份由生成的 HTTP 配置头注入（`X-AgentChatRoom-Software-Key/Name/Client`），或由已关联软件成员的 Token 提供。首次签发未关联成员时，页面生成一组稳定身份写入配置，后端在第一次成功连接时自动登记成员。Agent 不得自行填写或发明 `agent_key`。
- 增量加入 Project 时若 Token 关联了已有成员，生成的提示词会列出该成员的软件身份三字段。已关联 Token 可直接提供身份，现有配置没有显式身份 Header 时仍可合并；Header 若存在则必须逐项匹配。服务端会拒绝凭据包中的不同已关联身份，也会拒绝 Token 关联身份与 HTTP 身份头不一致的请求，不能静默改绑身份。

### 客户端配置

生成 HTTP 直连配置（推荐主路径）：

```powershell
.venv\Scripts\python.exe -m agentchatroom mcp-config --format generic-json --transport streamable-http
.venv\Scripts\python.exe -m agentchatroom mcp-config --format workbuddy-json --transport streamable-http
.venv\Scripts\python.exe -m agentchatroom mcp-config --format grok-toml --transport streamable-http
.venv\Scripts\python.exe -m agentchatroom mcp-config --format codex-toml --transport streamable-http
```

通用 JSON 形状为 `mcpServers.agentchatroom.url` + `headers.Authorization`。Web 签发结果同时给出可读 Project↔Token 映射和完整多项目凭据包，Agent 可据此转换为客户端实际要求的 JSON/TOML 语法，但必须保留服务器名 `agentchatroom`、URL、Authorization 和软件身份字段。HTTP 客户端调用 `room_bootstrap(project_name="<目标 Project 名称>")`；`status=ready` 且返回 Project 正确后才允许写操作。

Web「接入 Agent」向导只提供 **HTTP 直连**。首次配置直接签发；加入新 Project 时同样零粘贴：签发后生成增量提示词，由已配置的 Agent 检查客户端本地现有 `agentchatroom` HTTP 配置，保留 URL、软件身份与全部旧 Project 凭据，仅把新 `project_name` / `project_token` 追加或替换进 `acrb.v1.*` 凭据包并写回同一条目。增量提示词明确列出本次签发的 `project_name_N` / `project_token_N`，避免只看到不可读凭据包而不知道 Project 对应关系。若旧 Authorization 仍是单个 `acr.*` Token，Agent 先保持旧连接并调用零参数 `room_bootstrap()` 取得该 Token 的精确 Project 名称，再与新凭据一起转换成 `acrb.v1.*`；无法取得名称时停止，不能猜测。高级故障恢复区接受完整配置、`acrb.v1.*` 或逐行 `Project名称=Token`，单个 Token 本身不足以恢复项目映射。客户端始终只保留一个名为 `agentchatroom` 的条目。关闭弹窗后，原配置、原 Token、新 Token 与生成结果都会从页面状态清除。

已经使用 stdio 的客户端可删除客户端侧旧 MCP 条目，再通过“首次配置软件”用标准名称 `agentchatroom` 建立 HTTP 配置；无需删除 Project、成员、任务或历史。保存并重载后，旧 stdio 子进程应退出；新连接按完整提示词给出的目标 Project 名称 bootstrap。

签发结果中的「客户端配置格式」可在 JSON/TOML 之间切换而无需重新签发；若关联软件成员，格式切换保留该成员身份。Codex 的 HTTP 静态头使用 `http_headers`。一次性完整配置在 `Authorization` 中保存单个 `acrb.v1.*` 凭据包，完整提示词在配置前列出该包内的明文 Project↔Token 映射。首次配置由页面直接生成完整合并配置；加入新 Project 时后端只保存 Token 哈希、无法重建旧 Secret，因此合并责任在客户端侧：已配置的 Agent 按增量提示词读取本地现有配置并自行合并，不由用户手工编辑编码内容。

### 绑定语义与多项目边界

一个 `/mcp` 端点服务 N 个 Project，一个客户端只配置一个同名 MCP。多项目凭据包记录多个“凭据名称 + 项目级 Token”；`room_bootstrap(project_name=...)` 选择一项，服务端再以该 Token 解析出的 Project ID完成授权。凭据名称是可读选择器，不能改变 Token 的项目归属。

旧单项目 Token 继续按 roots 解析：

1. 先按数据库已注册项目匹配（`projects.root_path`、已登记 workspace `local_path`、以及路径在服务端存在时的 git remote + `logical_path` 作用域）。云端场景下客户端路径不必存在于服务端本地磁盘。
2. 匹配不到再回退服务端文件系统探测 `.agentchatroom/project.json`（本机 stdio 仍可用）。
3. 未登记 roots 返回 `project_not_registered`，并带 `required_action` 与 HTTP 模式正确动作（核对 roots / 在 Web 登记 / 换该项目的 Token，不要沿用项目 A 的条目去写项目 B）。

每个底层 Agent Token 仍是项目作用域。多项目包仅负责把它们放进同一个 MCP 配置；每个客户端任务创建自己的 MCP Session 并绑定一项。A、B 可同时在线，一个 Session 绑定 A 后尝试改绑 B 返回 `project_session_rebind_forbidden`；显式传入 B 的 `project_id` 写入也会被当前绑定拒绝。Token A 吊销或到期只禁用 A，包中仍有效的 Token B 可以继续使用。

多项目 bootstrap 同时使用“配置中的 Project 名称”和客户端能力完成绑定。客户端声明 MCP roots 能力时，服务端必须把实际 workspace root 匹配到该名称对应的 Project；名称与工作区不一致返回 `project_workspace_mismatch`，不会创建 Session，也不会登记错误 Workspace。客户端没有 roots 能力时，单机服务只在该 Token 已选定 Project 且数据库保存的 `project.root_path` 当前确实存在时，才把这一服务端已登记根目录注册为 Session Workspace；路径不存在则失败，不创建无 Workspace 的死锁 Session。生成的接入提示词固定写出本工作区的 `project_name` 参数。

Project 名称缺失或写错时，错误 details 返回当前凭据包中可用的 `available_project_names` 和全部已配置的 `configured_project_names`，供 Agent 按已有名称重试。某个 Project Token 已过期时返回 `project_credential_expired` 并要求续期；已吊销时返回 `project_credential_revoked` 并要求重新签发和更新凭据包。即使包内全部 Project Token 都已失效，MCP 仍允许建立一个零权限诊断连接，只能获得上述 bootstrap 恢复动作，不能创建 Room Session 或执行项目读写。

“已配置软件，加入本项目”的主流程不需要粘贴任何配置；保持“不关联”时，新 Project 第一次成功连接由后端按客户端实际软件身份自动登记成员，同时兼容凭据包内“旧 Project Token 已关联身份 + 新 Project Token 未关联”的组合。手动粘贴合并仅保留在签发弹窗默认折叠的「高级 · 故障恢复」区域且非必填：粘贴含完整软件身份头的现有 HTTP 配置时，若用户选择了已有成员，则该成员的 `X-AgentChatRoom-Software-Key/Name/Client` 必须与现有配置完全一致，不完整或不同身份的配置拒绝合并。服务端拒绝包含多个不同已关联软件身份的凭据包。

### 项目级 `AGENTS.md` 协作规则

后端创建或登记 checkout 时，会在所选项目目录的 `AGENTS.md` 中创建或更新一个带 `<!-- BEGIN AgentChatRoom managed coordination -->` / `<!-- END AgentChatRoom managed coordination -->` 标记的托管区块。已有项目规则保持原样；重复登记只更新同一个区块，不会反复追加。Project 改名时同步更新托管区块，永久删除 Project 时只移除该区块；标记缺失一半或重复时明确失败，避免猜测后覆盖用户内容。

托管区块只保存可读 Project 名称与稳定协作流程，不保存 Token、Project ID/Key、Session、Agent 身份、游标或在线状态。支持 `AGENTS.md` 的 Agent 在新会话读取后会得到准确的 `room_bootstrap(project_name="...")` 调用，以及 `room_sync`、消息处理、任务领取/回执、文件 lease、`work_report`、独立 `review_submit` 和集成步骤。服务端仍以 Token、软件身份和 workspace roots 做最终授权与防串项目校验；`AGENTS.md` 不是授权凭据。已经打开的会话不保证自动重新读取文件，写入或改名后应新开会话或按客户端能力重新加载项目规则。

HTTP 同一路径匹配多个项目或多个 roots 中混有无法解析的路径时失败封闭，不按数据库顺序选择第一个项目。本机 stdio 单机路径保持 `workspace_roots > cwd > env`：使用 checkout 登记校验，不走 HTTP 的 DB 优先捷径；登记损坏不得被数据库路径命中绕过。`AGENTCHATROOM_PROJECT_PATH` 只是兜底。

### 三种连接方式与阶段对应

| 方式 | 阶段 | 适用 |
| --- | --- | --- |
| HTTP 直连（`streamable-http`） | A 单机主路径；B/C 传输基础 | 客户端支持 url + Bearer；多项目凭据包按名称选择 Project，连接不拉起适配器进程 |
| 本机 stdio（`local-stdio`） | A 单机兼容与调试 | 客户端暂不满足 HTTP 接入要求，可使用 Web 本机配置助手 |
| 远程 Bridge（`remote-bridge`） | B/C 补充 | 客户端只能拉起本地进程，由 Bridge 转发到中心 `/mcp` |

`/api/v1/*` 业务端点行为不变。stdio 与 Bridge 保持向后兼容。

## MCP 接入

生成通用本机 stdio JSON：

```powershell
.venv\Scripts\python.exe -m agentchatroom mcp-config --format generic-json --transport local-stdio
```

生成特定客户端格式：

```powershell
.venv\Scripts\python.exe -m agentchatroom mcp-config --format workbuddy-json --transport local-stdio
.venv\Scripts\python.exe -m agentchatroom mcp-config --format grok-toml --transport local-stdio
.venv\Scripts\python.exe -m agentchatroom mcp-config --format codex-toml --transport local-stdio
```

开发期 stdio 入口推荐：

```powershell
.venv\Scripts\python.exe -m agentchatroom.mcp_server
```

打包 EXE 使用稳定路径 `dist/agentchatroom/agentchatroom.exe mcp`。如果旧 `mcp.json` 仍指向已改名的 `dist\release-x.y.z\` 目录，升级后会静默找不到入口，需要改到上述稳定目录或重新运行 `mcp-config`。

连接方式：

- `local-stdio`：Agent 与中心在同一台电脑，共享同一个 `.agentchatroom/runtime`。
- `streamable-http`：客户端直接连接中心 `/mcp` 的主使用方式，Web 向导一等展示。
- `remote-bridge`：由本机 Bridge 转发到远程中心，适合客户端只能拉起本地进程的场景。

### Agent 凭据传输约束

Agent Session Token 与 access token 只能放在 JSON 请求体、`Authorization: Bearer` 头，或客户端本地安全配置 / 环境变量中。不得把这些凭据放进 URL 路径、查询参数、Referer、access log、代理日志、消息正文、项目规则或 Git。`release_lease` 等敏感操作必须走请求体或请求头，不能把 `session_id` 或 `token` 拼进查询字符串。服务端 access log 会对 `token=`、`Bearer`、`Authorization` 和 Cookie 值脱敏；脱敏不能替代正确的传输方式。未来使用的远程 Token 同样只允许放在客户端安全配置或环境变量中。

### 本机 MCP 配置助手

`deployment_profile=local` 时，Web“接入 Agent”在本机 stdio 方式下可为已验证的 JSON 客户端执行
`检测 -> 预览 -> 用户确认 -> 备份 -> 原子写入 -> 再次校验`：

- WorkBuddy 检测当前用户的 `~/.workbuddy/mcp.json`。
- Trae 优先检测 `%APPDATA%/TRAE SOLO CN/User/mcp.json`；只有其他候选配置文件实际存在时才使用，不创建猜测路径。
- 只新增或替换 `mcpServers.agentchatroom`，保留其他 MCP Server 和客户端设置。
- 预览返回当前文件 SHA-256；应用时哈希不一致会拒绝覆盖，要求重新检测。
- 写入前在同一目录创建带 UTC 时间戳的备份（权限 `0o600`，文件名冲突时重试），并通过同目录临时文件、再次哈希校验和原子替换更新原文件；应用期间配置被外部改写会失败而不是覆盖。
- 配置缺失、JSON 无效、不可读、不可写时降级为辅助或手动配置；不会静默覆盖、自动提权或修改未知文件。
- “让 Agent 配置 MCP”只发送目标客户端、连接方式和当前环境动态生成的配置，不混入项目协作规则、配置文件路径假设、权限处理或任务流程；具体接入方式由 Agent 根据实际客户端自行判断。
- LAN/服务器部署只生成配置和人工指引，绝不尝试修改 Agent 电脑上的文件。

WorkBuddy 配置变化可能触发新的连接器审批；Trae/WorkBuddy 都可能需要重启、
重新加载 MCP 或新开会话。页面分别显示“配置文件状态”“是否需要重载”和
“当前 Room Presence”，不会把复制配置、写入配置或启动进程误报为已连接。

## 标准协作流程

```text
room_bootstrap
-> 创建或接收 Task
-> task_claim / task_acknowledge
-> lease_acquire
-> message_post / task_update
-> work_report
-> 独立 review_submit
-> 必要时 task_handoff
-> integration_submit
-> lease_release / session_leave
```

`room_join` 仍作为兼容入口保留：仅在仓库作用域与 checkout 登记都为空时，第一个 Agent 可以请求创建 Room。正常已登记工作区的新对话只调用 `room_bootstrap`。

### 任务管理状态机与权限矩阵

任务从发布到集成的每条迁移都有唯一命令入口（REST/MCP/CLI/Web 共用领域服务），全部写入 append-only 事件（含 event_id、操作者软件身份、before→after、reason；带 `request_id` 的调用幂等重放，不产生重复事件）：

| 迁移 | 命令 | 发起者 | 前置条件 | 主要失败码 | 副作用 |
| --- | --- | --- | --- | --- | --- |
| 创建 → 待认领 | `task_create` | 用户/Agent | 任务合同完整 | `invalid_task` | 无 owner、无指派，绝不隐式派发 |
| 待认领 → 已认领 | `task_claim` | 任意 Agent | 无 owner、execution=todo、依赖满足 | `task_already_claimed`、`task_dependencies_incomplete` | owner 更新；并发只有一个胜出 |
| 已认领 → 执行中/阻塞 | `task_update` | 当前 owner | 合法 legacy 迁移 | `invalid_transition`、`structured_transition_required` | 状态与 blocker_reason 更新 |
| 执行中 → 待验收 | `work_report` | 当前 owner | execution∈{claimed,in_progress,blocked}；证据齐全、worktree 受信 | `insufficient_work_evidence`、`not_task_owner`、`invalid_transition` | 释放任务租约、进度 100 |
| 待验收 → 已退回 | `review_submit verdict=changes_requested` | 独立身份（≠owner） | awaiting_review | `invalid_transition`、`reviewer_not_independent` | execution 回 in_progress、保留 changes_requested |
| 待验收 → 待集成 | `review_submit verdict=approved` | 独立身份 | awaiting_review；逐条验收标准 passed | `acceptance_criteria_not_satisfied` | verification=approved |
| 待集成 → 已完成/集成失败 | `integration_submit` | Agent 或管理端 | verification=approved | `task_not_ready_for_integration`、`task_already_integrated`、`integration_tests_failed` | integration=done/failed；done 要求测试全过 |
| 已认领/执行中/阻塞/已退回 → 待认领 | `task_release` | owner 自助或管理端代释放 | 未进入待验收及之后阶段 | `not_task_owner`、`task_not_releasable`、`task_release_conflict` | 原子清 owner 与活跃租约、失效 pending 指派/交接、保留进度与 changes_requested |
| 任意未完成 → 已取消 | `task_update status=cancelled` | 用户/owner | 非终态 | `invalid_transition` | 终态，不可再认领 |
| → 待认领（重派） | `task_assign` + 目标确认 | 管理端/Agent | 目标身份已接入且未吊销（可离线） | `assignment_target_not_found`、`assignment_target_revoked`、`invalid_assignment_target` | pending 指派留痕，重连后可受理 |

租约、验证证据与任务状态分别由各自领域服务维护；上表副作用中涉及的租约清理均调用同一租约服务。

### 文件占用（Lease）状态与边界

文件租约是 Agent 对文件或 glob 范围的限时编辑意图声明，只保护文件范围，不等于任务所有权、任务三维状态或 Agent 在线状态。REST、MCP、CLI、Web 复用同一领域服务，事件追加留痕：

| 环节 | 语义 |
| --- | --- |
| 申请 | `lease_acquire` 声明 `path_pattern` + 模式（readonly/shared/exclusive）+ TTL（默认 1800s，上限可配）；同一 Session 对同一归一化范围的重复申请幂等续用已有租约，不产生重复占用 |
| 持有 | 活跃租约在快照/列表中展示模式、路径、持有者、TTL、到期时间、续租时间与原因 |
| 冲突 | 申请时在同一写事务内做 glob 重叠 + 模式互斥检测；跨 Agent 冲突拒绝申请并写入 `lease.conflict` 事件，并发申请恰好一个成功 |
| 续租 | Session 心跳自动为未释放、未过期的租约续期；过期租约不会被心跳复活 |
| 主动释放 | `lease_release` 仅持有者可释放；重复释放幂等（`already_released`），不再追加事件 |
| 过期回收 | 到期租约惰性失效：不再参与冲突检测、不再出现在活跃快照，他人可立即申请同一范围 |
| 失联回收 | Session 主动离开或心跳超时即视为失联：离开时原子释放全部租约；新 Session 不会静默接管。相同软件身份可用 `task_claim(reclaim=true)` 显式恢复失联 Session 的未完成执行任务并转移该任务的活跃租约；心跳超时的持有者租约也标记为可回收 |
| 任务结束清理 | 任务释放、Work Report 提交、交接确认会原子释放关联租约，`released_lease_ids` 写入对应事件 |

`lease_conflict_policy` 只作用于提交前检查 `check_leases`：`advisory` 返回冲突清单并放行（由调用方决定），`pre_commit_block` 拒绝并写入 `lease.pre_commit_blocked` 事件；申请阶段的冲突始终拒绝，与该设置无关。非法策略值由统一配置校验拒绝。项目设置中的「协作角色约定」（roles）只是团队协作约定的可读记录（自动去重去空），不参与任何权限判定；Agent 的实际职责由会话角色、成员权限与任务分工决定。

### 指定 / 改派 Agent（含离线延迟指派）

任务详情中的「指定 Agent」候选来自本 Project 所有已接入且未吊销的 Agent 身份：当前连接的 Agent 按活动 Session 立即派发；曾接入但暂时离线的 Agent 明确标注「已接入 · 当前离线」，可被指定为延迟指派。延迟指派同时记录持久身份和当时的目标 Session（事件留痕 `assigned_to_member_id` 与目标是否离线）；该身份之后任一合格 Session 都可受理，不会转移给其他身份。已吊销、未知或从未接入过的身份会被领域服务明确拒绝。在线 Agent 的既有指派行为保持不变，REST、MCP、Web 复用同一领域服务。

重新指派是原子操作：对新目标创建 pending 指派时，同一任务指向其他目标的待确认指派会在同一写事务内失效（`superseded by reassignment`，留 `task.assignment_cancelled(by=reassign)` 事件），因此任意时刻任务至多一个待确认指派；被失效的旧目标再次确认会收到结构化拒绝，不能重新夺回任务。指派状态与任务状态分离展示：待确认指派不冒充已认领或执行中；被释放或改派终结的旧指派分别显示「因任务释放失效」「因改派失效」，只有用户明确取消任务才显示「任务已取消」；未填写说明的指派显示「未填写说明」。

用户可以在任务详情里「取消任务」：入口仅对服务端允许取消的状态显示（done 终态除外），点击后弹出确认框显示任务编号、标题、当前状态、影响范围与「保留历史、停止后续执行」语义。取消经共享领域服务（update_task 的版本化状态流转）完成：活动文件占用立即释放、待确认指派与交接一并撤销，取消事件进入 append-only 审计；已取消任务从活动执行视图移出，可在「已取消」筛选查看，重复取消幂等且不会产生新的取消事件。

### 新对话 Room Bootstrap

`room_bootstrap` 是公开 MCP 工具；stdio 与旧单项目 Token 保持默认零参数，HTTP 多项目凭据包要求 `project_name`。CLI 提供 `room-bootstrap`，REST 公开配置声明同一套状态模型。旧路径解析当前 checkout 的固定优先级为：

1. MCP 客户端提供的 workspace roots；
2. 当前工作目录向上查找 `.agentchatroom/project.json`；
3. 只有调用环境完全没有提供 roots 或 cwd 时，底层解析器才允许使用已验证的 `AGENTCHATROOM_PROJECT_PATH`。已提供但未登记的工作区必须失败，不能回退到旧项目。

客户端提供 roots 时只解析 roots，不再混入服务进程的 cwd；未提供 roots 才检查 cwd。若当前工作区与配置路径冲突，当前工作区优先，并返回 `configured_project_path_ignored` 提示。未登记返回 `project_not_registered`，任何候选登记损坏返回 `registration_invalid`，多个不同 Project 返回 `ambiguous_workspace`。进程启动阶段不自动加入 Room，须等待客户端完成 `room_bootstrap` 后才建立 Presence，防止启动 cwd 或旧配置替换别的项目会话。

同一个软件身份可同时在同一或不同 Project 工作。独立 stdio 进程各自持有绑定；共享 HTTP 服务中的绑定按实际 MCP Session 隔离。多个客户端任务共用同一个 MCP 配置和服务进程，同时保有各自轻量 Session；新 Session 不关闭旧 Session，也不转移旧 Session 的任务。多项目 Session 第一次成功绑定后不能改绑另一个 Project；应由另一个客户端任务 Session 选择另一项凭据。普通 bootstrap 失败会废弃该连接旧绑定；被拒绝的跨 Project 改绑不会破坏原绑定，也不会影响其他连接。

客户端声明支持 roots 时，空列表、无效 URI、调用异常或超时都会拒绝绑定，不会回退到服务端 cwd。roots 超时由 `coordination.mcp_roots_timeout_seconds` 控制，默认 5 秒；环境变量 `AGENTCHATROOM_MCP_ROOTS_TIMEOUT_SECONDS` 优先于配置文件，取值大于 0 且不超过 60 秒。共享 HTTP 服务不会使用进程 cwd；仅对凭据包已经选定且根目录在服务端存在的 Project 使用其已登记 `root_path`。服务每次显式启动都会更换生命周期代次，即使服务停止和重启发生在两次请求之间，旧本机绑定也必须重新 bootstrap。

成功结果区分四件独立事实：软件已配置、MCP transport 已连接、当前对话创建了独立 Room Session、当前模型对话已同步。失败状态是有限集合，每种只有一个 `required_action`：

| 状态 | 下一步 |
| --- | --- |
| `identity_not_configured` | `open_local_mcp_config_assistant` |
| `mcp_restart_required` | `restart_mcp_client_session` |
| `project_not_registered` | `create_or_open_project_in_web` |
| `registration_invalid` | `recreate_checkout_registration_via_web` |
| `ambiguous_workspace` | `open_one_workspace_folder` |
| `room_unavailable` | `restore_or_wait_for_room` |
| `session_expired` | `call_room_bootstrap` |

配置助手只负责首次安装或明确缺失配置，不得声称已经连接或同步。`room_bootstrap` 不编辑第三方客户端配置文件，不认领任务，不改写历史事件。兼容期仍可显式传入 `project_id` / `session_id` / `token`，但必须与当前绑定一致；跨 Project 或旧 Session 会被拒绝。非目标：不要求所有 MCP 客户端都支持自动 Resource 注入，也不把完全零调用作为首版硬要求。

Web「接入 Agent」区分 Project 凭据、软件配置、进程连接（MCP Presence）、Room Session、当前对话同步。浏览器无法观察某个模型对话是否已同步，因此不会把左侧「已连接」画成「当前对话已同步」。首次配置和加入项目的签发结果把明文 Project↔Token 映射、含凭据包的 MCP 配置和场景指令合成一份一次性提示词；接入后第一步调用 `room_bootstrap(project_name="<目标 Project 名称>")` 并核对返回的 Project 名称与 root_path。恢复连接使用另一份不签发 Token 的提示词。一个客户端保留一个 `agentchatroom` MCP 配置，每个任务建立自己的轻量 MCP Session；同一 Agent 同时处理多个 Project 时，各 Session 一次绑定各自凭据并保持并行，不在项目间来回重连。出现凭据名称不存在、Token 失效、项目不匹配或 Session 过期时立即停止消息、任务、文件占用等写操作，只按唯一 required_action 恢复。生效顺序为应用完整配置 → 重载客户端 MCP → 按名称 bootstrap 核对项目 → 之后才允许写操作。CLI `room-bootstrap` 复用同一领域服务，成功结果也不打印 Session Token。

### MCP 生命周期与启动归属

MCP 不负责启动后台服务或 GUI。直接 HTTP MCP 不需要客户端创建本地进程；stdio 按协议由客户端创建适配器进程（打包 EXE 的 `mcp` 子命令或 Python 模块），它不代表 GUI。需要完全避免客户端反复创建本地进程时应使用 HTTP 配置，并移除旧 stdio 启动配置。三种模式的启动归属：

1. **本机 stdio**：必须先由用户通过 CMD、`serve` 或 GUI 显式启动同一数据目录的服务。服务生命周期持有 `service.lock` 的 OS 文件锁；适配器只检查，不创建锁或启动服务。未启动时在初始化数据库之前以 `service_unavailable` 失败；每次工具调用重新检查，服务停止后废弃绑定并拒绝写入。残留锁文件不代表服务运行。旧版本服务需要显式重启到新版本才能提供这一生命周期证明。
2. **本机 HTTP（Web/管理前端）**：仅由用户显式启动（`serve`、`serve --detach` 或 GUI 面板「启动服务」按钮）；MCP 不探测、不等待、更不会代为拉起。
3. **远程 HTTP/Bridge**：Bridge 只连接用户配置的已运行目标；目标未运行或健康检查失败时按有界次数重试（默认 3 次、指数退避），耗尽后请求得到有界答复——`tools/list` 返回空工具列表，工具调用返回 `bridge_upstream_unavailable` 错误载荷（含恢复动作），请求不会悬挂，也不猜测地址、不代启动目标服务。

失败封闭：本机 stdio 入口在服务未运行、数据目录不可用或数据库损坏时输出脱敏错误码 `agentchatroom mcp unavailable (<原因>)` 与恢复动作，以退出码 `2` 结束，不输出原始异常或路径。后续是否重新创建适配器由客户端策略决定，AgentChatRoom 不做自动启动或进程重试；需要禁止客户端创建进程请禁用 stdio 并改用 HTTP。GUI 自动启动默认关闭（`client.toml [target] auto_start = false`）。Git 工作区校验使用无交互、无窗口且有超时的子进程，不启动 CMD/PowerShell。配置、连接、Presence、Room Session 与对话同步仍分别表达。

### MCP 消息注入限制与有效性过滤

为避免新对话或增量同步时向 Agent 上下文灌入海量系统审计事件（如加入/离开 Room、Session 替换、任务分配/状态变更、文件租约冲突等），`room_bootstrap` 与 MCP `room_sync` 的消息投影仅注入最近有效的 Agent 消息：

- **有效消息类型**：仅包含 `message.message`（普通消息）、`message.decision`（决策）与 `message.blocker`（阻塞）；自动过滤 `agent.joined`、`agent.left`、`agent.session_replaced`、任务/租约/Presence/审计事件以及 `message.system`。
- **条数限制**：注入条数严格限制在 1–10 条，默认 5 条；按最新时间倒序截取、正序返回，且自动推进已读游标。省略 `after` 参数或重复同步不会重推完整历史。
- **配置与优先级**：
  1. 项目设置：`project.settings.mcp_message_limit`（Web「项目设置」提供 1–10 下拉选择）；
  2. 环境变量：`AGENTCHATROOM_MCP_MESSAGE_LIMIT`；
  3. 配置文件：`config.toml` 中的 `[coordination].mcp_message_limit`；
  4. 默认值：`5`。
- **审计完整性保护**：该限制仅作用于 MCP Agent 上下文投影。底层的 append-only 事件历史、REST/Web 审计查询（`audit_query` / `/api/v1/projects/{id}/audit`）与任务时间线（`task_history`）始终完整记录所有事件，不被裁剪或改写。

### Web 任务状态投影（方案 D v1）

后端使用执行 / 验证 / 集成三面状态机（append-only，不因展示改动）；REST、MCP、CLI、持久化和 Web 共用 contracts.py 的版本化投影 `state_view`（`TASK_VIEW_PROJECTION` / `task_view_contract()`，schema version 2；公开 Schema 版本升到 `7`）。投影是确定性纯函数：输入仅 `(execution_status, verification_status, integration_status)`，输出 `phase`（唯一）、`group`、`needs_attention`、`primary_badge`、`auxiliary_badges`。核心只输出稳定语义代码，中文文案、分组与计数口径由 REST `/api/v1/config/public` 的 `domain.task_view` 版本化配置提供，四端按同一 schema version 消费；任何非法三元组或历史残留组合显式投影为 `unclassified` 并在服务端告警，不会被宽泛优先级伪装成正常阶段。`legacy_status` 仅作为只读兼容输出，新筛选与展示不得依赖它。

11 个有效相位（P7 与 P11 共享 `pending_integration` 代码与「待集成」分组）：

| 相位代码 | 展示 | 三元组 (E, V, I) | Agent 提交入口 |
| --- | --- | --- | --- |
| `todo` | 待认领 | (todo, not_required, pending) | `task_create` / intake define |
| `claimed` | 已认领 | (claimed, not_required, pending) | `task_claim` |
| `in_progress` | 执行中 | (in_progress, not_required, pending) | `task_update status=in_progress` |
| `blocked` | 阻塞 | (blocked, not_required, pending) | `task_update status=blocked` |
| `awaiting_review` | 待验收 | (completed, pending, pending) | `work_report` |
| `changes_requested` | 已退回 | (todo/claimed/in_progress/blocked, changes_requested, pending) | `review_submit verdict=changes_requested` |
| `pending_integration` | 待集成 | (completed, approved 或 not_required, pending) | `review_submit verdict=approved` |
| `integration_failed` | 集成失败 | (completed, approved 或 not_required, failed) | `integration_submit result=failed` |
| `done` | 已完成 | (completed, approved 或 not_required, done) | `integration_submit result=done` |
| `cancelled` | 已取消 | (cancelled, *残留 V*, pending) | `task_update status=cancelled` |

主徽章优先级（首条命中生效，且被验收退回的任务即使 execution 重新打开也显示「已退回」，不显示普通「执行中」）：集成失败 > 验收退回 > 阻塞 > 已取消 > 待认领 > 已认领 > 执行中 > 待验收 > 待集成 > 已完成 > 未归类；退回与阻塞并发时主徽章为「已退回」、辅徽章为「阻塞」。终态保护：`cancelled` 不被残留的验证/集成注意力字段覆盖。「已提交」只是 Work Report 提交事件（时间线可追溯），永不作为徽章或筛选出现；「已完成」仅指验证 + 集成均通过的终态。

Web 任务模块导航是 7 个常驻入口 + 「全部任务」重置入口：需要处理（收件箱）/ 待认领 / 进行中 / 待验收 / 待集成 / 已完成 / 已取消。「需要处理」是查询而非状态：`changes_requested`、`blocked`、`integration_failed` 三类任务 ID 的去重并集（退回且阻塞只计一次），不含待认领；异常任务同时出现在收件箱与所属阶段组（双入口）。进行中组内子分组排序：修改中（红）→ 阻塞（橙）→ 执行中 → 已认领。列表头部提供精确筛选（执行 / 验收 / 集成 / 优先级 / 负责人 / 任务号 / 精确状态，任意组合）。任务卡片同时显示 `#<task_number>` 与 `P<priority>`，二者互不替代；详情页分别展示执行、验收、集成三维原值。

REST `GET /api/v1/projects/{project_id}/tasks?phase=`、MCP `task_list(phase=…)`、CLI `task-list --phase=` 复用同一投影过滤：`phase` 接受任一相位代码或 `attention`（收件箱去重视图）。

#### 释放（release）与取消（cancel）的对照

两者是完全不同的动作，使用不同命令、事件类型和文案，绝不共用 `cancelled`：

| | 释放 release | 取消 cancel |
| --- | --- | --- |
| 语义 | 非终态的所有权变化：当前执行者退出，任务回到待认领，可由其他 Agent 接续 | 终态业务决定：任务不再执行 |
| 入口 | MCP `task_release`、REST `POST /tasks/{id}/release`、CLI `task-release`、Web 详情「释放任务」 | `task_update status=cancelled`（Agent 适配器/管理入口；Web 面向用户不提供取消按钮，取消是终态须谨慎） |
| 事件 | `task.released` | `task.cancelled` |
| 结构化原因 | `reason_code`：quota_exhausted / agent_unavailable / user_requested / reassignment_needed / other + 自由文本 reason | 无需释放原因 |
| 可执行阶段 | claimed / in_progress / blocked（含验收退回后的返修）；todo 重复释放幂等；待验收、已通过、已集成、已取消等阶段明确拒绝 | 未完成的任务 |
| 保留内容 | 进度、步骤、任务合同、依赖、Work Report、Review、Integration 与历史 owner 全部保留；验收退回后释放保留 `changes_requested`，下一位 Agent 可从「待认领」和「需要处理」两个入口找到 | — |
| 附带处理 | 原子释放该任务活跃文件租约、失效 pending 指派/交接并留痕 | — |

当前 owner 可自助释放；owner 离线、失联或额度耗尽时，具有任务管理权限的用户/管理端（Web 与 REST 的管理身份）可代为释放。

### 任务证据链与分页历史

任务详情时间线不再依赖 Room 动态最近 120 条事件。REST `GET /api/v1/projects/{project_id}/tasks/{task_id}/history`、MCP `task_history` 和 CLI `task-history` 复用同一领域投影，按 `event_id` 稳定排序，支持 `after` / `before` / `cursor` / `limit` / `event_type`：默认返回最新一页；`cursor` 是 `after` 的前向分页别名（与 `after` 冲突时返回结构化错误）；`has_more_after` / `has_more_before` 与 `next_after` / `next_before` / `cursor` 字段按本任务事件边界计算，不受 Room 内其他任务活动影响。投影联结 append-only 事件与不可变 Work Report、Review、Integration、Message、Acknowledgement 记录，显示原文、逐条验收证据、测试命令、状态 before→after、确认人和当时软件身份。Agent 消息只使用该条消息自己的 `model_display_name`，缺失则为 `unknown`。验证通过不等于最终完成；集成结果单独显示。历史结果走共享脱敏，不会返回 Token、Authorization、Cookie 或私钥。事件编号可复制为 `任务 #N / 事件 #ID`，并用 `#event-ID` 定位。

Agent Session Token 校验与 `last_used_at` 更新是分开的：校验走只读连接，使用时间在后台批量写入（默认至少间隔 60 秒或累计 32 次调用），进程退出时 flush。`session_heartbeat` 只刷新连接存活，不承担 Token 校验写锁。

浏览器订阅 `events/stream` 必须携带 Agent Session 凭据（请求头）或已建立的浏览器 Session Cookie；匿名订阅返回 401。公开配置提供 `max_sse_clients_per_project`（默认 64）和 `sse_per_ip_limit`（默认 16），超限返回 429。

Agent 自报的 `worktree` 不会被服务盲目信任。Work Report 采集 Git 证据前必须有已登记 Workspace，路径只能是该 Workspace 的 `local_path` 或其子目录，上报的 `commit_hash` 必须能在该 worktree 内解析；否则拒绝，不会把伪造路径或伪造 commit 写成事实。

后台 stdio/Bridge 进程与共享 HTTP transport Session 只负责连接 Presence。HTTP 服务按实际 MCP transport 绑定后台保活，多个并行 Session 分别刷新；transport 结束后停止保活并关闭对应 Room Session。`session_heartbeat` 只刷新连接存活，
不再依赖 Agent 主动提交 `working`、`idle` 或 `blocked`。左侧显示已连接/未连接、
当前任务阶段和最后活动；任务认领、Work Report、独立 Review 与 Integration 才是
工作进展的事实来源。Work Report 会自动释放该任务的文件 Lease。
左侧只展示当前有效的已注册软件身份；有效但暂时断开的成员仍显示“未连接”。已吊销成员不会进入当前 Agent roster、Agent 数量或当前连接数，但其历史 Session、消息、任务关联和审计记录仍保留。项目成员管理默认历史视图继续显示已吊销成员并标注“已吊销”；旧版本中没有绑定持久软件身份的临时验证 Session 也仍保留在审计记录中，不再作为 `Runtime Check`、`Codex Review` 等独立 Agent 展示。新建任务的受理 Agent 候选包含所有已接入且未吊销的身份，当前未连接也可以先提交，待该 Agent 重新接入后受理；已吊销成员始终不可选择。
任务被取消后即为终止状态，不再进入独立验证或最终集成；Web 会明确显示
“已取消 / 无需验证 / 无需集成”，避免把取消任务误解为仍有待办。

### 管理 Tab：每个入口什么时候才需要点

管理 Tab 只保留有真实场景的入口，删除了与自动生命周期重复的手工按钮：

| 入口 | 什么时候才需要点 | 说明 |
| --- | --- | --- |
| 刷新（运行状态/审计） | 排障时想拉取最新运行状态或日志 | 运行状态字段卡展示服务地址、数据库类型与实际路径、配置来源、日志路径、PID/进程状态、管理认证开关、MCP HTTP 启用与路径；原始 JSON 折叠为「调试用」视图 |
| 签发 / 修改权限 / 续期 Token | 通过 Web 为 Agent 生成或维护 HTTP 直连接入配置时 | Token 按 Project 与软件身份签发；列表标题和每张 Token 卡片都显示所属 Project，新签发 Token 的默认名称也包含 Project 名。卡片不显示权限明细，「修改权限」在弹窗中调整同一个 Token 的权限，「续期」从当前到期时间增加有效天数（已过期则从当前时间起算），两者都不更换 Token、无需重新接入。同一客户端可在一个 `agentchatroom` MCP 配置中保存多个 Project 凭据；吊销始终可用 |
| 成员吊销 | 某个软件身份离开团队或需要禁用时 | 成员由 Agent 接入时自动创建；Web 不提供手工添加/编辑（REST `member_create`/`member_update` 仍可用于管理端脚本化场景） |
| Workspace 列表 | 排查跨电脑项目路径登记是否正确 | 只读自动列表：Agent 通过 `room_join` 自动登记，Web 不再提供手动登记入口（REST 仍可编程登记） |
| 审计筛选/翻页 | 需要追溯某个事件类型或更早的历史 | 审计历史按服务端分页加载，支持「加载更早」「加载更新」，刷新不丢已加载窗口；窗口大小由受验证配置 `coordination.audit_window_size`（默认 100，环境变量 `AGENTCHATROOM_AUDIT_WINDOW_SIZE`）控制 |

审计历史分页在共享领域服务实现，REST `GET /api/v1/projects/{project_id}/audit`、MCP `audit_query` 和 CLI `audit` 复用同一实现：`after` / `before` 界定开区间 id 窗口（`before=0` 保持旧的前向行为），`limit` 1–1000（默认 200），支持 `event_type`、`actor_session_id`、`task_id` 过滤；响应含 `has_older` / `has_newer` 续页标志。`after >= before`（同时提供时）返回结构化错误。事件本身仍只追加、不改写。

### 数据库备份与回滚

数据库承载全部协作历史与审计。管理 Tab 的「数据库与备份」区块与管理端 REST 暴露产品级备份能力，底层复用 `backup_sqlite` / `backup_postgresql`：

- **位置可见性**：运行状态字段卡展示数据库类型、SQLite 绝对路径（PostgreSQL 显示脱敏 DSN 指向）、数据目录与日志路径，值全部来自运行时配置。
- **立即备份**：`POST /api/v1/admin/backups`（管理认证）把数据库快照写入 `<数据目录>/backups/`，返回备份文件绝对路径（界面可复制）；备份清单记录 schema 版本与事件游标，操作写入审计事件 `backup.created`。
- **从备份回滚**：`POST /api/v1/admin/backups/restore` 要求键入确认 `confirm="REPLACE"`（界面为二次确认弹窗）；安全校验包括 schema 版本一致（`backup_schema_mismatch`）、备份是否落后于当前最新写入（`backup_stale`，必须显式 `allow_data_loss=true` 接受丢弃较新数据）、数据库是否可写（`database_busy`）。拒绝与完成均写审计事件。回滚会丢弃备份之后的数据，请先确认没有任何 Agent 正在写入。
- **删除备份**：`DELETE /api/v1/admin/backups/{filename}`（管理认证）安全删除指定的数据库快照及其元数据清单（`.manifest.json`），Web 界面提供二次确认弹窗防误触。严格限制文件名必须符合 `backup-*.sqlite` 命名格式，禁止路径穿越或删除未受管快照，成功删除后写入审计事件 `backup.deleted`。
- **自动备份**：受验证配置 `[backup]`：`auto_backup_enabled`（默认关闭）、`auto_backup_interval_seconds`（默认 3600，最小 60）、`auto_backup_max_kept`（默认 10，超出自动清理最旧备份），环境变量 `AGENTCHATROOM_AUTO_BACKUP_*` 可覆盖。项目设置对话框（项目设置重设计任务）接线同一配置，无第二套事实来源。

### 项目文档：规范注入与回执闭环

每个 Project 可以维护版本化的「项目文档」，kind 分为两类：

- **规范（binding）**：必须遵循的工程规范。Agent 每次通过 `task_claim` 认领任务时，响应自动附带当前 binding 文档内容（受 `documents.inject_max_chars` 配置限制，默认 12000 字符，超出部分降级为摘要与按需获取指引）；服务端在 `task.claimed` 事件中记录本次认领适用的规范版本快照。
- **参考（reference）**：设计/架构文档，进入文档清单（manifest），按需获取，不强制注入。

回执闭环：`work_report` 时服务端从认领事件自动读取该快照并写入 `work.reported` 事件的 `spec_receipt` 字段（不依赖 Agent 自觉填写）；任务详情与验收界面按该版本对照判定合规。后端只负责注入、留痕与展示，不做合规性自动判定——独立验收才是执法环节。

- 存储：文档是 Room 数据（数据库表 `project_documents`，schema v19），每次编辑生成不可变新版本，当前版本为指针；历史版本可查、绝不改写或删除。文档增删改全部写审计事件（`document.created` / `document.updated` / `document.archived`），文档更新随 Room 事件流增量可见，不做全量内容推送。
- 读写权限：REST `GET /api/v1/projects/{id}/documents`（清单）与 `GET .../documents/{doc_key}`（含历史）为只读；创建新版本与归档（`POST .../documents`、`POST .../documents/{doc_key}/archive`）走管理认证，保留用于运维脚本场景。项目文档由 Agent 经 MCP 接口（`project_document_upsert`）进行不可变新版本维护，人工仅经 REST 脚本化操作，Web 管理端仅提供只读展示而不提供人工编辑界面。MCP 提供 `project_document_list`、`project_document_get`（按 kind/version 取全文）与 `project_document_upsert`；CLI 提供 `doc-list` / `doc-get`。规范注入与回执闭环保持不变。
- 三者分工：仓库 `AGENTS.md` 定义协作流程，`README.md` 是产品公共契约，项目文档承载**本项目**的架构与工程规范，互不同步复制。

### 项目设置：每个选项的语义与生效时机

项目设置对话框（Web「项目设置」）与 REST `PATCH /api/v1/projects/{id}` 复用同一受验证的项目设置模型（`normalize_project_settings`，未知键拒绝）：

| 选项 | 语义 | 什么时候会生效 | 对谁生效 |
| --- | --- | --- | --- |
| 项目名称 | Room 显示名 | 保存后立即 | Room 内所有页面与导出数据 |
| 租约冲突策略 | 提示并记录（advisory）/ 提交前阻断（pre_commit_block） | 保存后立即 | 本项目的文件租约提交前检查（申请租约时的冲突始终拒绝，与本设置无关） |
| 默认任务优先级 | 0–4；任务创建未显式指定优先级时采用（Web/MCP/CLI 一致） | 保存后立即 | 本项目后续新建任务 |
| MCP 消息注入条数 | 1–10（默认 5）；控制 MCP `room_bootstrap` 与后续 `room_sync` 初始上下文注入的最近有效 Agent 消息条数 | 保存后下一次 MCP 消息同步生效 | 本项目的 MCP Agent 对话上下文注入（仅过滤展示普通消息/决策/阻塞，审计与事件全量保留） |
| 团队约定 | 自由记录的协作约定清单（自动去重去空），**无控制作用** | 保存后立即对 Room 内所有成员可见 | 仅作可读记录；权限由成员权限与项目成员管理决定 |
| 审计历史保留策略 | 永久保留（默认）/ 保留 30 天 / 保留 90 天 | 保存后立即清理一次；此后随审计读取每小时至多清理一次 | 本项目的审计历史；清理只按时间整条删除到期事件，绝不改写或重排保留事件，清理动作本身写审计（`audit.purged`）；删除不可恢复 |
| 自动备份开关 / 备份保留份数 | 直接读写备份能力引入的 `[backup]` 配置（`GET/PUT /api/v1/admin/backup-settings`），无第二套配置 | 保存即写入配置文件 `[backup]`；自动备份调度在服务重启后的下一个周期按新配置执行 | 整个数据库的自动备份调度（全局，所有项目共用） |

### Web 界面设计令牌与排版基线

浏览器界面采用双主题（light/dark）设计令牌体系：字号六级刻度（`--fs-xs` 13px 至 `--fs-2xl` 24px）、行高（正文 `--lh-body` 1.6）、4px 基栅间距、四级字重、z-index 分层与过渡时长均以 CSS 变量为单一来源；十六进制颜色字面量只出现在 `:root` 两个主题块内。全库正文与表单文本 ≥13px、行高 ≥1.6，次要信息以颜色降层级而非缩小字号；空状态、加载态（`.loading-state`）、错误态（`.error-state`）、禁用态为全局统一组件。动效沿用既有缓动并尊重 `prefers-reduced-motion`。

## 知识资产（Knowledge Asset）

Room 中的沉淀知识以版本化 Knowledge Asset 保存，默认类型包括决策、流程、坑点、验证方式、偏好和参考资料。知识资产与任务执行相互独立：Agent 先提交候选版本，再由不同 Agent Identity 独立审核，未通过审核的知识不会进入已批准状态。

资产生命周期为 `candidate -> approved / rejected -> superseded -> archived`：

- 修改正文总是追加新版本并记录内容哈希，历史版本只读；普通操作不能覆盖或删除历史内容。
- 已批准资产不能被直接修改，必须先标记 `superseded`，再以新候选版本重新提交审核。
- `rejected` 资产可以修改后重新进入审核；`archived` 是终态。

每个版本记录可追溯来源：`source_type`（manual、task_result、import、extractor）、可选的 Task/Work Report/Review/Integration 引用、相关事件 ID，以及创建者 Session 和 Agent Identity。来源引用会规范化：只提供 Report/Review/Integration 而省略 Task 时，自动从被引用记录派生 Task 链接；`task_result` 来源必须直接或间接标识其 Task。默认配置下，关联 Task 的资产在批准前要求该 Task 已通过独立验证。

每个资产的 `kind` 在创建时固定，修订版本不能变更类型；需要换类型时创建新资产。

三端入口复用同一个领域服务：

- REST：`GET/POST /api/v1/projects/{id}/knowledge/assets`、`GET .../assets/{asset_id}`（支持 `version_id` 读取历史）、`POST .../assets/{asset_id}/reviews|supersede|archive`。
- MCP：`knowledge_candidate_submit`、`knowledge_review`、`knowledge_supersede`、`knowledge_archive`、`knowledge_get`、`knowledge_list`。
- CLI：`agentchatroom knowledge-submit | knowledge-review | knowledge-supersede | knowledge-archive | knowledge-list | knowledge-get`。CLI 审核通过 `--criterion "描述::passed"` 或 `--criterion "描述::failed"` 表达每项判定（省略后缀默认 passed）。

项目导出（REST `/export`、CLI `project-export`）包含全部知识资产的版本与审核历史。

默认配置见 `config.example.toml` 的 `[knowledge]` 节：

```toml
[knowledge]
kinds = ["decision", "procedure", "pitfall", "verification", "preference", "reference"]
require_verified_task = true
```

`kinds` 控制本部署启用的知识类型，可自定义但不可为空；`require_verified_task` 控制关联 Task 的资产是否必须先通过独立验证。两者都可通过环境变量覆盖。

当前批次不引入 LLM 抽取、Embedding 检索、Node 服务、额外容器、MemoryProxy 或自动 Prompt 注入；知识入库由 Agent 显式调用，检索按状态、类型和来源过滤。

## 服务器部署

服务器示例位于 `deploy/`：

- `deploy/compose.yaml`
- `deploy/Dockerfile`
- `deploy/config.server.example.toml`
- `deploy/.env.example`
- `deploy/Caddyfile.example`
- `deploy/serverctl.py`

初始化本机部署配置：

```powershell
.venv\Scripts\python.exe deploy\serverctl.py init
```

执行脱敏预检：

```powershell
.venv\Scripts\python.exe deploy\serverctl.py check
```

服务器档案要求管理认证、MCP 鉴权、PostgreSQL 和 HTTPS 外部地址。Compose 默认只把应用端口绑定到宿主机回环地址，避免绕过反向代理直接暴露管理接口。

容器操作、备份和恢复细节见 `deploy/README.md`。

## 开发与测试

完整测试：

```powershell
.venv\Scripts\python.exe -m pytest tests -p no:cacheprovider
```

pytest 的临时目录（`--basetemp` / `tmp_path`）必须放在本 checkout 之外。临时目录位于 checkout 内时，`room_bootstrap` 的工作区解析会从测试临时目录向上查找 `.agentchatroom/project.json` 并命中仓库自身的登记文件，导致 `test_bootstrap` / `test_project_registration` 中依赖空 checkout 作用域的用例误报 `registration_invalid`；conftest 会在检测到这种情况时输出中文提示。需要显式 `--basetemp` 时请指向仓库外的专用目录。

其他门禁：

```powershell
.venv\Scripts\python.exe -m compileall -q src\agentchatroom
node --check src\agentchatroom\web\app.js
.venv\Scripts\python.exe scripts\audit_public_release.py
git diff --check
```

本机验收脚本产生的临时数据库、日志和协议结果统一写入
`.agentchatroom/verification/`，不会散落到仓库源码目录或进入 Git。
PostgreSQL 验收脚本在未安装可选数据库依赖时仍可查看 `--help`；实际执行
需要安装 `postgresql` extra 和测试专用的 `pgserver` 包。GitHub Actions
会在 Windows 与 Ubuntu 的干净环境中运行完整基础测试，以避免本机已安装包
掩盖依赖或跨平台问题。

检查全部可达 Git 历史是否含旧的运行数据：

```powershell
.venv\Scripts\python.exe scripts\audit_public_release.py --history
```

审计器只输出规则、文件和行号，不回显疑似凭据值。

## 当前边界

- 一期发布与验收只覆盖同一台电脑上的本地 SQLite、REST、MCP、CLI 和 Web
  协作闭环。
- Streamable HTTP、远程 Bridge、PostgreSQL 适配器和服务器部署文件已经进入
  代码库，但属于后续跨机器阶段的基础能力，不代表跨机器协作已经验收通过。
- 版本化知识资产（提交、独立审核、状态转换、导出）已经进入主线；LLM 抽取与语义检索仍是后续批次。
- Web 的任务、文件占用、验证和管理能力仍有部分流程标记为未闭环。
- 外部 Agent、正式公网 TLS、生产 PostgreSQL、正式备份演练、代码同步和两台
  物理电脑互联均暂缓，不作为一期完成条件。
- 本机第三方 Agent 宿主是否长期保留 MCP 子进程取决于对应客户端生命周期；
  中心不会伪造永久在线。
- 自动选择第三方模型、读取隐藏推理或强制外部 Agent 遵守规则不在产品保证范围内。

## License

本项目使用 MIT License，详见 `LICENSE`。
