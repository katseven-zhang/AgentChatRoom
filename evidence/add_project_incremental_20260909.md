# 「已配置软件，加入本项目」零粘贴增量流程改造验收（2026-09-09）

执行：ZCode（GLM-5.3-Flash）。改造目标：用户选择「已配置软件，加入本项目」后不再被
“粘贴已有 agentchatroom HTTP 配置 / acrb.v1 凭据包”阻塞；签发后生成交给已配置 Agent
的增量接入提示词，由 Agent 在客户端本地自行合并。

## 改动文件

- `src/agentchatroom/web/index.html`：粘贴框移入默认折叠的 `<details id="token-existing-config-advanced">`「高级 · 故障恢复：手动粘贴已有配置合并（可选）」，textarea 永不必填；成员提示补充增量场景身份语义；资源版本 central48 → central49。
- `src/agentchatroom/web/app.js`：
  - add_project 提交不再要求粘贴文本（删除旧必填拦截与 `.required = isAddProject`）；
  - 空文本签发走 `incremental: true` 分支 → `incrementalHttpPrompt()`（仅 add_project；first_setup 空文本仍生成完整配置，浏览器验收中发现并修复的分支缺陷）；
  - 高级区粘贴旧配置时保留原合并路径（解析凭据包 + 身份校验 + 完整合并配置）；
  - 结果弹窗/guide 三场景文案与按钮更新（增量提示词 / 完整配置 / 恢复提示词）。
- `src/agentchatroom/integrations.py`：`build_onboarding_prompt` 的 add_project HTTP 场景指令改写为零粘贴增量合并说明；migrate_http 提示词同步移除粘贴要求。
- `README.md`：接入场景选择、浏览器基本使用、HTTP MCP、绑定语义四处章节同步。
- `tests/http_onboarding_config.cjs`：新增 `incrementalHttpPrompt` 16 项内容断言（目标 Project、新 Token 映射、MCP 名称、自查本地配置、保留 URL/身份/旧凭据、acrb.v1 合并方法、写回同一条目、重载 + 独立 Session + bootstrap 核对、失败恢复、禁止第二个连接器、Token 保密、无完整 bundle 泄漏）。
- `tests/test_web_assets.py`：新增 `test_web_add_project_issues_without_pasting_raw_config`（高级区折叠非必填、拦截提示移除、增量分支仅限 add_project、三场景可区分）；资源版本断言 central49。
- `tests/test_integrations.py`：新增 `test_add_project_http_prompt_drives_incremental_agent_side_merge`。

## 门禁证据

- 完整 pytest：**610 passed, 2 skipped**（2 项为既有 skip），`--basetemp` 在 checkout 外。
- `node --check app.js`、`tests/onboarding_modes.cjs`、`tests/http_onboarding_config.cjs`、`python -m compileall src tests`、`git diff --check` 全部通过。
- 定向 JUnit：`evidence/add_project_incremental_20260909_tests.xml`（integrations/web_assets/http_primary/services）。
- 源码 CMD 重启加载新代码（`.agentchatroom/runtime/start-source-8765.cmd`，127.0.0.1:8765 health=200），未构建 EXE、未清空数据库。

## 浏览器走查（真实 IAB 浏览器，1440×900，截图见 evidence/）

1. **加入本项目（重点）**：场景引导显示「签发本项目 Token 并生成增量提示词」＋“无需粘贴现有配置”
   （task_add_project_guide_central49.png）。签发弹窗粘贴区默认折叠、非必填、留空可签发
   （task_add_project_token_dialog_empty_paste.png）。空文本直接签发成功，生成增量提示词，
   16 项内容要素全部命中（task_add_project_incremental_prompt.png）。
2. **高级 · 故障恢复**：展开折叠区粘贴含合法 acrb.v1 包与软件身份的旧配置后签发，仍生成
   保留旧 Project（ProjectA）+ 身份（recovery-key-1）的完整合并配置。伪造非法 bundle 被
   解码器明确拒绝（防呆有效）。
3. **首次配置软件**：无高级粘贴区；空文本签发生成完整 HTTP 接入提示词（含真实 bundle、
   软件身份、bootstrap 指令、场景标注「首次配置软件」）。首轮验收发现 first_setup 空文本
   误入增量分支的缺陷，已修复并加回归断言后复测通过。
4. **恢复当前项目连接**：不打开签发弹窗、不签发 Token，直接生成不含 Token 的恢复提示词，
   4 秒观察期内状态稳定（task_reconnect_prompt_central49.png）。
5. **清理**：本次测试签发的 4 个 Token 全部经管理 Tab 吊销（credential_26f2311b / 8c0e35f18 /
   286a00f0c / 1064334c6）；此前轮次遗留的 credential_04a34efd 与 Codex Token 未改动。

## 兼容性确认

- 新数据库不关联成员可签发（成员下拉默认「不关联；首次连接时自动创建成员」），首次连接自动登记成员（#5299 规则，服务端测试未回归）。
- 旧 Project Token 已关联身份 + 新 Project Token 未关联的凭据包组合仍被服务端接受（#5302 规则）；增量流程默认不关联即产生该组合。
- 选择已有成员时仅在高级恢复路径做页面侧身份一致性校验；纯增量路径由服务端在连接时校验身份，弹窗提示语已写明「与所选成员不一致会被拒绝」。

## 风险与边界

- 增量提示词要求 Agent 解码/编码 acrb.v1（base64url JSON）。标准 MCP 客户端 Agent 可机械完成；若客户端本地 Authorization 是旧的单 `acr.*` Token（无包前缀），提示词明确指示停止并转高级恢复，不会盲改。
- 页面侧无法校验客户端本地身份与所选成员一致（无粘贴即无凭据可解析）；不一致时首次连接被服务端精确错误拒绝，恢复动作是改用「不关联」重签或高级恢复粘贴合并。
- 提示词正文含新 Project Token 明文，仅存在于一次性结果弹窗，关闭即清除，不进 Room/日志/仓库；已验证提示词中不出现完整合并 bundle。
- 静态资源版本已升 central49，旧页面缓存不会混用新旧前后端。
