import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Markdown } from './Markdown.js'
import { setThemePreference, useTheme } from './theme.js'
import { workflowPayload, type WorkflowTask } from './workflowForm.js'
import inputStyle from '../dsh/InputBar.module.css'
import sidebarStyle from '../dsh/SidebarRoot.module.css'
import './freud.css'

type Workspace = { id: string; title: string; path: string }
type Session = { id: string; title: string; workspace_id: string | null }
type Item = { type: string; content?: string; tool?: string; args?: Record<string, unknown>; id?: string; reason?: string; workflow_result?: boolean }

function AssistantReply({ content, workflowResult }: { content: string; workflowResult?: boolean }) {
  const trainingMarker = '\n\n<!-- freud-training-details -->\n'
  const trainingPosition = content.indexOf(trainingMarker)
  if (trainingPosition !== -1) {
    return <div className="reply" data-workflow-result={workflowResult || undefined}>
      <Markdown content={content.slice(0, trainingPosition)} />
      <details className="analysis-details">
        <summary>展开完整指标、参数和时间切分</summary>
        <Markdown content={content.slice(trainingPosition + trainingMarker.length)} />
      </details>
    </div>
  }
  const marker = '\n\n实际参数和完整结果：\n```json\n'
  const position = content.indexOf(marker)
  if (position === -1) return <div className="reply" data-workflow-result={workflowResult || undefined}><Markdown content={content} /></div>
  const summary = content.slice(0, position)
  const details = content.slice(position + marker.length).replace(/\n```\s*$/, '')
  return <div className="reply" data-workflow-result={workflowResult || undefined}><div className="analysis-result"><Markdown content={summary} /><details className="analysis-details"><summary>查看原始结果 JSON</summary><pre>{details}</pre></details></div></div>
}

async function api(path: string, data?: object) {
  const response = await fetch(path, data === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) })
  if (!response.ok) throw new Error((await response.json()).error || '请求失败')
  return response.json()
}

export const App = () => {
  const [sessions, setSessions] = useState<Session[]>([])
  const [workspaces, setWorkspaces] = useState<Workspace[]>([])
  const [workspaceId, setWorkspaceId] = useState('default')
  const [addingWorkspace, setAddingWorkspace] = useState(false)
  const [workspacePath, setWorkspacePath] = useState('')
  const [workspaceError, setWorkspaceError] = useState('')
  const [savingWorkspace, setSavingWorkspace] = useState(false)
  const [current, setCurrent] = useState<Session | null>(null)
  const [items, setItems] = useState<Item[]>([])
  const [draft, setDraft] = useState('')
  const [mode, setMode] = useState('chat')
  const [menuOpen, setMenuOpen] = useState(false)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const theme = useTheme()
  const menuRef = useRef<HTMLDivElement>(null)
  const [permissionMode, setPermissionMode] = useState('ask')
  const [followupTarget, setFollowupTarget] = useState<string | null>(null)
  const [tasks, setTasks] = useState<WorkflowTask[]>([])
  const [fieldValues, setFieldValues] = useState<Record<string, string>>({})
  const [optionsText, setOptionsText] = useState('')
  const [busy, setBusy] = useState(false)
  const [loadingSession, setLoadingSession] = useState(false)
  const [error, setError] = useState('')
  const feed = useRef<HTMLDivElement>(null)
  const selection = useRef(0)
  const lastItem = items.at(-1)
  const selectedTask = tasks.find(task => task.id === mode)
  const followupLabel = tasks.find(task => task.id === followupTarget)?.result_label || '结果'
  const workspace = workspaces.find(row => row.id === (current ? current.workspace_id : workspaceId))
  const groups = [...workspaces, ...(sessions.some(session => session.workspace_id === null) ? [{ id: null, title: '未分组', path: '' }] : [])]
  const refresh = async () => setSessions(await api('/api/sessions'))
  const refreshWorkspaces = async () => setWorkspaces(await api('/api/workspaces'))
  useEffect(() => {
    void Promise.all([refresh(), refreshWorkspaces(), api('/api/workflows').then(setTasks)]).catch(e => setError(String(e)))
  }, [])
  useEffect(() => {
    const view = feed.current
    if (!view) return
    const result = lastItem?.workflow_result && view.querySelector('[data-workflow-result]:last-of-type')
    if (result) {
      const start = view.scrollTop + result.getBoundingClientRect().top - view.getBoundingClientRect().top - 16
      view.scrollTo(0, start)
    } else view.scrollTo(0, view.scrollHeight)
  }, [items.length, lastItem, current?.id])
  useEffect(() => {
    if (!menuOpen) return
    const close = (event: MouseEvent) => { if (!menuRef.current?.contains(event.target as Node)) setMenuOpen(false) }
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') setMenuOpen(false) }
    document.addEventListener('click', close)
    document.addEventListener('keydown', escape)
    return () => { document.removeEventListener('click', close); document.removeEventListener('keydown', escape) }
  }, [menuOpen])
  function chooseMode(next: string) {
    setMode(next); setMenuOpen(false); setOptionsText(''); setFieldValues({})
  }
  function newSession(next = workspaceId) {
    selection.current++
    setLoadingSession(false)
    setWorkspaceId(next); setCurrent(null); setItems([]); setDraft(''); chooseMode('chat'); setPermissionMode('ask'); setFollowupTarget(null); setError(''); setSidebarOpen(false)
  }
  async function addWorkspace(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!workspacePath.trim() || busy || savingWorkspace) return
    setSavingWorkspace(true); setWorkspaceError('')
    try {
      const added: Workspace = await api('/api/workspaces', { path: workspacePath })
      await Promise.all([refreshWorkspaces(), refresh()])
      setAddingWorkspace(false); setWorkspacePath('')
      newSession(added.id)
    } catch (e) {
      setWorkspaceError(e instanceof Error ? e.message : String(e))
    } finally {
      setSavingWorkspace(false)
    }
  }
  async function select(session: Session) {
    if (busy) return
    const request = ++selection.current
    setLoadingSession(true)
    try {
      const data = await api(`/api/sessions/${encodeURIComponent(session.id)}`)
      if (request !== selection.current) return
      const history: Item[] = []
      const doneCalls = new Set<string>()
      for (const message of data.messages) {
        if (message.role === 'user') history.push({ type: 'user', content: message.content })
        if (message.role === 'assistant') {
          if (message.content) history.push({ type: 'assistant', content: message.content })
          for (const call of message.tool_calls || []) {
            const name = call.function?.name
            if (name === 'done') doneCalls.add(call.id)
            else if (name) history.push({ type: 'tool', tool: name, args: (() => { try { return JSON.parse(call.function.arguments) } catch { return {} } })() })
          }
        }
        if (message.role === 'tool') history.push({ type: doneCalls.has(message.tool_call_id) ? 'assistant' : 'result', content: message.content })
      }
      setCurrent({ id: data.id, title: data.title, workspace_id: data.workspace_id }); setItems(history)
      if (data.workspace_id) setWorkspaceId(data.workspace_id)
      setDraft(''); chooseMode('chat'); setPermissionMode('ask'); setFollowupTarget(data.followup_context?.target || null); setError(''); setSidebarOpen(false)
    } catch (e) {
      if (request === selection.current) setError(String(e))
    } finally {
      if (request === selection.current) setLoadingSession(false)
    }
  }
  async function send() {
    const text = draft.trim()
    if (!text || busy || savingWorkspace || loadingSession) return
    let taskPayload
    try {
      taskPayload = workflowPayload(selectedTask, fieldValues, optionsText)
    } catch (e) { setError(String(e)); return }
    setBusy(true); setError(''); setDraft('')
    try {
      const session: Session = current || await api('/api/sessions', { workspace_id: workspaceId })
      setCurrent(session)
      setItems(old => [...old, { type: 'user', content: text }])
      const request = { ...taskPayload, session_id: session.id, text, mode, permission_mode: permissionMode, followup: mode === 'chat' && !!followupTarget }
      const response = await fetch(mode === 'chat' ? '/api/chat' : '/api/workflow', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(request) })
      if (!response.ok || !response.body) throw new Error('无法开始对话')
      const reader = response.body.getReader(), decoder = new TextDecoder()
      let buffer = ''
      let failed = false
      while (true) {
        const { value, done } = await reader.read()
        buffer += decoder.decode(value, { stream: !done })
        const lines = buffer.split('\n'); buffer = lines.pop() || ''
        for (const line of lines) if (line) {
          const event: Item = JSON.parse(line)
          if (event.type === 'error') { failed = true; setError(event.content || '发生错误') }
          else if (event.type === 'assistant_update') setItems(old => {
            const last = old.at(-1)
            return last?.type === 'assistant' ? [...old.slice(0, -1), { ...last, content: event.content }] : old
          })
          else setItems(old => [...old, event])
        }
        if (done) break
      }
      const updated = await api(`/api/sessions/${encodeURIComponent(session.id)}`)
      setCurrent({ id: session.id, title: updated.title, workspace_id: updated.workspace_id })
      setFollowupTarget(updated.followup_context?.target || null)
      if (!failed && mode !== 'chat') chooseMode('chat')
      await refresh()
    } catch (e) { setError(String(e)) } finally { setBusy(false) }
  }
  async function endFollowup() {
    if (!current || busy || loadingSession) return
    const request = selection.current
    try {
      await api(`/api/sessions/${encodeURIComponent(current.id)}/end-context`, {})
      if (request === selection.current) setFollowupTarget(null)
    } catch (e) {
      if (request === selection.current) setError(String(e))
    }
  }
  async function removeSession(session: Session) {
    if (busy || !window.confirm(`删除对话“${session.title}”？此操作不可撤销。`)) return
    const request = selection.current
    try {
      const response = await fetch(`/api/sessions/${encodeURIComponent(session.id)}`, { method: 'DELETE', headers: { 'Content-Type': 'application/json' } })
      if (!response.ok) throw new Error((await response.json()).error || '删除失败')
      if (request === selection.current && current?.id === session.id) newSession()
      await refresh()
    } catch (e) { setError(String(e)) }
  }
  async function approve(item: Item, approved: boolean) {
    try {
      await api('/api/approve', { id: item.id, approved })
      setItems(old => old.map(i => i === item ? { ...i, type: 'approvalDone', content: approved ? '已允许' : '已拒绝' } : i))
    } catch (e) { setError(String(e)) }
  }
  return <div className="freud-app">
    {sidebarOpen && <button className="sidebar-backdrop" aria-label="关闭侧栏" onClick={() => setSidebarOpen(false)} />}
    <aside className={'sidebar' + (sidebarOpen ? ' sidebar-open' : '')}>
      <div className="brand-row"><div className="brand"><span className="brand-mark" aria-hidden="true">◉</span>Freud</div>
        <button className="theme-toggle" aria-label={theme === 'dark' ? '切换到浅色模式' : '切换到深色模式'} title="切换主题" onClick={() => setThemePreference(theme === 'dark' ? 'light' : 'dark')}>◐</button>
      </div>
      <button className={`new-chat ${sidebarStyle.newSession}`} disabled={busy || savingWorkspace} onClick={() => newSession()}><span aria-hidden="true">＋</span>新对话</button>
      <div className="workspaces-heading"><span>工作区</span><button className="add-workspace" title="添加项目工作区" aria-label="添加项目工作区" aria-expanded={addingWorkspace}
        disabled={busy || savingWorkspace} onClick={() => { setAddingWorkspace(!addingWorkspace); setWorkspaceError('') }}>＋</button></div>
      {addingWorkspace && <form className="workspace-form" onSubmit={event => void addWorkspace(event)}>
        <input aria-label="项目目录" placeholder="已有目录的绝对路径" value={workspacePath} required
          onChange={event => setWorkspacePath(event.target.value)} disabled={busy || savingWorkspace} autoFocus />
        <button disabled={busy || savingWorkspace || !workspacePath.trim()}>添加</button>
        {workspaceError && <small className="error" role="alert">{workspaceError}</small>}
      </form>}
      <div className="session-list">{groups.map(group => <section className="workspace-group" key={group.id ?? 'ungrouped'} aria-label={group.title}>
        {group.id === null ? <div className="workspace-name"><span className="folder-icon" aria-hidden="true" /><span>{group.title}</span></div>
          : <button className={'workspace-name' + (workspace?.id === group.id ? ' active' : '')} title={group.path}
              aria-pressed={workspace?.id === group.id} disabled={busy || savingWorkspace} onClick={() => newSession(group.id ?? undefined)}>
              <span className="folder-icon" aria-hidden="true" /><span>{group.title}</span></button>}
        {sessions.filter(s => s.workspace_id === group.id).map(s => <div className="session-row" key={s.id}>
          <button className={'session ' + (s.id === current?.id ? 'active' : '')} disabled={busy || savingWorkspace} onClick={() => void select(s)}>{s.title}</button>
          <button className="delete-session" disabled={busy || savingWorkspace} aria-label={`删除对话 ${s.title}`} title="删除对话" onClick={() => void removeSession(s)}>×</button>
        </div>)}
      </section>)}</div>
    </aside>
    <main className="main">
      <header className="header"><div className="header-content">
        <button className="mobile-menu" aria-label="打开侧栏" onClick={() => setSidebarOpen(true)}>☰</button>
        <span title={current?.title}>{current?.title || '新对话'}</span>
      </div></header>
      <div className="feed" ref={feed} aria-busy={busy || loadingSession}><div className="conversation-column content">{!current && !items.length && !loadingSession && !error && <div className="welcome"><h1>有什么可以帮你？</h1></div>}
        {items.map((item, index) => item.type === 'user' ? <div key={index} className="bubble">{item.content}</div> : item.type === 'assistant' ? <AssistantReply key={index} content={item.content || ''} workflowResult={item.workflow_result} /> : item.type === 'tool' ? <details key={index} className="tool"><summary>{item.tool}</summary><pre>{JSON.stringify(item.args, null, 2)}</pre></details> : item.type === 'result' ? <details key={index} className="result"><summary>工具结果</summary><pre>{item.content}</pre></details> : item.type === 'approval' ? <div key={index} className="approval"><strong>需要你的确认</strong><p>{item.reason}</p><pre>{item.tool} {JSON.stringify(item.args, null, 2)}</pre><button onClick={() => void approve(item, true)}>允许执行</button><button onClick={() => void approve(item, false)}>拒绝</button></div> : <div key={index} className="process-update">{item.content}</div>)}
        {(busy || loadingSession) && <p className="status" role="status">{loadingSession ? '正在打开对话…' : '正在处理…'}</p>}{error && <p className="error" role="alert">{error}</p>}</div></div>
      <div className="conversation-column composer-wrap">
        <div className="composer-context"><div className="composer-workspace">
          <span className="folder-icon" aria-hidden="true" />
          {current ? <span title={workspace?.path}>{workspace?.title ?? '未分组'}</span>
            : <select aria-label="对话工作区" value={workspaceId} disabled={busy || savingWorkspace || !workspaces.length}
                onChange={event => setWorkspaceId(event.target.value)}>
                {workspaces.map(row => <option key={row.id} value={row.id}>{row.title}</option>)}
              </select>}
        </div>{selectedTask && <button className="selected-task" title="取消数据任务" aria-label={`取消${selectedTask.label}`} onClick={() => chooseMode('chat')}>
          <span>{selectedTask.label}</span><span aria-hidden="true">×</span></button>}</div>
        <div className={`composer ${inputStyle.card}`}>
        {selectedTask && <div className="workflow-controls">
          {selectedTask.fields.map(field => <input key={field.name} value={fieldValues[field.name] || ''}
            onChange={e => setFieldValues({ ...fieldValues, [field.name]: e.target.value })}
            placeholder={field.placeholder} aria-label={field.label} />)}
          {selectedTask.advanced && <details className="advanced"><summary>高级参数</summary><textarea className="options" value={optionsText} onChange={e => setOptionsText(e.target.value)} placeholder="JSON 对象，可留空" aria-label="高级参数 JSON" /></details>}
          <small>{selectedTask.hint}</small>
        </div>}
        <textarea className="message-input" value={draft} onChange={e => setDraft(e.target.value)} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void send() } }} placeholder={selectedTask ? '描述这次数据任务…' : followupTarget ? '继续询问这个结果，或选择其他数据任务…' : '描述你想完成的任务…'} aria-label="消息" />
        <div className="toolbar"><div className="task-picker" ref={menuRef}>
          <button className="task-trigger" disabled={busy || loadingSession || !tasks.length} aria-expanded={menuOpen} aria-controls="task-menu" onClick={() => setMenuOpen(!menuOpen)}><span aria-hidden="true">＋</span>数据任务</button>
          {menuOpen && <div className="task-menu" id="task-menu" aria-label="数据任务">{tasks.map(task => <button key={task.id} className="task-choice" onClick={() => chooseMode(task.id)}>{task.label}<small>{task.description}</small></button>)}</div>}
        </div>
        <div className="composer-actions">
          <select className="permission-mode" aria-label="权限模式" value={permissionMode} disabled={busy || loadingSession} onChange={e => setPermissionMode(e.target.value)}><option value="ask">询问确认</option><option value="accept">自动允许编辑</option><option value="yolo">自动批准（危险）</option></select>
          <button className="send" disabled={busy || savingWorkspace || loadingSession || !draft.trim()} onClick={() => void send()} aria-label="发送">↑</button>
        </div></div>
      </div>{mode === 'chat' && followupTarget && <div className="hint">正在基于最近的{followupLabel}继续对话 <button className="end-context" disabled={busy || loadingSession} onClick={() => void endFollowup()}>结束结果上下文</button></div>}</div>
    </main></div>
}
