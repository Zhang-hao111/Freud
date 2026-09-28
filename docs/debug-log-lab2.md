# Lab2 调试记录 — Coding Agent（freud）

- **日期**：2026-09-28
- **调试对象**：`/home/h/h/大数据分析/Lab-coding-agent`（实验一交付的 Python coding agent，CLI 命令 `freud`）
- **基准版本**：git commit `2489d5c`（feat: init Freud — AI 编程智能体），工作区无未提交改动
- **运行环境**：Linux x64，项目自带 `.venv`（Python 3.11），入口 `main.py` / `agent/cli.py`

## 问题清单（调试前通读代码定位）

| 编号 | 级别 | 位置 | 问题 |
| --- | --- | --- | --- |
| #1 | P0 功能 | `agent/cli.py` REPL `set` 分支 | `/key`、`/model` 等设置只写盘，重建 LLM Provider 时用的是启动时加载的旧内存配置，新配置当次会话不生效 |
| #2 | P0 功能 | `agent/core.py` deny / escalate-denied / 工具异常路径 | assistant 消息带 `tool_calls` 后追加的是 `UserMessage` 而非带 `tool_call_id` 的 `ToolMessage`，违反 OpenAI 工具调用协议，下一次 LLM 调用必被 400 拒绝；`agent/llm.py` MockLLM 生成的 assistant 消息 id 与 Action 的 `tool_call_id` 不一致，同类问题 |
| #3 | P0 安全 | `agent/cli.py` REPL 聊天循环 | 完全没有调用 guardrail，危险 shell 命令可直接执行；`take_note` 只回复"已记录"，从不写入 memory；`/run <file>` 不读文件内容，只把路径字符串发给 LLM |
| #4 | P1 配置 | `config.json` | `model` 为 `gpt-4o`，`api_base` 为 `https://api.deepseek.com`，真实调用必报"模型不存在" |
| #5 | P2 健壮性 | `agent/memory.py` `consolidate()` | `memory_path` 无目录前缀（裸文件名）时 `os.makedirs('')` 抛异常 |
| #6 | P2 文档 | `CLAUDE.md` / `README.md` | 描述的 `task/` 目录、`docs/实验要求.md`、`.env` 配置方式与实际代码不符（本次不修，仅记录） |
| #7 | P0 功能 | `agent/cli.py` repl 聊天循环 | `_print_tool_call` / `_print_result` 定义在 `core.py` 但 cli.py 未导入，REPL 中 LLM 一发起工具调用即 `NameError` 崩溃（步骤 4 复现时新发现） |

## 调试步骤

### 步骤 1：修复 REPL 设置不生效（问题 #1）

**根因**：`repl()` 启动时 `config = load_config()` 只加载一次；`set` 分支调用 `save_config()` 把新值写入 config.json 落盘，但随后用**旧的内存 `config` 字典**重建 `OpenAIProvider`，新 key/model/base-url 在当次会话永远用不上。

**复现**（探针脚本 `/tmp/verify_set_key.py`，monkeypatch `OpenAIProvider.__init__` 记录构造参数，通过管道模拟输入 `/key = sk-TESTNEWKEY123`）：

```
修复前输出：
== Provider 构造记录 ==
  第1次: api_key=sk-6aea...(旧key), model=gpt-4o, ...
  第2次: api_key=sk-6aea...(旧key), model=gpt-4o, ...   ← set 之后仍用旧 key
== 结果: FAIL — set 之后 Provider 仍使用旧 key（bug 复现）==
```

同时复现出附带问题：确认消息把完整 API key 明文打印到终端。

**修改**（`agent/cli.py`）：

1. `set` 分支在 `save_config()` 之后增加 `config[cfg_key] = cfg_value`，同步内存配置；
2. 新增 `_mask_secret()` 辅助函数，`set` 确认消息与 `_show_config()` 统一用脱敏形式展示 key。

**验证**：重跑探针脚本 —

```
== Provider 构造记录 ==
  第1次: api_key=sk-TESTNEWKEY123, ...
  第2次: api_key=sk-TESTNEWKEY123, ...                 ← 新 key 已生效
== 结果: PASS — 新 key 已生效 ==
确认消息：✓ 已更新 API Key = sk-TES******Y123          ← 已脱敏
```

（测试会写 config.json，跑完已从备份 `/tmp/config.json.bak.lab2` 还原。）

### 步骤 2：建立回归测试套件，复现协议破坏问题（问题 #2、#5）

**方法**：先写测试再修复（TDD）。新增 `tests/` 目录，4 个文件、18 个用例：

| 文件 | 覆盖内容 |
| --- | --- |
| `tests/test_harness_protocol.py` | 核心：`ProtocolCheckingLLM` 包装 MockLLM，每次调用前校验"assistant 的每个 `tool_call_id` 都必须被对应 `ToolMessage` 响应"，即真实 API 强制校验的协议不变量 |
| `tests/test_harness_guardrail.py` | 危险模式三态分类（allow/deny/escalate） |
| `tests/test_harness_memory.py` | FileMemory 读写、持久化、裸文件名 |
| `tests/test_harness_cli_intent.py` | REPL 自然语言/斜杠命令解析 |

运行：`.venv/bin/python -m unittest discover -s tests`（项目根目录）。

**修复前结果：`Ran 18 tests → FAILED (failures=4, errors=1)`**，4 个协议测试全部失败，证明 bug 真实存在：

- `test_deny_keeps_protocol_valid` — fork 炸弹被 deny 后 `t1` 无人响应；
- `test_escalate_denied_keeps_protocol_valid` — `rm -rf /` escalate 被拒后同样；
- `test_unknown_tool_exception_keeps_protocol_valid` — 未知工具抛 ValueError 后同样；
- `test_normal_tool_flow_unchanged` — **连正常流程都失败**：MockLLM 生成的 assistant 消息里 tool_call id 是自造的 `mock_1`，而 Action 的 `tool_call_id` 是 `None`，回灌的 ToolMessage 带不上正确的 id —— 即使用真实 LLM，只要走到 MockLLM 路径会话就是坏的；这暴露了 `llm.py` 的第二个 bug。
- `ERROR: test_bare_filename_consolidate` — 即问题 #5，`os.makedirs('')` 抛异常。

**根因**（两处）：

1. `agent/core.py` 三个分支（deny、escalate 被拒、工具执行抛异常）在 assistant 消息带 `tool_calls` 时追加了 `UserMessage` 而非 `ToolMessage(tool_call_id=...)`。OpenAI/DeepSeek 协议要求 tool_calls 后必须紧跟对应 tool 响应，否则下次调用直接 400 —— 表现为"护栏一旦拦截，agent 就反复报 LLM 调用失败直到耗尽步数"。
2. `agent/llm.py` `MockLLM.chat` 构造 assistant 消息时 id 用 `f'mock_{n}'`，忽略 `r.tool_call_id`，与回灌的 ToolMessage 对不上。

**修改**：

- `agent/core.py`：上述三个分支改为 `if action.tool_call_id: append(ToolMessage(...)) else: append(UserMessage(...))`；
- `agent/llm.py`：MockLLM 的 tool_call id 改为 `r.tool_call_id or f'mock_{self.call_count}'`。

**验证**：重跑测试，协议相关 4 项全部转绿（此时仅剩 memory 一项 ERROR，见步骤 3）。

### 步骤 3：修复 memory 裸文件名崩溃（问题 #5）

**根因**：`FileMemory.consolidate()` 无条件 `os.makedirs(os.path.dirname(path))`，当 `memory_path` 是裸文件名时 `dirname` 为空字符串，`makedirs('')` 抛 `FileNotFoundError`。

**修改**：`agent/memory.py` 中先取 `parent = os.path.dirname(self.file_path)`，非空才 `makedirs`。

**验证**：`Ran 18 tests → OK`，18 个测试全部通过。

### 步骤 4：修复 REPL 聊天模式（问题 #3，复现时新发现 #7）

**复现**（探针脚本 `/tmp/verify_repl.py`，monkeypatch 假 LLM 驱动 repl()，三个用例）：

```
修复前：
  Case A（guardrail）：直接崩溃 —— NameError: name '_print_tool_call' is not defined
  Case B（take_note）：take_note 写盘: FAIL（memory 文件未写入）
  Case C（/run）：LLM 收到的任务内容: '/tmp/task_hello.md' → FAIL（只收到路径字符串）
```

**根因**（四处）：

1. **（新发现，问题 #7）** repl 聊天循环调用 `_print_tool_call` / `_print_result`，二者定义在 `agent/core.py`，`cli.py` 未导入 —— REPL 中 LLM 一发起工具调用就 `NameError` 崩溃，交互模式的工具功能从未可用过。
2. repl 聊天循环执行工具前完全没有调用 `guardrail()`，危险命令直接执行，与设计文档"拦截逻辑是代码而非提示词"矛盾。
3. `take_note` 分支只拼接回复文本，从不调用 `memory.write()`（repl 也没创建 memory 实例）。
4. `/run <file>` 只把路径字符串 `intent.get('file')` 作为聊天文本发给 LLM。

**修改**（`agent/cli.py`）：

1. 导入 `from agent.core import run_agent, _print_tool_call, _print_result` 和 `from agent.guardrail import guardrail`；
2. repl 初始化时创建 `memory = FileMemory(config['memory_path'])`；
3. call_tool 分支：mock 检查之后插入与 batch 模式一致的 guardrail 检查 —— deny 直接拦截；escalate 通过 `input()` 询问用户 `[y/N]`，拒绝时以 `ToolMessage` 把拒绝原因回灌给 LLM（协议正确）；
4. take_note 分支：`memory.write(key, value)` + 立即 `memory.consolidate()`，并打印确认；
5. run/chat 分支：`/run` 用 `Path(...).read_text()` 读取任务文件内容后再发给 LLM，文件不存在时提示并继续。

**验证**：重跑探针脚本 —

```
Case A：→ shell("fdisk -l") → ⚡ 修改分区表，需要人工确认。批准执行? [y/N] → 输入 n
        → ⛔ ESCALATE DENIED（命令未执行）
        LLM 收到 user 消息序列: ['hi', '操作被拒绝: 修改分区表']   ← 拒绝原因已回灌
Case B：📝 lab2_note = hello-note → take_note 写盘: PASS
Case C：LLM 收到的任务内容: '实现一个函数计算两数之和' → PASS
```

### 步骤 5：修正 config.json 模型配置（问题 #4）

**根因**：`model` 为 `gpt-4o` 而 `api_base` 为 `https://api.deepseek.com`，DeepSeek 不提供 gpt-4o，真实调用必报"Model Not Exist"。

**修改**：`config.json` 中 `"model": "gpt-4o"` → `"deepseek-chat"`。

**验证**：`load_config()` 读出 model=deepseek-chat、api_base=https://api.deepseek.com，匹配检查 PASS。

## 修改文件清单

| 文件 | 改动 |
| --- | --- |
| `agent/cli.py` | 步骤 1、4（+67/-15 行中的主要部分）：set 同步内存配置、key 脱敏、导入打印函数与 guardrail、repl 创建 memory、聊天循环加护栏与 escalate 人工确认、take_note 写盘、/run 读文件 |
| `agent/core.py` | 步骤 2：deny / escalate-denied / 工具异常三处改为按 `tool_call_id` 回灌 ToolMessage |
| `agent/llm.py` | 步骤 2：MockLLM 的 tool_call id 与 `Action.tool_call_id` 保持一致 |
| `agent/memory.py` | 步骤 3：consolidate 兼容裸文件名路径 |
| `config.json` | 步骤 5：model 改为 deepseek-chat |
| `tests/`（新增 4 文件） | 18 个回归/单元测试 |
| `docs/debug-log-lab2.md` | 本文档 |

## 最终验证

```
$ .venv/bin/python -m unittest discover -s tests
Ran 18 tests in 0.004s — OK

$ .venv/bin/python main.py --file /tmp/mock_task.md --mock
  🎯 Mock completed        ← batch 模式主流程不受影响
```

## 遗留问题与建议（本次未修）

1. `run()`（batch 模式）没有注入 approver，escalate 一律拒绝并回灌原因让 LLM 换方法 —— 属安全默认，如需人工确认可加控制台 approver。
2. repl 聊天模式仍未接入 tracer（批量模式有完整 trace），如需观测可在 repl 中加 Tracer 并在退出时 flush。
3. `llm.py` 的 `chat()` 只取 LLM 返回的第一个 tool call，其余丢弃 —— 功能限制而非 bug。
4. 文档滞后（问题 #6）：`CLAUDE.md` 引用的 `task/` 目录与 `docs/实验要求.md` 已不存在；README 的配置说明（`.env`）与实际（config.json）不一致，建议提交前统一更新。
5. `config.json` 含明文 API key，所幸已在 `.gitignore` 中（不会进 git）；仍建议改走环境变量，并注意不要在演示/录屏中暴露该 key。

---

# 阶段二：功能完善（"把整个 agent 做好"，2026-09-28 下午）

应用户要求暂停 Hadoop 环境搭建，先把 agent 本体补齐到设计文档承诺的完整形态。以下按实施顺序记录。

## 步骤 6：LLM 调用重试（补齐设计 §13）

**问题**：设计文档 §13 承诺"LLM API 调用失败 → 指数退避重试最多 3 次"，实际 `OpenAIProvider.chat` 单次调用失败直接抛出。

**修改**（`agent/llm.py`）：`chat()` 包上重试循环——最多 `MAX_RETRIES=3` 次尝试，失败后打印告警并退避 1s/2s 重试，最终失败向上抛出（由 core 循环兜底处理）。

**验证**：`tests/test_harness_features.py::RetryTest` —— 用可编程的假 client（前两次抛异常、第三次成功 / 始终失败）分别断言重试成功与重试耗尽后抛出、调用次数恰为 3。

## 步骤 7：并行 tool calls 支持（补齐设计 §4.4）

**问题**：`chat()` 只取第一个 tool call，assistant 消息也重写为只含这一个调用。现代模型经常一次发多个并行调用，全部被静默丢弃。

**修改**：

- `agent/types.py`：`LLMResponse` 改为 `actions: list[Action]`，保留 `action` 属性（返回首动作）兼容旧代码与 MockLLM；
- `agent/llm.py`：`OpenAIProvider.chat` 解析全部 tool calls；`MockLLM` 改用 `actions=[r]`；
- `agent/core.py`：`run_agent` 主循环改为 `for action in response.actions:` 内层循环，每个动作独立走 guardrail → 执行 → 回灌；`done` 立即终止；
- `agent/cli.py`：REPL 对话循环同样改为多动作处理（动作处理逻辑提取为 `_repl_handle_action()`）。

**验证**：`MultiToolCallTest.test_parallel_tool_calls_all_answered` —— 一次响应带两个 `read_file`，断言第二次 LLM 调用前两个调用都已被各自的 ToolMessage 响应且内容正确。

## 步骤 8：记忆写读闭环（补齐设计 §7）

**问题**：设计文档说 LLM"通过 `take_note` 写入，通过 read 方法检索"，但 `run_agent` 和 REPL 都从未把记忆注入上下文——记忆实际是只写不读的摆设。

**修改**：

- `agent/memory.py`：新增 `items()` 返回全部条目快照；
- `agent/core.py`：新增 `build_memory_block()`（最多 20 条、每条值截断 300 字符），`run_agent` 的系统提示与 REPL 首条系统消息都拼接该块。

**验证**：`MemoryInjectionTest` —— 预置一条记忆后运行 agent，断言 LLM 收到的系统提示包含该记忆的键与值。

## 步骤 9：工具输出截断

**问题**：`read_file` / `shell` 原样返回全部输出，一次 `cat` 大文件即可撑爆上下文。

**修改**（`agent/registry.py`）：新增 `MAX_TOOL_OUTPUT=20000` 与 `_clip()`，两个工具的返回数据超限时截断并附"原始 N 字符"说明。

**验证**：`TruncationTest` —— 30000 字符文件读取后长度小于原值且含"截断"字样。

## 步骤 10：batch 模式 escalate 审批 + REPL 可观测性（补齐设计 §6.3、§8）

**问题**：① batch 模式未注入 approver，escalate 一律拒绝（安全但不完整）；② REPL 每步无 trace 记录。

**修改**（`agent/cli.py`）：

- 新增 `--yes` 参数与 `_console_approver()`；`run()` 按 `--yes` → 自动批准、`sys.stdin.isatty()` → 控制台确认、非交互 → 拒绝（保持安全默认）的顺序注入 approver；
- REPL 初始化 `Tracer`，对话循环内每步 `tracer.record(step, action, result)`，外层 `try/finally` 保证退出时 `tracer.flush()`。

**验证**：`EscalateApproverTest` —— 批准路径命令实际执行且回灌 ToolMessage；拒绝路径命令未执行、"操作被拒绝: 原因"回灌（顺带把 core.py 的拒绝文案从裸原因统一为与 REPL 一致的完整句式）。

## 步骤 11：真实 LLM 端到端验收（DeepSeek）

**方法**：用 config.json 中的真实 key 跑 batch 任务（max-steps=12）：写 `tasks/e2e_greet.py` 实现 greet 函数 → shell 验证 → done。

**结果**：8 步全链路成功——

```
step1: shell   → 返回码 2（失败，触发反馈回路）
step2: shell   → ls 成功（换方法重试）
step3: write_file → tasks/e2e_greet.py
step4: shell   → 验证输出 '你好, 世界' OK
step5: write_file → tasks/test_e2e_greet.py（agent 自写的 unittest）
step6: shell   → Ran 3 tests OK
step7: take_note → 记录产物位置
step8: done    → 任务完成 ✅
```

要点：step1 失败后 agent 自主换方法重试，证明反馈回路在真实 API 下工作；产物落盘、agent 生成的测试 3 用例全过（本地复跑确认）。

## 阶段二测试与文档

- 测试从 18 个增至 **26 个**，`Ran 26 tests → OK`；mock 冒烟与 REPL 探针三用例（护栏/take_note//run）复验通过。
- 文档同步：README（目录结构、配置说明、`--yes`、重试/截断说明）、CLAUDE.md（按当前实际重写，含"改消息处理必须跑协议测试"的约定）、design.md 末尾新增"§19 实现状态备注"。
- E2E 产物保留在 `tasks/e2e_greet.py`、`tasks/test_e2e_greet.py`，trace 在 `~/.agent-harness/traces/`。

## 阶段二后的遗留

1. `thinking-*.md` 思考过程导出（README 提到）尚未实现——tracer JSON 已可用，导出 markdown 属展示增强。
2. `edit_file` 工具仍未提供（设计文档标注 MVP 非必需，`write_file` 可覆盖）。
3. Hadoop 环境搭建与迭代一业务功能（清洗/评分/前端）未开始——待用户指示后推进。

---

# 阶段三：修复"全局 freud 命令打不开"（2026-09-28 晚）

**现象**：在项目目录外运行 `freud`，打印"[!] 错误：未设置 API Key"后立即退出。

**复现与根因**（三个因素叠加）：

1. 全局 `freud` 是 9/9 `uv tool install` 装的旧代码，不包含阶段一/二的所有修复；
2. 旧代码的 `_default_cfg_path()` 指向 cwd——某次在家目录运行 `freud` 时，`ensure_config()` 在 `~/config.json` 自动生成了**空的默认配置**（api_key 为空）；
3. 配置查找 cwd 优先，这个空配置从此把其他候选全部挡住，在 ~ 下运行必然找不到 key。且旧逻辑根本没有全局配置位置，装完的工具在任何新目录都无 key 可用。

**修改**（`agent/config.py`）：

- 候选路径改为三级：**cwd → 源码仓库根 → 全局 `~/.agent-harness/config.json`**；
- `_default_cfg_path()` 指向全局位置，`ensure_config()` 只在完全没有配置时创建全局默认——cwd 不再被自动写入垃圾配置；
- 全局安装刷新：`uv tool install --reinstall .`；项目配置同步复制到 `~/.agent-harness/config.json`；
- 清理：删除家目录的空 `~/config.json`（已备份 `/tmp/home-config.json.bak`）。

**验证**：项目目录内/外运行 `freud` 均正常进入 REPL；batch mock 正常；26 个测试全绿。

---

# 阶段四：交互界面重设计（feature/repl-ui 分支，2026-09-28 晚）

应用户要求新建 `feature/repl-ui` 分支，参考 Claude Code 的终端风格重排交互输出。

## 设计要点

1. **行式语言**：工具调用行 `⏺ Bash(ls -la)`（橙色圆点 + 加粗工具名 + 参数预览），结果行 `⎿ 输出摘要`（折叠空白、超 200 字符截断并附 `…(+N 字符)`）；错误结果红色，正常结果暗色。
2. **工具显示名**：read_file→Read、write_file→Write、shell→Bash、done→Done、take_note→Note。
3. **颜色自动降级**：仅 TTY 且未设置 `NO_COLOR`/`TERM=dumb` 时启用 ANSI；管道/重定向输出为纯文本（日志、测试、演示录屏干净）。橙色点支持真彩色（`COLORTERM`）与 256 色降级。
4. **中文等宽对齐**：`visual_width()` 按东亚宽度计数（全角 2 列），`pad()` 宽度感知补齐；盒线/帮助表头在 CJK 终端下真正对齐。经验教训：`—`、`·` 属宽度歧义字符（ambiguous width），对齐敏感位置必须避开。
5. **横幅与帮助**：圆角盒线横幅（含 Mock 模式提示行）；`/help` 命令列按显示宽度对齐、说明文字暗色；`/config` 标签暗色对齐。

## 修改文件

| 文件 | 改动 |
| --- | --- |
| `agent/ui.py`（新增） | 颜色检测/着色、橙点渲染、显示宽度对齐、工具行/结果行渲染器、工具显示名映射 |
| `agent/core.py` | `_print_tool_call`/`_print_result` 改用渲染器；deny/escalate/异常/Done/Note/最大步数各行重排；删除失效的 `_truncate` |
| `agent/cli.py` | 横幅 `_print_banner`（圆角盒线）、`_show_help`/`_show_config` 重排、set 确认绿色 ✓、REPL 护栏/审批/Note 行统一新风格 |
| `tests/test_harness_ui.py`（新增） | 12 个用例：颜色开关/NO_COLOR/非 TTY 降级、显示宽度与 pad 对齐、渲染器（显示名映射、截断、空白折叠、错误标记） |

## 验证

- 全量测试 `Ran 41 tests → OK`（29 → 41）。
- 盒线对齐经 `cat -A` 字节级核对，各带边框行右边框位置一致。
- REPL 探针复验：`⏺ Bash(fdisk -l)` → 人工确认 → `⎿ 操作被拒绝`（协议不变量不受影响）；`⏺ Note(key)` / `⎿ 值`；/run 正常。
- batch mock 流程输出正常。

## 启动页重设计（参照用户提供的真实 Claude Code 截图）

用户提供 Claude Code v2.1.283 截图后，横幅从盒线样式改为同款布局：

- 橙色像素吉祥物 **Ψ**（Psi，心理学符号，呼应 Freud 命名；真彩色/256 色降级）；
- `Freud  v0.1.0`（加粗名称 + 暗色版本号）；
- 状态行 `deepseek-chat · https://api.deepseek.com`（模型 · API 地址；Mock 模式显示 `mock-llm · 无需 API Key` 并附黄色提示行）；
- 工作目录行（暗色）；
- 末行暗色提示 `/help 查看命令，或直接输入自然语言`。

`agent/ui.py` 新增 `paint_orange()`；`cli.py` 的 `_print_banner` 重写并接收 config（避免重复加载）。与原 Claude Code 的差异：其输入框占位符（`Try "..."`）与终端底部固定状态栏需要全屏 TUI（raw mode/curses）实现，当前基于 `input()` 的 REPL 不做，保持普通终端可用性。

验证：41 个测试全绿；普通/Mock 两种模式启动页正确；全局 `freud`（editable 安装）实时生效。

### 迭代二：布局与配色调整（用户看真机截图后反馈）

1. **左图右文布局**：文字块（名称/状态/目录/提示）从 logo 下方移到右侧，与 Claude Code 启动页一致；`visual_width` 逐行补齐。
2. **吉祥物缩小**：Ψ 从 6 行 16 列缩到 5 行 9 列。
3. **主题色换粉色**：`paint_orange` → `paint_accent`（保留别名），真彩色 RGB(255,105,180)，256 色兜底 205。
4. **输入框**：REPL 每轮提示符上下各画一条暗色横线（宽度取 `shutil.get_terminal_size()`），模拟 Claude Code 的输入区边框。占位符仍不可行（`input()` 限制，见上）。
5. **关键修复——歧义宽度随 locale**：用户终端为中文 locale，`█` 按 2 列渲染（此前图标"显得巨大"且对齐会错的根源）。`visual_width` 现按 `LC_ALL/LC_CTYPE/LANG` 是否 zh/ja/ko 决定歧义宽度字符口径（宽/窄），两种 locale 下文字列起始列断言一致（均为第 23 列，PASS）。
6. **附带修掉的测试 bug**：缓存变量与函数同名 `_ambig_wide`，测试重置时把函数覆盖成 None 导致 8 个用例报 `NoneType is not callable`；缓存改名 `_ambig_wide_cache`。

验证：46 个测试全绿；PTY 下 256 色（`38;5;205`）与真彩色（`38;2;255;105;180`）粉色均正常输出。

### 热修：真终端启动崩溃（NameError: _wrap）

**现象**：用户真终端运行 `freud` 即崩，`paint_orange` 引用未定义的 `_wrap`。

**根因**：`_wrap` 只被 `dot()`/`paint_orange()` 使用且从未定义；此前所有验证均在管道下进行（颜色关闭提前 return），启用颜色的代码路径从未被执行——**管道测试掩盖了 TTY-only 的 bug**。另查明本沙箱 `TERM=dumb`，导致 PTY 模拟验证时颜色仍关闭，需要显式 `TERM=xterm-256color` 才能复现真实终端行为。

**修复**：补上 `_wrap()` 并让 `paint()` 复用；新增 `test_enabled_path_all_renderers` 强制开启颜色遍历全部渲染器（29→42 用例中的一部分）。

**验证**：42 个测试全绿；`script` PTY + `TERM=xterm-256color` 下 256 色橙输出正常，`COLORTERM=truecolor` 下真彩橙（RGB 217,119,87）输出正常。

---

# 阶段五：会话化记忆管理（2026-09-28 晚）

用户需求：新会话不携带任何历史记忆；每个会话独立保存自己的记忆；`/resume` 参照 Claude Code 找回之前的会话。

## 设计

- **会话文件**：每个会话一个 JSON，存 `~/.agent-harness/sessions/session-<时间戳>-<rand>.json`，内容含 `id / title / created_at / updated_at / workspace / memory[]（take_note 条目）/ messages[]（完整消息历史）`。
- **新会话零记忆**：repl 启动创建全新会话，`build_memory_block` 注入的只是当前会话的记忆（初始为空）——历史 `take_note` 不再自动出现在新会话。
- **/resume 恢复**：按最近更新倒序列出会话（标题 · N 前 · 笔记数 · 工作目录），排除当前会话；选中后整体替换当前记忆与消息历史，对话无缝续跑。`freud --resume` 启动时直接进入选择器。
- **batch 模式**：每次 `--file` 运行也是一个独立会话（标题取 `--name`）。
- **done 的协议补全**：done 也是 tool call，结束前回灌 ToolMessage——否则恢复"停在 done"的会话后下一轮 API 400。
- **遗留数据迁移**：首次初始化时若旧全局 `memory.json` 非空且无任何会话，自动导入为一个"旧版全局记忆（自动导入）"会话（本机两条旧笔记 task1_two_sum、e2e_greet 已迁入）。

## 修改文件

| 文件 | 改动 |
| --- | --- |
| `agent/session.py`（新增） | Session（落盘/标题/记忆/消息序列化）、SessionStore（列表排序、遗留导入）、SessionMemory（FileMemory 同接口，含 live_messages 同步落盘）、time_ago |
| `agent/cli.py` | repl/run 接入 SessionMemory；`/resume` 意图（斜杠 + 自然语言）与 `_pick_session`；`--resume`；每轮对话结束落盘；done 回灌 |
| `agent/core.py` | done 路径补 ToolMessage 回灌 |
| `tests/test_harness_session.py`（新增） | 10 个用例：空会话、记忆落盘、消息往返、列表排序、标题首条、遗留导入一次性、time_ago、resume 意图解析 |

## 验证

- 全量测试 `Ran 56 tests → OK`（46 → 56）。
- 端到端（mock）：新会话落盘 → `/resume` 列出 3 个会话（含遗留导入）→ 选中恢复 → batch 模式独立会话计数递增。
- 全局 `memory.json` 退役（保留在磁盘上作为遗留来源，不再被读取）。

---

# 阶段六：终端交互升级（2026-09-28 深夜）

用户需求：`/resume` 用方向键选择；终端底部（输入框下方）加权限模式栏，Shift+Tab 循环切换——"每次改动都询问 / 允许编辑 / 允许所有"。

## 新增模块

| 文件 | 职责 |
| --- | --- |
| `agent/term.py` | ① raw-mode 行编辑器（光标移动、↑/↓ 输入历史、Tab 接受暗色提示、Backspace），footer 行紧贴输入行实时刷新；② 方向键选择器 `choose()`（❯ 标记、↑/↓ 循环、Esc 取消）；③ footer 渲染（左右分段、填充线按歧义宽度折算）。非 TTY 全部自动降级（input()/数字选择/纯文本），管道与测试不受影响 |
| `agent/permissions.py` | 三档权限模式：`ask`（默认，write_file 逐次确认，'a' 可当场切 accept）/ `accept`（允许编辑）/ `yolo`（写入与 escalate 全自动批准，deny 仍拒绝）；Shift+Tab 循环 ask → accept → yolo |

REPL 输入区新布局：顶部横线 + 输入行 + **footer（模式徽标 · shift+tab 提示 · 当前模型）**；footer 在每次按键重绘时同步刷新。

## 两个关键坑（都已写入测试/流程）

1. **select 与 TextIOWrapper 缓冲脱节**：`sys.stdin.read(1)` 会把整段输入预读进 Python 内部缓冲，之后 `select` 监视的 fd 恒为空，ESC 序列被拆成 'esc'/'['/'B' 三个键。修复：全部改用 `os.read(0,1)` 裸读 fd，多字节 UTF-8（中文输入）按首字节长度一次性取齐。
2. **`script` 工具在管道喂键 + cbreak 下转发不可靠**（按键迟迟不到达子进程）。改用 `pty.fork` 自制驱动器 `/tmp/pty_drive.py` 按时间表注入按键，作为 PTY 层回归验证工具。

## 验证

- 全量测试 `Ran 68 tests → OK`（56 → 68：模式循环/确认矩阵/footer 宽度/选择器降级/visual_clip）。
- PTY 全流程（pty_drive 注入按键）：`/resume` → ↓ → Enter 恢复会话 → Shift+Tab×2 依次显示 `ask before edits` → `accept edits on` → `yolo - auto approve` → `/exit` 干净退出。
- 键盘解析探针：`\x1b[B`→down、`\x1b[Z`→shift_tab、`\r`→enter 全部正确。

## 迭代二：输入框钉在终端底部（用户真机反馈）

用户反馈：① footer 末尾的模型名多余；② 旧 footer 残留在原地随回滚堆积；③ 要输入框整体钉在终端最下方。

**方案**：scroll region（滚动区域）实现真固定底栏——

- 内容滚动区 `1..h-4`；底部三行固定：`h-3` 横线、`h-2` 输入行、`h-1` footer，最底行 `h` 留空缓冲（避免最底行换行触发整屏滚动）；
- 每轮对话：光标先回内容区底部（`goto_content_end`）→ 输出在区内滚动、输入框纹丝不动 → 渲染横线 → 光标定位输入行进入行编辑；
- footer 去掉模型名；footer 行重绘前先 `\r\x1b[K` 清行（修复残留堆叠）；
- 行分配曾出错（rule/input 同行）+ footer 贴最底行会整屏滚动，均已修正并经 PTY 断言；
- 退出时 `\x1b[r` 解除滚动区域恢复正常终端；终端过小（<12 行）或非 TTY 自动退化为普通滚动模式。

**注意**：固定框模式下内容区滚动不走终端回滚缓冲（vim/less 同款行为），长会话回看历史靠 `/resume` 列表与会话文件；退出后终端恢复正常。

**验证**：68 测试全绿；PTY 断言——区域 `\x1b[1;26r`（30 行终端）、输入行定位 28 行、footer 每键实时重绘、yolo 切换、`再见` 干净退出。

## 迭代三：回到"普通滚动 + 底部钉住"（用户第三次真机反馈）

用户指出：① 内容区无法上翻（scroll region 吞掉了回滚）；② footer 去掉"shift+tab 切换模式"提醒；③ 提交过的用户消息要像 Claude Code 一样保留在对话流里。

**方案**：废弃 scroll region，改为 **DSR-CPR 光标查询 + 底部重绘**——

- 每次渲染输入框前向终端发 `\x1b[6n` 查询光标行；不足底部预留行（h-3）则用换行补齐，超出则 `\x1b[{k}S` 整屏上滚对齐。输出照常打印，**完整进入终端回滚缓冲，可自由上翻**；
- 提交后：清掉输入框三行，用户消息以**灰底横条**（`\x1b[48;5;236m` + ` ❯ 消息`，整行铺满宽度）印进对话流，与 Claude Code 一致；
- footer 只剩当前模式徽标 + 填充线；"shift+tab 切换权限模式"说明移到启动横幅；
- CPR 查询失败（非 TTY/不支持）自动退化为普通打印；PTY 驱动器 `/tmp/pty_drive.py` 增加 CPR 自动应答以模拟真实终端。

**验证**：68 测试全绿；PTY 六项断言全 PASS（CPR 查询、灰底回显、footer 无提醒无模型名、横幅说明、无 scroll region、干净退出）。

## 迭代四：双线框住输入行 + 框宽随输入伸缩

用户反馈：footer 右侧的填充横线去掉；输入行用上下两条线框起来；框宽随输入变化。

- 布局变为四行：**顶线 / 输入行 / 底线 / footer（仅模式徽标）**；`render_footer` 去掉填充线逻辑；
- 框宽 `frame_width(buf, term_w)`：内容宽度 + 2，下限 20、上限终端宽-2，每次按键重绘时更新（PTY 实测宽度 20→44 随输入逐字增长）；
- `read_line` 新增 `rule_fn(buf)` 回调；钉住模式下每键绝对定位重绘四行（`prepare_pinned_prompt` 同时渲染初始上下线）；
- 新增 `frame_width` 单测 3 个 + footer 无填充线断言 2 个（68 → 70）。

**验证**：70 测试全绿；PTY 断言——CPR 查询、上下框线绝对定位（26/28 行）、宽度线 20→44 伸缩、灰底回显、footer 无填充、干净退出。

## 迭代五：横幅信息精简

用户反馈横幅文字信息重复且冗余。精简为三行：`Freud v0.1.0`（名称+版本）、接入的模型名（`deepseek-chat`，Mock 模式显示 `mock-llm` 并附黄色提示行）、打开终端的目录。移除 `/help` 提示行、shift+tab 说明与 api_base 地址。框线铺满终端宽度（`h-1` 列）。

**验证**：67 测试全绿（横幅无断言依赖）；PTY 预览确认仅剩三行信息。

## 备注

- 终端内效果：橙色 `⏺`/吉祥物、暗色 `⎿` 结果、红色错误、黄色审批提示；本文档示例省略颜色。
- 分支 `feature/repl-ui`（基于 main `24f10b0`），改动尚未提交；全局 `freud` 为 editable 安装，跟随项目当前检出的分支。



