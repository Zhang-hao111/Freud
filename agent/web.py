"""本地网页入口：会话、Agent 事件与工具审批。"""

import argparse
import json
import mimetypes
import os
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from agent.config import load_config
from agent.conversations import install_conversations
from agent.plugins import install_plugins
from agent.runtime import default_runtime, install_workflows
from agent.session import dump_messages
from agent.workspaces import install_workspaces

DIST = Path(__file__).resolve().with_name('web_dist')


class WebAgent:
    def __init__(self, config, store=None, llm_factory=None, mock=False, *, runtime=None):
        self.runtime = runtime if runtime is not None else default_runtime(config, store, llm_factory, mock)
        if 'workflows' not in self.runtime:
            self.runtime.mount(install_workflows)
        if 'workspaces' not in self.runtime:
            self.runtime.mount(install_workspaces)
        if 'conversations' not in self.runtime:
            self.runtime.mount(install_conversations)
        self.conversations = self.runtime['conversations']
        self.pending = {}
        self.lock = threading.Lock()

    def approve(self, request_id, approved):
        with self.lock:
            pending = self.pending.get(request_id)
            if pending is None or pending['event'].is_set():
                return False
            pending['approved'] = approved
            pending['event'].set()
            return True

    def _confirm_tool(self, action, reason, emit, session_id, heartbeat):
        request_id = uuid.uuid4().hex
        pending = {'event': threading.Event(), 'approved': False}
        with self.lock:
            self.pending[request_id] = pending
        try:
            emit({'type': 'approval', 'id': request_id, 'tool': action.tool,
                  'args': action.args or {}, 'reason': reason})
            now = time.monotonic()
            deadline = now + 300
            next_heartbeat = now + 1
            while not pending['event'].wait(.1):
                self.conversations.checkpoint(session_id)
                now = time.monotonic()
                if now >= deadline:
                    return False
                if heartbeat is not None and now >= next_heartbeat:
                    heartbeat()
                    next_heartbeat = now + 1
            self.conversations.checkpoint(session_id)
            return pending['approved']
        finally:
            with self.lock:
                self.pending.pop(request_id, None)

    def chat(self, session, text, emit, *, heartbeat=None, **options):
        return self.conversations.chat(
            session, text, emit, **options,
            approve=lambda action, _gate, reason: self._confirm_tool(
                action, reason, emit, session.data['id'], heartbeat,
            ),
        )


def make_handler(app):
    conversations = app.conversations

    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, data):
            body = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def allowed(self):
            host = self.headers.get('Host', '')
            expected = f'127.0.0.1:{self.server.server_port}'
            origin = self.headers.get('Origin')
            return host == expected and (origin is None or origin == f'http://{expected}')

        def do_GET(self):
            if not self.allowed():
                return self.reply(403, {'error': '无效的请求来源'})
            path = urlparse(self.path).path
            if path == '/api/workspaces':
                return self.reply(200, conversations.workspaces.list())
            if path == '/api/workflows':
                return self.reply(200, conversations.workflows.catalog())
            artifact = re.fullmatch(r'/api/workflows/([^/]+)/([a-z][a-z0-9-]*)/([0-9a-f]{32})(?:/([a-z_]+))?', path)
            if artifact:
                session = conversations.session(artifact.group(1))
                found = conversations.workflows.artifact(session, artifact.group(2), artifact.group(3), artifact.group(4)) if session else None
                if not found:
                    return self.reply(404, {'error': '结果不存在'})
                data, content_type = found
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                extension = {'text/markdown': 'md', 'application/json': 'json'}.get(content_type.split(';', 1)[0])
                if extension:
                    self.send_header('Content-Disposition', f'attachment; filename="{artifact.group(2)}-{artifact.group(3)}.{extension}"')
                self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; sandbox")
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if path == '/api/sessions':
                return self.reply(200, conversations.list_sessions())
            status = re.fullmatch(r'/api/sessions/([^/]+)/status', path)
            if status:
                return self.reply(200, {'run_status': conversations.run_status(status.group(1))})
            if path.startswith('/api/sessions/'):
                session_id = path.removeprefix('/api/sessions/')
                # 如果任务在读取历史期间结束，让前端再刷新一次以取到最终结果。
                run_status = conversations.run_status(session_id)
                session = conversations.session(session_id)
                if not session:
                    return self.reply(404, {'error': '会话不存在'})
                return self.reply(200, {**conversations.session_info(session),
                                        'messages': dump_messages(session.messages),
                                        'run_status': run_status,
                                        'followup_context': conversations.workflows.context(session)})
            target = (DIST / path.lstrip('/')).resolve() if path != '/' else DIST / 'index.html'
            if not target.is_relative_to(DIST.resolve()) or not target.is_file():
                return self.reply(404, {'error': '页面不存在，请先构建前端'})
            data = target.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', mimetypes.guess_type(target.name)[0] or 'application/octet-stream')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_DELETE(self):
            if not self.allowed() or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                return self.reply(403, {'error': '无效的请求来源'})
            match = re.fullmatch(r'/api/sessions/([^/]+)', urlparse(self.path).path)
            if not match:
                return self.reply(404, {'error': '接口不存在'})
            try:
                return self.reply(200 if conversations.delete_session(match.group(1)) else 404, {})
            except ValueError as exc:
                return self.reply(409, {'error': str(exc)})

        def do_POST(self):
            if not self.allowed() or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                return self.reply(403, {'error': '无效的请求来源'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if length <= 0 or length > 1024 * 1024:
                    raise ValueError('请求大小无效')
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError('请求格式无效')
            except (ValueError, json.JSONDecodeError):
                return self.reply(400, {'error': '请求格式无效'})
            if self.path in ('/api/workspaces', '/api/sessions'):
                try:
                    if self.path == '/api/workspaces':
                        return self.reply(200, conversations.workspaces.add(data.get('path')))
                    session = conversations.new_session(data.get('workspace_id', 'default'))
                    return self.reply(201, conversations.session_info(session))
                except ValueError as exc:
                    return self.reply(400, {'error': str(exc)})
            match = re.fullmatch(r'/api/sessions/([^/]+)/cancel', self.path)
            if match:
                if conversations.cancel(match.group(1)):
                    return self.reply(202, {'run_status': 'cancelling'})
                return self.reply(409, {'error': '当前没有运行中的任务'})
            match = re.fullmatch(r'/api/sessions/([^/]+)/end-context', self.path)
            if match:
                try:
                    conversations.clear_followup(match.group(1))
                    return self.reply(200, {})
                except ValueError as exc:
                    return self.reply(409, {'error': str(exc)})
            if self.path == '/api/approve':
                if not isinstance(data.get('approved'), bool):
                    return self.reply(400, {'error': '审批值无效'})
                return self.reply(200 if app.approve(data.get('id'), data['approved']) else 404, {})
            if self.path in ('/api/chat', '/api/workflow'):
                session = conversations.session(data.get('session_id'))
                text = data.get('text')
                if not session or not isinstance(text, str) or not text.strip():
                    return self.reply(400, {'error': '会话或消息无效'})
                self.send_response(200)
                self.send_header('Content-Type', 'application/x-ndjson; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                def emit(event):
                    self.wfile.write((json.dumps(event, ensure_ascii=False) + '\n').encode())
                    self.wfile.flush()
                def heartbeat():
                    # 空行不产生界面事件，写失败时清理正在等待的审批。
                    self.wfile.write(b'\n')
                    self.wfile.flush()
                try:
                    if self.path == '/api/chat':
                        app.chat(session, text.strip(), emit, followup=data.get('followup') is True,
                                 permission_mode=data.get('permission_mode', 'ask'), heartbeat=heartbeat)
                    else:
                        conversations.run_workflow(session, text.strip(), data, emit)
                except ValueError as exc:
                    emit({'type': 'error', 'content': str(exc)})
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            return self.reply(404, {'error': '接口不存在'})
    return Handler


def main():
    parser = argparse.ArgumentParser(description='Freud 本地网页')
    parser.add_argument('--port', type=int, default=8875)
    parser.add_argument('--mock', action='store_true')
    args = parser.parse_args()
    if not (DIST / 'index.html').exists():
        parser.error('缺少网页资源：请安装完整 wheel，或在源码的 web 目录执行 pnpm install && pnpm build')
    config = load_config()
    config['launch_dir'] = str(Path.cwd())
    config['workspace'] = str(Path(config['workspace']).expanduser().resolve())
    os.chdir(config['workspace'])
    app = WebAgent(config, mock=args.mock)
    install_plugins(app.runtime)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(app))
    print(f'Freud 网页：http://127.0.0.1:{server.server_port}/', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
