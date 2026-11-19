type TaskField = {
  name: string
  label: string
  placeholder: string
  kind: 'text' | 'integer' | 'integer-list'
  location: 'options' | 'request'
}

export type WorkflowTask = {
  id: string
  label: string
  description: string
  hint: string
  result_label: string
  fields: TaskField[]
  advanced: boolean
}

export function workflowPayload(task: WorkflowTask | undefined, values: Record<string, string>, advanced: string) {
  let options: Record<string, unknown> = {}
  const request: Record<string, unknown> = {}
  if (task?.advanced && advanced.trim()) {
    options = JSON.parse(advanced)
    if (!options || Array.isArray(options) || typeof options !== 'object') {
      throw new Error('高级参数必须是 JSON 对象')
    }
  }
  for (const field of task?.fields || []) {
    const value = values[field.name]?.trim()
    if (!value) continue
    let parsed: string | number | number[] = value
    if (field.kind !== 'text') {
      const parts = field.kind === 'integer-list' ? value.split(/[,，]/) : [value]
      const numbers = parts.map(part => Number(part.trim()))
      if (numbers.some(number => !Number.isSafeInteger(number) || number < 1)) {
        throw new Error(field.label + '必须为正整数')
      }
      parsed = field.kind === 'integer-list' ? numbers : numbers[0]
    }
    const target = field.location === 'request' ? request : options
    target[field.name] = parsed
  }
  return { ...request, options }
}
