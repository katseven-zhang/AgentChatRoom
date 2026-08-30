# AgentChatRoom

AgentChatRoom 是一个面向异构 AI 编程 Agent 的项目级实时协作中心。它让 Codex、WorkBuddy、Grok Build、Trae 以及其他支持标准 MCP 的客户端，在同一 Project/Room 中交换消息、领取任务、声明文件占用、提交工作证据，并由独立 Agent 完成验证和最终集成。

当前版本提供 Python 后端、浏览器管理端、REST、SSE、MCP stdio、Streamable HTTP MCP、远程 stdio Bridge、CLI、SQLite 本地档案和 PostgreSQL 服务器适配器。

## 2026-08-30 修复更新

本次修复版包版本为 `0.2.2`，重点收口 Project/Room 生命周期、软件身份和单机协作的事实来源：

- Project key 改由后端从 `project_id` 派生；Agent、REST、CLI 和 Web 不再自定义或猜测 key。
- Git 或本地路径来源由后端依据实际 checkout 自动检测；同一规范化作用域只允许一个活动 Project/Room。
- `.agentchatroom/project.json` 由后端维护并作为 checkout 注册；永久删除后的孤儿登记不会复活旧 Project。
- 归档保留 `archived_at` 和追加式历史；永久删除物理清除 Project 及级联 Room 数据，不保留 tombstone。
- Presence 只表示连接存活；任务认领、Work Report、独立 Review 和 Integration 才是工作进展事实，Work Report 会释放本任务 Lease。
- 左侧 Agent 现在表示本机安装的软件身份，而不是任务角色或临时名称。身份由 MCP 配置注入并由后端生成数据库标识；同一软件在同一 Project 中只保留一个身份和一个活动 Session。
- 同一软件重连会关闭旧 Session，并把未完成任务、有效 Lease、待响应指派和待处理 Handoff 原子转给新 Session；`executor`、`reviewer`、`coordinator` 只是 Session/Task 角色。
- 独立 Review 按软件身份判断。Codex 改名为 `Codex Review`、`Runtime Check` 或启动子任务仍然是 Codex，不能审核 Codex 自己完成的工作。
- 取消任务在 Web 中显示“已取消 / 无需验证 / 无需集成”，并从总览“正在进行”列表排除。
- Knowledge Asset Batch A 已完成版本化、独立审核、来源追溯和 REST/MCP/CLI 统一适配；外部 Agent、跨机器协作、服务器部署和代码同步仍不属于一期范围。

本次更新已通过完整测试、Python/JavaScript 语法检查、公开发布审计、差异检查和本地浏览器回归；运行时数据、凭据和本地 `docs/` 资料不纳入版本提交。

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
4. CMD 窗口持续显示后端日志；关闭窗口会停止前台服务。

需要清理异常退出后残留的后台进程时，双击 `关闭 AgentChatRoom.cmd`。

默认地址：

```text
http://127.0.0.1:8765
```

启动脚本通过自身位置推导仓库根目录，所以仓库可以放在任意盘符、任意父目录，也可以改名。

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
2. Project 根目录填写需要协作的代码工作区；这是运行数据库中的本机数据，不会写入仓库配置。
3. 点击“接入 Agent”，选择客户端和连接方式。
4. 复制页面生成的一段接入提示词，粘贴给对应 Agent。
5. 本机 stdio MCP 进程加载完整软件身份和 checkout 路径配置后，会自动加入已登记 Room 并出现在左侧；Agent 开始工作前仍调用 `room_join` 和 `room_sync` 获取本次运行凭据并同步事实。
6. 在 Room 动态中查看消息、模型标签、任务进展、文件占用、验证结果和事件顺序。

一个本机 Agent 软件安装在一个 Project 中只对应一个持久软件身份。Codex、Trae、WorkBuddy、Grok Build 等客户端的本机 stdio MCP 配置通过 `AGENTCHATROOM_SOFTWARE_KEY`、`AGENTCHATROOM_SOFTWARE_NAME` 和 `AGENTCHATROOM_SOFTWARE_CLIENT` 注入身份，并通过 `AGENTCHATROOM_PROJECT_PATH` 指向当前 checkout。四项配置完整且 checkout 已登记时，MCP 进程启动即自动建立 Presence；缺少配置时不会根据模型参数猜测身份，也不会自动创建 Room。模型不得按任务、角色、审核或运行检查临时改名。数据库 `agent_key`/`member_id` 由后端生成，不由 Agent 填写。每次连接仍保留新的 Session 审计记录，但同一软件同时最多一个活动 Session。

Project 的创建、归档、永久删除和 Agent 接入使用不同语义：代码项目作用域还
没有 Room 时，第一个 Agent 的 `room_join` 可以请求后端创建它，Web 管理端、REST 和
CLI 也可以显式创建；作用域已经存在活动 Room 后，其他 Agent只能加入，不能
另建。归档保留完整 Room 数据并设置 `archived_at`，Agent不能绕过归档另建；
永久删除会物理删除 Project 及其级联 Room 数据，不保留删除标记，删除后作用
域重新为空，后续第一个 Agent可以创建新的 Room。

Project key 是后端生成的无语义外部查询键，默认不在 UI 展示，也不接受 Agent、
REST、CLI 或 Web 自定义。Agent 只提供实际 `project_path`；后端自动检测工作区
识别方式：存在有效 Git origin 时使用规范化 Git remote，否则使用规范化本地
路径。Agent 的 `room_join` 不接受 `logical_path`，不能通过改写作用域参数另建
Room。用户划分单仓库子项目时，应把实际子目录作为 `project_path`/`root_path`
交给 Web、REST 或 CLI；后端根据它相对 Git 根目录的位置生成 `logical_path`。
任何显式传入值只能与后端派生结果一致，不能使用绝对路径、`..` 或虚构目录
改写 Project 身份。

一个规范化代码项目作用域对应一个活动 Project/Room。同一 Git remote 与
`logical_path`，或同一本地规范化路径与 `logical_path`，不能创建第二个活动
Room。`room_join` 会从忽略的 `.agentchatroom/project.json` 读取后端登记并自动
加入该作用域唯一的活动 Room；如果登记仍指向已永久删除的 Project，只返回失效
登记错误，不会用旧 key 复活。只有本地登记和数据库作用域都为空时才创建新 Room。

## MCP 接入

生成通用本机 stdio JSON：

```powershell
.venv\Scripts\agentchatroom.exe mcp-config --format generic-json --transport local-stdio
```

生成特定客户端格式：

```powershell
.venv\Scripts\agentchatroom.exe mcp-config --format workbuddy-json --transport local-stdio
.venv\Scripts\agentchatroom.exe mcp-config --format grok-toml --transport local-stdio
.venv\Scripts\agentchatroom.exe mcp-config --format codex-toml --transport local-stdio
```

支持的连接方式：

- `local-stdio`：Agent 与中心在同一台电脑，共享同一个 `.agentchatroom/runtime`。
- `streamable-http`：客户端直接连接中心 `/mcp`，需要 Agent Token。
- `remote-bridge`：客户端只支持 stdio 时，由本机 Bridge 转发到远程中心。

远程 Token 只放在客户端安全配置或环境变量中，不写入 README、项目规则、日志、消息正文或 Git。

## 标准协作流程

```text
room_join -> room_sync
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

后台 MCP/Bridge 进程只负责连接 Presence。`session_heartbeat` 只刷新连接存活，
不再依赖 Agent 主动提交 `working`、`idle` 或 `blocked`。左侧显示已连接/未连接、
当前任务阶段和最后活动；任务认领、Work Report、独立 Review 与 Integration 才是
工作进展的事实来源。Work Report 会自动释放该任务的文件 Lease。
左侧只展示已注册的软件身份；旧版本中没有绑定持久软件身份的临时验证 Session
仍保留在审计记录中，但不再作为 `Runtime Check`、`Codex Review` 等独立 Agent 展示。
任务被取消后即为终止状态，不再进入独立验证或最终集成；Web 会明确显示
“已取消 / 无需验证 / 无需集成”，避免把取消任务误解为仍有待办。

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
