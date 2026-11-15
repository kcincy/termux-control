"""Fast-forward update of the installed distribution; never runs setup or kills workload tmux."""
import hashlib
import importlib.metadata
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from backend.config import load_config

ORIGINS = {'https://github.com/kcincy/termux-control', 'https://github.com/kcincy/termux-control.git', 'git@github.com:kcincy/termux-control.git', 'ssh://git@github.com/kcincy/termux-control.git'}


def git(repo, *args):
    return subprocess.check_output(['git', *args], cwd=repo, text=True, stderr=subprocess.PIPE, timeout=120, env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'}).strip()


def execute(repo, *args):
    subprocess.run(args, cwd=repo, check=True)


@contextmanager
def update_lock(repo):
    import fcntl
    private = repo / '.local'
    private.mkdir(exist_ok=True, mode=0o700)
    with (private / 'update.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Outra atualização já está em andamento.') from None
        yield


def service_running(repo, config):
    return subprocess.run(['tmux', '-S', str(config.state / 'service.sock'), 'has-session', '-t', 'tc-server'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def service(repo, config, action):
    subprocess.run([sys.executable, '-m', 'backend.cli', 'service', action], cwd=repo, env={**os.environ, 'TERMUX_CONTROL_STATE': str(config.state)}, check=True)


def wait_ready(config):
    address = '127.0.0.1' if config.host == '0.0.0.0' else '::1' if config.host == '::' else config.host
    for _ in range(20):
        try:
            with socket.create_connection((address, config.port), timeout=1):
                return
        except OSError:
            time.sleep(0.25)
    raise RuntimeError('O serviço reiniciado não abriu a porta esperada.')


def update(repo, config):
    with update_lock(repo):
        if git(repo, 'remote', 'get-url', 'origin') not in ORIGINS:
            raise RuntimeError('O origin deve apontar para kcincy/termux-control. Nenhum remoto será alterado.')
        if git(repo, 'branch', '--show-current') != 'main':
            raise RuntimeError('Selecione a branch main antes de atualizar.')
        if git(repo, 'status', '--porcelain', '--untracked-files=no'):
            raise RuntimeError('Existem alterações locais em arquivos versionados. Preserve-as antes de atualizar; nada foi sobrescrito.')
        old = git(repo, 'rev-parse', 'HEAD')
        print('Buscando alterações do mesmo repositório...', flush=True)
        git(repo, 'fetch', '--no-tags', 'origin', 'main')
        target = git(repo, 'rev-parse', 'FETCH_HEAD')
        try:
            git(repo, 'merge-base', '--is-ancestor', old, target)
        except subprocess.CalledProcessError:
            raise RuntimeError('O histórico local divergiu do GitHub. Atualização cancelada sem substituir commits.') from None
        changed = git(repo, 'diff', '--name-only', old, target).splitlines()
        for name in changed:
            path = Path(name)
            if any(part in ('.env', '.venv', '.local') or part.startswith('.env.') for part in path.parts) or path.name == 'config.json' or path.suffix in ('.key', '.pem', '.p12', '.pfx'):
                raise RuntimeError('A atualização tentou alterar um caminho reservado a dados privados; cancelada.')
            try:
                state_path = config.state.relative_to(repo).as_posix()
            except ValueError:
                state_path = None
            if state_path and (name == state_path or name.startswith(state_path + '/')):
                raise RuntimeError('A atualização tentou alterar a pasta de configuração; cancelada.')
        marker_file = repo / '.local/update.json'
        marker = json.loads(marker_file.read_text()) if marker_file.exists() else {}
        requirements = git(repo, 'show', f'{target}:requirements.txt')
        requirements_hash = hashlib.sha256(requirements.encode()).hexdigest()
        dependency_change = marker.get('requirements') != requirements_hash
        deployed = marker.get('commit')
        runtime_change = True
        if deployed:
            try:
                affected = git(repo, 'diff', '--name-only', deployed, target).splitlines()
                runtime_change = any(name.startswith(('backend/', 'dist/', 'public/', 'scripts/')) or name == 'requirements.txt' for name in affected)
            except subprocess.CalledProcessError:
                pass
        if os.environ.get('PREFIX') == '/data/data/com.termux/files/usr' and not shutil.which('termux-battery-status'):
            execute(repo, 'pkg', 'install', '-y', 'termux-api')
        was_running = service_running(repo, config)
        stopped = merged = dependencies_touched = False
        snapshot = repo / '.local/requirements-before-update.txt'
        try:
            if was_running and (runtime_change or dependency_change):
                stopped = True
                service(repo, config, 'stop')
            if old != target:
                git(repo, 'merge', '--ff-only', target)
                merged = True
            if not (repo / 'dist/index.html').is_file():
                raise RuntimeError('Frontend compilado ausente na atualização.')
            if dependency_change:
                versions = sorted({f'{d.metadata["Name"]}=={d.version}' for d in importlib.metadata.distributions() if d.metadata.get('Name') and d.metadata['Name'].lower() != 'pip'})
                snapshot.write_text('\n'.join(versions) + '\n')
                dependencies_touched = True
                execute(repo, sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check', '-r', 'requirements.txt')
            execute(repo, sys.executable, '-m', 'pip', 'check')
            execute(repo, sys.executable, '-m', 'compileall', '-q', 'backend')
            if stopped:
                service(repo, config, 'start')
                wait_ready(config)
            temporary = marker_file.with_suffix('.tmp')
            temporary.write_text(json.dumps(dict(commit=target, requirements=requirements_hash)))
            os.replace(temporary, marker_file)
        except BaseException:
            print('Falha na atualização. Tentando recuperar a versão anterior...', file=sys.stderr)
            try:
                if stopped and service_running(repo, config):
                    service(repo, config, 'stop')
                if merged:
                    if git(repo, 'rev-parse', 'HEAD') != target:
                        raise RuntimeError('O histórico mudou durante a atualização; recuperação manual necessária.')
                    git(repo, 'reset', '--merge', old)
                if dependencies_touched:
                    execute(repo, sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check', '-r', str(snapshot))
                if stopped:
                    service(repo, config, 'start')
                    wait_ready(config)
            except Exception:
                print('Recuperação incompleta. Configuração e .env foram preservados. Verifique a rede/dependências e execute bash scripts/start.sh; o snapshot de dependências está em .local/requirements-before-update.txt.', file=sys.stderr)
            raise
        print(f'Atualização concluída: {target[:12]}. Configuração, credenciais, .env e tmux de trabalho preservados.')
        if not was_running:
            print('O dashboard estava parado e continua parado. Para iniciar: bash scripts/start.sh')
        elif not stopped:
            print('Nenhuma mudança de execução: o servidor não precisou reiniciar.')


def main():
    try:
        update(Path.cwd().resolve(), load_config())
    except (Exception, KeyboardInterrupt) as error:
        if isinstance(error, subprocess.CalledProcessError):
            print('Atualização não concluída. Um comando falhou; confira a rede, os conflitos locais e as mensagens acima.', file=sys.stderr)
        else:
            print(f'Atualização não concluída: {error}', file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(str(error.stderr)[-2000:], file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
