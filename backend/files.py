import os
import tempfile
from pathlib import Path

from starlette.exceptions import HTTPException

MAX_TEXT = 2 * 1024 * 1024
MAX_UPLOAD = 16 * 1024 * 1024


class Files:
    def __init__(self, root, state):
        self.root, self.state = root.resolve(), state.resolve()

    def path(self, relative='.', allow_root=True):
        if not isinstance(relative, str) or len(relative) > 4096 or '\x00' in relative or '\\' in relative:
            raise HTTPException(400, 'Caminho inválido.')
        raw = Path(relative)
        if raw.is_absolute() or '..' in raw.parts or raw.drive:
            raise HTTPException(403, 'Caminho fora da pasta autorizada.')
        result = (self.root / raw).resolve()
        if not result.is_relative_to(self.root) or result == self.state or result.is_relative_to(self.state):
            raise HTTPException(403, 'Caminho fora da pasta autorizada.')
        # Do not allow symlink operations, including links back into the allowed root.
        current = self.root
        for part in raw.parts:
            current /= part
            if current.is_symlink():
                raise HTTPException(403, 'Links simbólicos não são permitidos no explorador.')
        if not allow_root and result == self.root:
            raise HTTPException(403, 'A pasta raiz não pode ser alterada.')
        return result

    def listing(self, relative):
        folder = self.path(relative)
        if not folder.is_dir():
            raise HTTPException(404, 'Pasta não encontrada.')
        entries = []
        for item in folder.iterdir():
            if item.is_symlink() or item.resolve() == self.state or item.resolve().is_relative_to(self.state):
                continue
            try:
                stat = item.stat()
                entries.append(dict(name=item.name, path=item.relative_to(self.root).as_posix(), directory=item.is_dir(), size=stat.st_size, modified=stat.st_mtime, version=stat.st_mtime_ns))
            except (PermissionError, FileNotFoundError):
                continue
        return sorted(entries, key=lambda item: (not item['directory'], item['name'].lower()))

    def text(self, relative):
        target = self.path(relative, False)
        if not target.is_file():
            raise HTTPException(404, 'Arquivo não encontrado.')
        if target.stat().st_size > MAX_TEXT:
            raise HTTPException(413, 'O editor aceita arquivos de até 2 MiB.')
        version = target.stat().st_mtime_ns
        data = target.read_bytes()
        if len(data) > MAX_TEXT:
            raise HTTPException(413, 'O editor aceita arquivos de até 2 MiB.')
        if target.stat().st_mtime_ns != version:
            raise HTTPException(409, 'O arquivo mudou durante a leitura. Abra novamente.')
        if b'\x00' in data:
            raise HTTPException(415, 'Arquivo binário. Use o download.')
        try:
            return dict(content=data.decode('utf-8'), version=version)
        except UnicodeDecodeError:
            raise HTTPException(415, 'O editor aceita somente texto UTF-8.') from None

    def save(self, relative, content, version):
        target = self.path(relative, False)
        data = content.encode('utf-8')
        if len(data) > MAX_TEXT:
            raise HTTPException(413, 'O editor aceita arquivos de até 2 MiB.')
        if not target.is_file() or str(target.stat().st_mtime_ns) != str(version):
            raise HTTPException(409, 'O arquivo mudou. Reabra antes de salvar para evitar perda de dados.')
        mode = target.stat().st_mode & 0o777
        descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix='.tc-edit-')
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return target.stat().st_mtime_ns
