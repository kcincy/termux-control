import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


def state_directory():
    return Path(os.environ.get('TERMUX_CONTROL_STATE', '~/.local/share/termux-control')).expanduser().resolve()


@dataclass
class Config:
    state: Path
    root: Path
    username: str
    password_hash: str
    origins: list[str]
    host: str = '127.0.0.1'
    port: int = 8765
    session_hours: int = 12
    wake_lock: bool = False

    @property
    def secure_cookie(self):
        return all(urlsplit(origin).scheme == 'https' for origin in self.origins)

    def validate(self):
        self.state = self.state.resolve()
        self.root = self.root.expanduser().resolve()
        if not self.username or not self.password_hash or not self.origins:
            raise ValueError('Execute python -m backend.cli setup antes de iniciar.')
        if not 1 <= self.port <= 65535 or not 1 <= self.session_hours <= 24:
            raise ValueError('Porta ou duração de sessão inválida.')
        for origin in self.origins:
            url = urlsplit(origin)
            if url.scheme not in ('http', 'https') or not url.hostname or url.path or url.query or url.fragment or url.username:
                raise ValueError('Informe origens exatas, sem caminho, por exemplo http://100.64.0.2:8765.')
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state.chmod(0o700)
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root == self.state or self.state in self.root.parents:
            raise ValueError('A pasta de arquivos não pode ficar dentro da pasta de credenciais.')
        return self


def load_config():
    state = state_directory()
    try:
        data = json.loads((state / 'config.json').read_text())
    except FileNotFoundError:
        raise RuntimeError('Configuração ausente. Execute: python -m backend.cli setup') from None
    return Config(state=state, root=Path(data.pop('root')), **data).validate()
