import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .config import Config, load_config, state_directory
from .security import Store, password_hash

PROJECT = Path(__file__).resolve().parent.parent


def save(config):
    config.validate()
    data = vars(config).copy()
    data.pop('state')
    data['root'] = str(data['root'])
    target = config.state / 'config.json'
    temporary = config.state / 'config.tmp'
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(data, stream, indent=2)
        stream.write('\n')
    temporary.chmod(0o600)
    os.replace(temporary, target)


def setup():
    state = state_directory()
    previous = load_config() if (state / 'config.json').exists() else None
    print('\nTERMUX CONTROL · configuração privada\n')
    if previous:
        print('Reconfigurar revoga todas as sessões de acesso existentes.')
    username = input(f'Usuário [{previous.username if previous else "admin"}]: ').strip() or (previous.username if previous else 'admin')
    password = getpass.getpass('Nova senha (mínimo 14 caracteres; nada será exibido): ')
    if len(password) < 14 or len(password) > 1024 or password != getpass.getpass('Repita a senha: '):
        raise ValueError('Senhas diferentes ou comprimento inválido (14–1024 caracteres).')
    root = Path(input(f'Pasta autorizada [{previous.root if previous else Path.home() / "termux-workspace"}]: ').strip() or str(previous.root if previous else Path.home() / 'termux-workspace'))
    print('\nO padrão 127.0.0.1 permite acesso somente no celular.')
    print('No Android, o IP Tailscale pode não existir como interface local para bind.')
    print('Use 0.0.0.0 apenas se necessário: isso também escuta no Wi-Fi.')
    host = input(f'Interface [{previous.host if previous else "127.0.0.1"}]: ').strip() or (previous.host if previous else '127.0.0.1')
    port = int(input(f'Porta [{previous.port if previous else 8765}]: ').strip() or (previous.port if previous else 8765))
    default_origins = ','.join(previous.origins) if previous else f'http://127.0.0.1:{port},http://localhost:{port}'
    print('Adicione a URL Tailscale exata: http://100.x.y.z:porta. Sem barra final.')
    origins = [item.strip() for item in (input(f'Origens permitidas, separadas por vírgula [{default_origins}]: ').strip() or default_origins).split(',')]
    if any(origin.startswith('http://') for origin in origins):
        print('HTTP depende da criptografia Tailscale entre os dispositivos. Na LAN, HTTP transmite a senha em claro.')
        if input('Confirma HTTP somente em loopback ou na rede privada Tailscale? [s/N]: ').lower() != 's':
            raise ValueError('Use origens HTTPS e configure certificado/chave para o servidor.')
    wake_lock = input('Solicitar termux-wake-lock ao iniciar? [s/N]: ').lower() == 's'
    config = Config(state, root, username, password_hash(password), origins, host, port, wake_lock=wake_lock)
    save(config)
    store = Store(config)
    with store.db:
        store.db.execute('DELETE FROM sessions')
    store.db.close()
    print('\nConfiguração salva com permissões privadas. Inicie: bash scripts/start.sh')


def service(action):
    config = load_config()
    tmux = shutil.which('tmux')
    if not tmux or os.name != 'posix':
        raise RuntimeError('O serviço requer Termux/Linux e tmux. Para desenvolvimento: python -m backend.cli serve')
    base = [tmux, '-S', str(config.state / 'service.sock')]

    def running():
        return subprocess.run([*base, 'has-session', '-t', 'tc-server'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0

    if action in ('stop', 'restart') and running():
        subprocess.run([*base, 'send-keys', '-t', 'tc-server', 'C-c'], check=True)
        for _ in range(50):
            if not running():
                break
            time.sleep(0.2)
        else:
            raise RuntimeError('Servidor não encerrou em 10 segundos. Inspecione tmux antes de tentar novamente.')
        if config.wake_lock and shutil.which('termux-wake-unlock'):
            subprocess.run(['termux-wake-unlock'], check=False)
    if action in ('start', 'restart') and not running():
        if config.wake_lock and shutil.which('termux-wake-lock'):
            subprocess.run(['termux-wake-lock'], check=True)
        subprocess.run([*base, 'new-session', '-d', '-s', 'tc-server', '-c', str(PROJECT), sys.executable, '-m', 'backend.cli', 'serve'], check=True)
        time.sleep(1)
        if not running():
            raise RuntimeError('Servidor encerrou ao iniciar. Execute .venv/bin/python -m backend.cli serve para ver o erro.')
    print('Servidor: ' + ('ativo' if running() else 'parado'))
    if running():
        print('Endereços autorizados (conectividade depende da interface/VPN):')
        print('\n'.join(config.origins))
        print(f'Diagnóstico: tmux -S {config.state / "service.sock"} attach -t tc-server')


def main():
    parser = argparse.ArgumentParser(description='TERMUX CONTROL')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('setup')
    server = commands.add_parser('serve')
    server.add_argument('--certfile')
    server.add_argument('--keyfile')
    manager = commands.add_parser('service')
    manager.add_argument('action', choices=['start', 'stop', 'restart', 'status'])
    commands.add_parser('doctor')
    args = parser.parse_args()
    try:
        if args.command == 'setup':
            setup()
        elif args.command == 'service':
            service(args.action)
        elif args.command == 'doctor':
            config = load_config()
            print(json.dumps(dict(python=sys.version.split()[0], platform=sys.platform, tmux=shutil.which('tmux'), node=shutil.which('node'), frontend=(PROJECT / 'dist/index.html').exists(), root=str(config.root), state=str(config.state), host=config.host, origins=config.origins), indent=2))
        else:
            import uvicorn
            config = load_config()
            certfile = args.certfile or os.environ.get('TERMUX_CONTROL_CERTFILE')
            keyfile = args.keyfile or os.environ.get('TERMUX_CONTROL_KEYFILE')
            if config.secure_cookie and not (certfile and keyfile):
                raise ValueError('Origens HTTPS exigem --certfile e --keyfile ou TERMUX_CONTROL_CERTFILE/KEYFILE.')
            from .app import create_app
            uvicorn.run(create_app(config), host=config.host, port=config.port, workers=1, ws='wsproto', ws_max_size=32768, proxy_headers=False, access_log=False, ssl_certfile=certfile, ssl_keyfile=keyfile)
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print(f'Erro: {error}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
