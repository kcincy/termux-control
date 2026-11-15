import asyncio
import os
import platform
import shutil
import time
from pathlib import Path


class Metrics:
    def __init__(self, root):
        self.root = root
        self.started = time.monotonic()
        self.previous = None

    def sample(self):
        cpu = memory = uptime = None
        try:
            fields = [int(v) for v in Path('/proc/stat').read_text().splitlines()[0].split()[1:9]]
            total, idle = sum(fields), fields[3] + fields[4]
            if self.previous and total > self.previous[0]:
                cpu = round(100 * (1 - (idle - self.previous[1]) / (total - self.previous[0])), 1)
            self.previous = total, idle
        except (OSError, ValueError, IndexError):
            pass
        try:
            fields = {line.split(':')[0]: int(line.split()[1]) * 1024 for line in Path('/proc/meminfo').read_text().splitlines()}
            total = fields['MemTotal']
            available = fields.get('MemAvailable', fields.get('MemFree', 0))
            memory = dict(total=total, used=total - available, percent=round((total - available) / total * 100, 1))
        except (OSError, ValueError, KeyError):
            pass
        try:
            uptime = float(Path('/proc/uptime').read_text().split()[0])
        except (OSError, ValueError):
            pass
        disk = shutil.disk_usage(self.root)
        return dict(cpu=cpu, memory=memory, disk=dict(total=disk.total, used=disk.used, percent=round(disk.used / disk.total * 100, 1)), uptime=uptime, server_uptime=time.monotonic() - self.started, device=platform.node(), platform=platform.system(), time=time.time())


async def tailscale_status():
    # The Android VPN app is independent of Termux; absence of a CLI says nothing about its connection.
    executable = shutil.which('tailscale')
    if not executable:
        return dict(status='unknown', message='App Android: verificar no Tailscale', ip=None)
    try:
        import json
        process = await asyncio.create_subprocess_exec(executable, 'status', '--json', stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            output, _ = await asyncio.wait_for(process.communicate(), 3)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise
        data = json.loads(output)
        return dict(status='online' if data.get('BackendState') == 'Running' else 'offline', ip=next(iter(data.get('TailscaleIPs', [])), None), message='Verificado pela CLI local')
    except (OSError, ValueError, asyncio.TimeoutError):
        return dict(status='unknown', ip=None, message='Não foi possível consultar a CLI')


async def processes():
    if os.name != 'posix':
        return dict(available=False, items=[], message='Listagem POSIX disponível no Termux/Linux.')
    try:
        proc = await asyncio.create_subprocess_exec('ps', '-u', str(os.getuid()), '-o', 'pid=,comm=,etime=', stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            output, _ = await asyncio.wait_for(proc.communicate(), 3)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        items = []
        for line in output.decode(errors='replace').splitlines():
            parts = line.split(None, 2)
            if len(parts) == 3 and parts[0].isdigit():
                items.append(dict(pid=int(parts[0]), name=parts[1], elapsed=parts[2]))
        return dict(available=proc.returncode == 0, items=items[:200], message='Somente o usuário do Termux; o Android pode restringir esta lista.')
    except (OSError, asyncio.TimeoutError):
        return dict(available=False, items=[], message='O sistema restringiu a listagem de processos.')
