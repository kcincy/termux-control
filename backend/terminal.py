import asyncio
import codecs
import json
import os
import re
import shutil
import sys
import uuid
from pathlib import Path

from starlette.exceptions import HTTPException
from starlette.websockets import WebSocketDisconnect

SESSION_ID = re.compile(r'^tc-(?:term|job)-[a-f0-9]{12}$')


class Tmux:
    def __init__(self, config):
        self.config = config
        self.lock = asyncio.Lock()
        self.clients = 0
        self.executable = shutil.which('tmux')
        self.socket = str(config.state / 'tmux.sock')

    @property
    def available(self):
        return os.name == 'posix' and self.executable is not None

    async def command(self, *args, missing_ok=False):
        if not self.available:
            raise HTTPException(503, 'PTY/tmux requer Termux ou Linux. No Termux: pkg install tmux')
        process = await asyncio.create_subprocess_exec(self.executable, '-S', self.socket, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            output, error = await asyncio.wait_for(process.communicate(), 5)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise HTTPException(504, 'O tmux não respondeu.') from None
        if process.returncode and not missing_ok:
            raise HTTPException(400, error.decode(errors='replace')[:300] or 'Operação tmux falhou.')
        return output.decode(errors='replace') if not process.returncode else ''

    def validate(self, sid):
        if not SESSION_ID.fullmatch(sid):
            raise HTTPException(400, 'Sessão inválida.')

    async def listing(self):
        if not self.available:
            return []
        output = await self.command('list-sessions', '-F', '#{session_name}\t#{session_created}\t#{session_attached}', missing_ok=True)
        result = []
        for line in output.splitlines():
            parts = line.split('\t')
            if len(parts) == 3 and SESSION_ID.fullmatch(parts[0]):
                result.append(dict(id=parts[0], created=int(parts[1]), attached=int(parts[2]), kind='terminal' if '-term-' in parts[0] else 'job'))
        return result

    async def create(self, kind='term', command=None):
        async with self.lock:
            sessions = await self.listing()
            if len(sessions) >= 16 or sum(s['kind'] == 'terminal' for s in sessions) >= 8 and kind == 'term':
                raise HTTPException(409, 'Limite de sessões atingido (8 terminais / 16 no total).')
            sid = f'tc-{kind}-{uuid.uuid4().hex[:12]}'
            args = ['new-session', '-d', '-s', sid, '-c', str(self.config.root), '-x', '120', '-y', '32']
            if command:
                args.extend(command(sid))
            await self.command(*args)
            await self.command('set-option', '-t', sid, 'history-limit', '5000', missing_ok=True)
            return sid

    async def exists(self, sid):
        self.validate(sid)
        if not any(item['id'] == sid for item in await self.listing()):
            raise HTTPException(404, 'A sessão foi encerrada. Crie uma nova sessão.')

    async def kill(self, sid):
        await self.exists(sid)
        await self.command('kill-session', '-t', sid)

    async def bridge(self, websocket, sid, is_authenticated):
        await self.exists(sid)
        if '-term-' not in sid:
            raise HTTPException(403, 'Somente sessões de terminal permitem conexão interativa.')
        if self.clients >= 4:
            raise HTTPException(429, 'Limite de quatro terminais conectados simultaneamente.')
        import fcntl
        import pty
        import struct
        import termios

        self.clients += 1
        master = slave = None
        process = None
        reader_task = receiver_task = None
        try:
            master, slave = pty.openpty()
            os.set_blocking(master, False)
            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack('HHHH', 32, 120, 0, 0))
            environment = {**os.environ, 'TERM': 'xterm-256color', 'COLORTERM': 'truecolor'}
            process = await asyncio.create_subprocess_exec(sys.executable, str(Path(__file__).with_name('pty_bridge.py')), self.executable, self.socket, sid, stdin=slave, stdout=slave, stderr=slave, env=environment, start_new_session=True)
            os.close(slave)
            slave = None
            loop = asyncio.get_running_loop()

            async def output():
                decoder = codecs.getincrementaldecoder('utf-8')('replace')
                while True:
                    ready = loop.create_future()
                    def readable():
                        if not ready.done():
                            ready.set_result(None)
                    loop.add_reader(master, readable)
                    try:
                        await ready
                        chunk = os.read(master, 16384)
                    except (OSError, asyncio.CancelledError):
                        return
                    finally:
                        loop.remove_reader(master)
                    if not chunk:
                        return
                    if not is_authenticated():
                        await websocket.close(code=4401)
                        return
                    text = decoder.decode(chunk)
                    if text:
                        await websocket.send_json(dict(type='output', data=text))

            async def receive():
                while True:
                    if not is_authenticated():
                        await websocket.close(code=4401)
                        return
                    try:
                        raw = await asyncio.wait_for(websocket.receive_text(), 15)
                    except asyncio.TimeoutError:
                        await websocket.send_json(dict(type='ping'))
                        continue
                    if len(raw) > 32768:
                        await websocket.close(code=1009)
                        return
                    message = json.loads(raw)
                    if message.get('type') == 'input':
                        data = message.get('data')
                        if not isinstance(data, str):
                            raise ValueError('Entrada inválida')
                        pending = data.encode()
                        while pending:
                            try:
                                written = os.write(master, pending)
                                pending = pending[written:]
                            except BlockingIOError:
                                await asyncio.sleep(0.01)
                    elif message.get('type') == 'resize':
                        cols, rows = int(message['cols']), int(message['rows'])
                        if not 2 <= cols <= 500 or not 2 <= rows <= 300:
                            raise ValueError('Dimensões inválidas')
                        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))

            reader_task = asyncio.create_task(output())
            receiver_task = asyncio.create_task(receive())
            await asyncio.wait([reader_task, receiver_task], return_when=asyncio.FIRST_COMPLETED)
        except (WebSocketDisconnect, OSError, ValueError, KeyError, TypeError):
            pass
        finally:
            tasks = [task for task in (reader_task, receiver_task) if task]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if process and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            for descriptor in (master, slave):
                if descriptor is not None:
                    os.close(descriptor)
            self.clients -= 1
            try:
                await websocket.close()
            except (RuntimeError, OSError):
                pass
