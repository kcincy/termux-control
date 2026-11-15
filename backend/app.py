import asyncio
import hmac
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import HTTPConnection
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.staticfiles import StaticFiles

from .config import load_config
from .files import Files, MAX_UPLOAD
from .jobs import Jobs
from .metrics import Metrics, processes, tailscale_status
from .security import COOKIE, Store
from .terminal import Tmux

DIST = Path(__file__).resolve().parent.parent / 'dist'


async def body(request):
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > 3 * 1024 * 1024:
            raise HTTPException(413, 'Requisição muito grande.')
    try:
        result = json.loads(content)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, 'JSON inválido.') from None


class Security:
    """One guard for HTTP and WebSocket handshakes; no proxy headers are trusted."""
    def __init__(self, app, config, store):
        self.app, self.config, self.store = app, config, store
        self.hosts = {urlsplit(origin).netloc.lower() for origin in config.origins}

    async def __call__(self, scope, receive, send):
        if scope['type'] not in ('http', 'websocket'):
            return await self.app(scope, receive, send)
        request = HTTPConnection(scope)
        websocket = scope['type'] == 'websocket'

        async def reject(code, message):
            if websocket:
                await send(dict(type='websocket.close', code=4401 if code == 401 else 4403))
            else:
                await JSONResponse(dict(error=message), status_code=code)(scope, receive, send)

        if request.headers.get('host', '').lower() not in self.hosts:
            return await reject(400, 'Host não autorizado. Configure a origem exata no servidor.')
        origin = request.headers.get('origin')
        if origin is not None and origin not in self.config.origins:
            return await reject(403, 'Origem não autorizada.')
        write = scope.get('method') not in ('GET', 'HEAD', 'OPTIONS')
        if (websocket or write) and origin not in self.config.origins:
            return await reject(403, 'Origem obrigatória.')
        api = scope['path'].startswith('/api/')
        public = scope['path'] in ('/api/login', '/api/health')
        session = self.store.session(request.cookies.get(COOKIE))
        if api and not public:
            if not session:
                return await reject(401, 'Sua sessão expirou. Entre novamente.')
            scope['tc_session'] = session
            if websocket:
                protocols = scope.get('subprotocols', [])
                if len(protocols) != 2 or protocols[0] != 'termux-control' or not hmac.compare_digest(protocols[1].encode(), session['csrf'].encode()):
                    return await reject(403, 'Autorização WebSocket inválida.')
            elif write and not hmac.compare_digest(request.headers.get('x-csrf-token', '').encode(), session['csrf'].encode()):
                return await reject(403, 'Token CSRF inválido.')

        async def secure_send(message):
            if message['type'] == 'http.response.start':
                headers = list(message.get('headers', []))
                headers.extend([
                    (b'x-content-type-options', b'nosniff'),
                    (b'x-frame-options', b'DENY'),
                    (b'referrer-policy', b'no-referrer'),
                    (b'permissions-policy', b'camera=(), microphone=(), geolocation=()'),
                    (b'content-security-policy', b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
                ])
                if api:
                    headers.append((b'cache-control', b'no-store'))
                message['headers'] = headers
            await send(message)

        await self.app(scope, receive, secure_send)


def create_app(config=None, tmux=None):
    config = (config or load_config()).validate()
    store = Store(config)
    files = Files(config.root, config.state)
    tmux = tmux or Tmux(config)
    jobs = Jobs(config, store, files, tmux)
    metrics = Metrics(config.root)
    network = dict(status='unknown', ip=None, message='Verificação pendente')

    @asynccontextmanager
    async def lifespan(app):
        nonlocal network
        network = await tailscale_status()
        store.event('Servidor iniciado')
        yield
        store.db.close()

    async def health(request):
        return JSONResponse(dict(ok=True))

    async def login(request):
        data = await body(request)
        username, password = data.get('username', ''), data.get('password', '')
        if not isinstance(username, str) or not isinstance(password, str) or len(username) > 128 or len(password) > 1024:
            raise HTTPException(400, 'Credenciais inválidas.')
        token, csrf = store.login(username, password, request.client.host)
        response = JSONResponse(dict(username=config.username, csrf=csrf))
        response.set_cookie(COOKIE, token, max_age=config.session_hours * 3600, httponly=True, secure=config.secure_cookie, samesite='strict', path='/')
        return response

    async def me(request):
        return JSONResponse(dict(username=config.username, csrf=request.scope['tc_session']['csrf']))

    async def logout(request):
        store.revoke(request.scope['tc_session']['id'])
        response = JSONResponse(dict(ok=True))
        response.delete_cookie(COOKIE, path='/')
        return response

    async def stats(request):
        return JSONResponse(dict(**metrics.sample(), tailscale=network, terminal_available=tmux.available, sessions=await tmux.listing()))

    async def events(request):
        return JSONResponse([dict(row) for row in store.db.execute('SELECT * FROM events ORDER BY rowid DESC LIMIT 30')])

    async def terminals(request):
        if request.method == 'POST':
            sid = await tmux.create()
            store.event('Sessão de terminal criada')
            return JSONResponse(dict(id=sid), status_code=201)
        return JSONResponse([item for item in await tmux.listing() if item['kind'] == 'terminal'])

    async def delete_terminal(request):
        sid = request.path_params['sid']
        if '-term-' not in sid:
            raise HTTPException(403, 'Sessão de terminal inválida.')
        await tmux.kill(sid)
        store.event('Sessão de terminal encerrada')
        return JSONResponse(dict(ok=True))

    async def terminal_ws(websocket):
        token = websocket.cookies.get(COOKIE)
        try:
            await websocket.accept(subprotocol='termux-control')
            await tmux.bridge(websocket, websocket.path_params['sid'], lambda: store.session(token) is not None)
        except HTTPException as error:
            try:
                await websocket.send_json(dict(type='error', message=error.detail))
            except RuntimeError:
                pass
            await websocket.close(code=4404 if error.status_code == 404 else 4403)

    async def file_list(request):
        relative = request.query_params.get('path', '.')
        return JSONResponse(dict(path=relative, entries=files.listing(relative)))

    async def file_text(request):
        if request.method == 'GET':
            result = files.text(request.query_params.get('path', ''))
            result['version'] = str(result['version'])
            return JSONResponse(result)
        data = await body(request)
        if not isinstance(data.get('content'), str):
            raise HTTPException(400, 'Conteúdo de texto obrigatório.')
        version = files.save(data.get('path'), data['content'], data.get('version'))
        store.event('Arquivo de texto atualizado')
        return JSONResponse(dict(version=str(version)))

    async def file_mutation(request):
        data = await body(request)
        target = files.path(data.get('path'), False)
        if request.method == 'POST':
            if data.get('directory') is True:
                target.mkdir()
            else:
                with target.open('xb'):
                    pass
            store.event('Arquivo ou pasta criado')
        elif request.method == 'PATCH':
            destination = files.path(data.get('destination'), False)
            if destination.exists():
                raise HTTPException(409, 'O destino já existe.')
            target.rename(destination)
            store.event('Arquivo ou pasta renomeado')
        else:
            if data.get('confirm') is not True:
                raise HTTPException(400, 'Confirmação de exclusão obrigatória.')
            if target.is_dir():
                target.rmdir()
            else:
                target.unlink()
            store.event('Arquivo ou pasta excluído')
        return JSONResponse(dict(ok=True))

    async def upload(request):
        target = files.path(request.query_params.get('path', ''), False)
        length = 0
        try:
            with target.open('xb') as stream:
                try:
                    async for chunk in request.stream():
                        length += len(chunk)
                        if length > MAX_UPLOAD:
                            raise HTTPException(413, 'Upload limitado a 16 MiB.')
                        stream.write(chunk)
                except BaseException:
                    stream.close()
                    target.unlink(missing_ok=True)
                    raise
        except FileExistsError:
            raise HTTPException(409, 'Arquivo já existe. Renomeie antes de enviar.') from None
        store.event('Upload concluído')
        return JSONResponse(dict(ok=True, size=length), status_code=201)

    async def download(request):
        target = files.path(request.query_params.get('path', ''), False)
        if not target.is_file():
            raise HTTPException(404, 'Arquivo não encontrado.')
        return FileResponse(target, filename=target.name, media_type='application/octet-stream', headers={'Cache-Control': 'no-store'})

    async def scripts(request):
        if request.method == 'POST':
            return JSONResponse(dict(id=jobs.save(await body(request))), status_code=201)
        return JSONResponse(jobs.scripts())

    async def remove_script(request):
        if any(item['script_id'] == request.path_params['sid'] and item['active'] for item in await jobs.listing()):
            raise HTTPException(409, 'Interrompa as execuções ativas antes de remover o favorito.')
        with store.db:
            store.db.execute('DELETE FROM scripts WHERE id = ?', (request.path_params['sid'],))
        return JSONResponse(dict(ok=True))

    async def run_script(request):
        return JSONResponse(dict(id=await jobs.start(request.path_params['sid'])), status_code=201)

    async def job_list(request):
        return JSONResponse(dict(jobs=await jobs.listing(), processes=await processes()))

    async def job_action(request):
        data = await body(request)
        sid = request.path_params['sid']
        item = next((item for item in await jobs.listing() if item['id'] == sid), None)
        if not item:
            raise HTTPException(404, 'Execução não encontrada.')
        if data.get('action') not in ('stop', 'restart'):
            raise HTTPException(400, 'Ação inválida.')
        if item['active']:
            await tmux.kill(sid)
            # Allow the runner's five-second termination grace before a restart.
            for _ in range(60):
                status = jobs.logs / f'{sid}.status.json'
                if status.exists() and json.loads(status.read_text()).get('exit_code') is not None:
                    break
                await asyncio.sleep(0.1)
            else:
                raise HTTPException(409, 'Encerramento ainda não confirmado. Verifique os logs antes de reiniciar.')
        store.event('Execução interrompida pelo painel')
        return JSONResponse(dict(id=await jobs.start(item['script_id']) if data['action'] == 'restart' else sid))

    async def job_logs(request):
        return JSONResponse(dict(content=jobs.log(request.path_params['sid'])))

    async def settings(request):
        return JSONResponse(dict(root=str(config.root), host=config.host, port=config.port, origins=config.origins, secure_cookie=config.secure_cookie, session_hours=config.session_hours, wake_lock=config.wake_lock, terminal_available=tmux.available, tailscale=network, version='1.0.0'))

    async def auth_sessions(request):
        rows = store.db.execute('SELECT id, created, expires FROM sessions WHERE expires > ? ORDER BY created DESC', (time.time(),))
        return JSONResponse([dict(row, current=row['id'] == request.scope['tc_session']['id']) for row in rows])

    async def revoke_session(request):
        store.revoke(request.path_params['sid'])
        return JSONResponse(dict(ok=True))

    async def index(request):
        if request.url.path.startswith('/api/'):
            raise HTTPException(404, 'Endpoint não encontrado.')
        if not (DIST / 'index.html').is_file():
            return JSONResponse(dict(error='Frontend não compilado. Execute npm ci && npm run build.'), status_code=503)
        return FileResponse(DIST / 'index.html', headers={'Cache-Control': 'no-cache'})

    async def error(request, exc):
        if isinstance(exc, HTTPException):
            return JSONResponse(dict(error=exc.detail), status_code=exc.status_code)
        if isinstance(exc, FileExistsError):
            return JSONResponse(dict(error='O arquivo ou destino já existe.'), status_code=409)
        if isinstance(exc, FileNotFoundError):
            return JSONResponse(dict(error='Arquivo ou pasta não encontrado.'), status_code=404)
        if isinstance(exc, PermissionError):
            return JSONResponse(dict(error='O Android ou sistema recusou a permissão.'), status_code=403)
        return JSONResponse(dict(error='Operação recusada pelo sistema. Pastas devem estar vazias para excluir.'), status_code=400)

    routes = [
        Route('/api/health', health), Route('/api/login', login, methods=['POST']),
        Route('/api/me', me), Route('/api/logout', logout, methods=['POST']),
        Route('/api/stats', stats), Route('/api/events', events),
        Route('/api/terminals', terminals, methods=['GET', 'POST']),
        Route('/api/terminals/{sid}', delete_terminal, methods=['DELETE']),
        WebSocketRoute('/api/terminal/{sid}', terminal_ws),
        Route('/api/files', file_list), Route('/api/files/text', file_text, methods=['GET', 'PUT']),
        Route('/api/files/entry', file_mutation, methods=['POST', 'PATCH', 'DELETE']),
        Route('/api/files/upload', upload, methods=['POST']), Route('/api/files/download', download),
        Route('/api/scripts', scripts, methods=['GET', 'POST']), Route('/api/scripts/{sid}', remove_script, methods=['DELETE']),
        Route('/api/scripts/{sid}/run', run_script, methods=['POST']),
        Route('/api/jobs', job_list), Route('/api/jobs/{sid}', job_action, methods=['POST']), Route('/api/jobs/{sid}/logs', job_logs),
        Route('/api/settings', settings), Route('/api/auth-sessions', auth_sessions), Route('/api/auth-sessions/{sid}', revoke_session, methods=['DELETE']),
    ]
    app = Starlette(routes=routes, lifespan=lifespan, exception_handlers={HTTPException: error, OSError: error})
    app.mount('/assets', StaticFiles(directory=DIST / 'assets', check_dir=False), name='assets')
    app.add_route('/favicon.svg', lambda request: FileResponse(Path(__file__).resolve().parent.parent / 'public/favicon.svg'))
    app.add_route('/{path:path}', index)
    app.state.store, app.state.config, app.state.tmux = store, config, tmux
    app.add_middleware(Security, config=config, store=store)
    return app
