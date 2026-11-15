import json
import shutil
import sys
import uuid
from pathlib import Path

from starlette.exceptions import HTTPException


class Jobs:
    def __init__(self, config, store, files, tmux):
        self.config, self.store, self.files, self.tmux = config, store, files, tmux
        self.logs = config.state / 'jobs'
        self.logs.mkdir(exist_ok=True, mode=0o700)

    def scripts(self):
        return [dict(row, args=json.loads(row['args'])) for row in self.store.db.execute('SELECT * FROM scripts ORDER BY name')]

    def save(self, data):
        name, relative, args = data.get('name'), data.get('path'), data.get('args', [])
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or not isinstance(relative, str):
            raise HTTPException(400, 'Informe nome e caminho do script.')
        target = self.files.path(relative, False)
        if not target.is_file() or target.suffix not in ('.py', '.js', '.mjs', '.cjs'):
            raise HTTPException(400, 'Selecione um arquivo Python (.py) ou Node (.js, .mjs, .cjs) existente.')
        if not isinstance(args, list) or len(args) > 32 or any(not isinstance(arg, str) or len(arg) > 2048 or '\x00' in arg for arg in args):
            raise HTTPException(400, 'Argumentos inválidos.')
        if self.store.db.execute('SELECT COUNT(*) FROM scripts').fetchone()[0] >= 64:
            raise HTTPException(409, 'Limite de 64 scripts favoritos.')
        sid = uuid.uuid4().hex[:12]
        with self.store.db:
            self.store.db.execute('INSERT INTO scripts VALUES (?, ?, ?, ?)', (sid, name.strip(), relative, json.dumps(args)))
        return sid

    async def start(self, script_id):
        script = self.store.db.execute('SELECT * FROM scripts WHERE id = ?', (script_id,)).fetchone()
        if not script:
            raise HTTPException(404, 'Script não encontrado.')
        target = self.files.path(script['path'], False)
        if not target.is_file():
            raise HTTPException(404, 'Arquivo do script não encontrado.')
        executable = sys.executable if target.suffix == '.py' else shutil.which('node')
        if not executable:
            raise HTTPException(503, 'Node.js não está instalado. No Termux: pkg install nodejs-lts')
        # Retain at most 32 completed runs; live runs are bounded by Tmux.create.
        active = {item['id'] for item in await self.tmux.listing()}
        completed = sorted((p for p in self.logs.glob('*.meta.json') if p.name.removesuffix('.meta.json') not in active), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in completed[31:]:
            old_id = old.name.removesuffix('.meta.json')
            for suffix in ('.meta.json', '.status.json', '.log'):
                (self.logs / f'{old_id}{suffix}').unlink(missing_ok=True)

        def command(sid):
            (self.logs / f'{sid}.meta.json').write_text(json.dumps(dict(script_id=script_id, name=script['name'])))
            return [sys.executable, str(Path(__file__).with_name('job_runner.py')), str(self.logs / f'{sid}.log'), str(self.logs / f'{sid}.status.json'), executable, '-u' if target.suffix == '.py' else '--', str(target), *json.loads(script['args'])]

        sid = await self.tmux.create('job', command)
        self.store.event('Script favorito iniciado')
        return sid

    async def listing(self):
        active = {item['id'] for item in await self.tmux.listing()}
        result = []
        for meta in self.logs.glob('*.meta.json'):
            sid = meta.name.removesuffix('.meta.json')
            try:
                item = json.loads(meta.read_text())
                status = self.logs / f'{sid}.status.json'
                item.update(json.loads(status.read_text()) if status.exists() else {})
                result.append(dict(item, id=sid, active=sid in active))
            except (OSError, ValueError):
                continue
        return sorted(result, key=lambda item: item.get('started', 0), reverse=True)

    def log(self, sid):
        self.tmux.validate(sid)
        target = self.logs / f'{sid}.log'
        if not target.is_file():
            return ''
        with target.open('rb') as stream:
            stream.seek(max(0, target.stat().st_size - 65536))
            return stream.read(65536).decode(errors='replace')
