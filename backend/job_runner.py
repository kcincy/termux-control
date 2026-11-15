"""Detached tmux job with bounded logs and a bounded shutdown of its process group."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

LIMIT = 1024 * 1024


def run(log_path, status_path, command):
    log_path, status_path = Path(log_path), Path(status_path)
    child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True, bufsize=0)
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        if not stopping:
            stopping = True
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            signal.alarm(5)

    def force_stop(signum, frame):
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, stop)
    signal.signal(signal.SIGALRM, force_stop)
    def save_status(state):
        temporary = status_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(state))
        os.replace(temporary, status_path)

    save_status(dict(pid=child.pid, started=time.time(), exit_code=None))
    try:
        with log_path.open('ab', buffering=0) as log:
            while chunk := child.stdout.read(4096):
                if log.tell() + len(chunk) > LIMIT:
                    log.seek(0)
                    log.truncate()
                    log.write(b'[log anterior descartado: limite de 1 MiB]\n')
                log.write(chunk)
        code = child.wait()
    finally:
        signal.alarm(0)
        force_stop(None, None)
        child.wait()
    state = json.loads(status_path.read_text())
    state.update(exit_code=code, finished=time.time())
    save_status(state)


if __name__ == '__main__':
    run(sys.argv[1], sys.argv[2], sys.argv[3:])
