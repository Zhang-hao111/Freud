/** 与 agent/events.py 对应的 NDJSON 流式事件。 */
export type StreamEvent =
  | { type: 'progress' | 'result' | 'error' | 'assistant_update'; content: string }
  | { type: 'assistant'; content: string; workflow_result?: boolean }
  | { type: 'tool'; tool: string; args: Record<string, unknown> }
  | { type: 'approval'; id: string; tool: string; args: Record<string, unknown>; reason: string }

export function parseStreamEvent(line: string): StreamEvent {
  const event: unknown = JSON.parse(line)
  if (!event || typeof event !== 'object' || !('type' in event)) throw new Error('收到无效的事件')
  const row = event as Record<string, unknown>
  const argsValid = !!row.args && typeof row.args === 'object' && !Array.isArray(row.args)
  switch (row.type) {
    case 'progress': case 'result': case 'error': case 'assistant_update':
      if (typeof row.content === 'string') return row as StreamEvent
      break
    case 'assistant':
      if (typeof row.content === 'string' && (row.workflow_result === undefined || typeof row.workflow_result === 'boolean')) return row as StreamEvent
      break
    case 'tool':
      if (typeof row.tool === 'string' && argsValid) return row as StreamEvent
      break
    case 'approval':
      if (typeof row.id === 'string' && typeof row.tool === 'string' && typeof row.reason === 'string' && argsValid) return row as StreamEvent
  }
  throw new Error('收到无效的事件')
}
