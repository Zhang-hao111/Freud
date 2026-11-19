# 网页流式事件协议

`POST /api/chat` 和 `POST /api/workflow` 返回 UTF-8 NDJSON，客户端按发送顺序处理 JSON 事件，并忽略审批等待期间用于检测断线的空行。断线后待审批的工具不会执行，该回合会结束。字段和类型在 `agent/events.py` 与 `web/src/app/events.ts` 中声明。历史消息仍通过会话接口获取，不使用此事件格式。

| type | 字段 | 用途 |
| --- | --- | --- |
| `progress` | `content: string` | 工作流执行进度 |
| `assistant` | `content: string`，可选 `workflow_result: boolean` | 追加助手回复，工作流结果可带标记 |
| `assistant_update` | `content: string` | 更新最近一条助手回复，例如追加 LLM 解读 |
| `tool` | `tool: string`，`args: object` | 工具调用 |
| `result` | `content: string` | 工具结果 |
| `approval` | `id: string`，`tool: string`，`args: object`，`reason: string` | 请求用户批准，使用 `/api/approve` 回应 |
| `error` | `content: string` | 本回合错误 |

`GET /api/sessions/{id}/status` 返回运行状态 `idle`、`running` 或 `cancelling`。发送 `POST /api/sessions/{id}/cancel` 请求停止当前回合。已有结果保留，尚未完成的任务会在下一个安全阶段停止。

任务目录由 `GET /api/workflows` 返回，每个任务声明文本、字段和高级参数；具体请求按字段的 `location` 放在请求顶层或 `options` 中。训练与分析结果当前以 Markdown 返回，报告和图表通过工作流产物接口读取。修改协议时需要同步 Python、TypeScript 和本文档。
