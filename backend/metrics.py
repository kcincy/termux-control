import asyncio
import json
import math
import os
import platform
import re
import signal
import shutil
import time
from pathlib import Path


async def command_output(*command, timeout=4):
    process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env={**os.environ, 'LC_ALL': 'C'}, start_new_session=os.name == 'posix')
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout)
        if process.returncode:
            raise OSError('Consulta indisponível')
        return output.decode(errors='replace')
    except (asyncio.TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            try:
                if os.name == 'posix':
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
        await process.communicate()
        raise


def top_cpu(output):
    # Android top reports e.g. 800%cpu; normalize the aggregate, never sum visible processes.
    samples = re.findall(r'([\d.]+)%cpu[^\n]*?([\d.]+)%idle', output, re.I)
    if samples:
        total, idle = map(float, samples[-1])
        return round(max(0, min(100, 100 * (total - idle) / total)), 1) if total > 0 else None
    samples = re.findall(r'%Cpu\(s\):[^\n]*?([\d.]+)\s+id\b', output, re.I)
    return round(max(0, min(100, 100 - float(samples[-1]))), 1) if samples else None


def battery_data(data):
    def number(value, minimum, maximum):
        try:
            result = float(str(value).replace(',', '.'))
            return round(result, 1) if math.isfinite(result) and minimum <= result <= maximum else None
        except (ValueError, TypeError):
            return None
    percentage = number(data.get('percentage'), 0, 100)
    temperature = number(data.get('temperature'), -30, 100)
    status = data.get('status')
    status = status if status in ('CHARGING', 'DISCHARGING', 'FULL', 'NOT_CHARGING') else 'UNKNOWN'
    return dict(percentage=percentage, temperature=temperature, status=status, charging=None if status == 'UNKNOWN' else status == 'CHARGING', available=percentage is not None or temperature is not None or status != 'UNKNOWN', message='Dados do Termux:API')


class Metrics:
    def __init__(self, root):
        self.root = root
        self.started = time.monotonic()
        self.previous = None
        self.previous_idle = None
        self.cpu = None
        self.cpu_source = None
        self.cores = os.cpu_count()
        self.battery = dict(percentage=None, temperature=None, status='UNKNOWN', charging=None, available=False, message='Aguardando Termux:API')

    async def update_cpu(self):
        try:
            lines = Path('/proc/stat').read_text().splitlines()
            fields = [int(v) for v in lines[0].split()[1:9]]
            if lines[0].split()[0] != 'cpu' or len(fields) < 5:
                raise ValueError('Contadores inválidos')
            self.cores = sum(bool(re.match(r'^cpu\d+\s', line)) for line in lines) or self.cores
            total, idle = sum(fields), fields[3] + fields[4]
            self.cpu = None
            if self.previous:
                elapsed, idle_elapsed = total - self.previous[0], idle - self.previous[1]
                if elapsed > 0 and idle_elapsed >= 0:
                    self.cpu = round(max(0, min(100, 100 * (elapsed - idle_elapsed) / elapsed)), 1)
            self.previous = total, idle
            self.cpu_source = 'proc_stat'
            return
        except (OSError, ValueError, IndexError):
            self.previous = None
        try:
            # /proc/uptime's second field is idle seconds summed across CPUs, not a load average.
            wall, idle = map(float, Path('/proc/uptime').read_text().split()[:2])
            cores = os.sysconf('SC_NPROCESSORS_ONLN')
            clock = time.monotonic()
            if not all(math.isfinite(value) and value >= 0 for value in (wall, idle)) or cores < 1:
                raise ValueError('Contadores inválidos')
            self.cpu = None
            if self.previous_idle and self.previous_idle[2] == cores:
                elapsed = wall - self.previous_idle[0]
                idle_elapsed = idle - self.previous_idle[1]
                awake = clock - self.previous_idle[3]
                if 0 < elapsed <= 10 and idle_elapsed >= 0 and abs(elapsed - awake) <= max(0.2, elapsed * 0.1):
                    self.cpu = round(max(0, min(100, 100 * (1 - idle_elapsed / (elapsed * cores)))), 1)
            self.previous_idle = wall, idle, cores, clock
            self.cores = cores
            self.cpu_source = 'proc_uptime'
            return
        except (OSError, ValueError, IndexError, AttributeError):
            self.previous_idle = None
        executable = '/system/bin/top' if Path('/system/bin/top').is_file() else shutil.which('top')
        if executable and os.name == 'posix':
            try:
                self.cpu = top_cpu(await command_output(executable, '-b', '-n', '2', '-d', '1', '-p', str(os.getpid())))
                self.cpu_source = 'top' if self.cpu is not None else None
            except (OSError, ValueError, asyncio.TimeoutError):
                pass
            else:
                return
        self.cpu = self.cpu_source = None

    async def monitor_cpu(self):
        while True:
            await self.update_cpu()
            await asyncio.sleep(1)

    async def update_battery(self):
        message = 'Instale o app Termux:API da mesma origem do Termux e o pacote termux-api.'
        if shutil.which('termux-battery-status'):
            try:
                data = json.loads(await command_output('termux-battery-status', timeout=8))
                if not isinstance(data, dict):
                    raise ValueError('Resposta inválida')
                self.battery = battery_data(data)
                return
            except (OSError, ValueError, asyncio.TimeoutError):
                message = 'Termux:API não respondeu. Confira o app, permissões e teste termux-battery-status.'
        self.battery = dict(percentage=None, temperature=None, status='UNKNOWN', charging=None, available=False, message=message)

    async def monitor_battery(self):
        while True:
            await self.update_battery()
            await asyncio.sleep(30)

    def sample(self):
        memory = None
        try:
            fields = {line.split(':')[0]: int(line.split()[1]) * 1024 for line in Path('/proc/meminfo').read_text().splitlines()}
            total = fields['MemTotal']
            available = fields.get('MemAvailable', fields.get('MemFree', 0))
            memory = dict(total=total, used=total - available, percent=round((total - available) / total * 100, 1))
        except (OSError, ValueError, KeyError):
            pass
        disk = shutil.disk_usage(self.root)
        uptime = max(0, time.monotonic() - self.started)
        return dict(cpu=self.cpu, cpu_cores=self.cores, cpu_source=self.cpu_source, battery=self.battery.copy(), memory=memory, disk=dict(total=disk.total, used=disk.used, percent=round(disk.used / disk.total * 100, 1)), uptime=uptime, server_uptime=uptime, device=platform.node(), platform=platform.system(), time=time.time())


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
