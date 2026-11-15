import asyncio
import json
import math
import os
import platform
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


def process_cpu_time(text, pid):
    prefix, opening, rest = text.partition('(')
    _, closing, tail = rest.rpartition(')')
    if not opening or not closing or int(prefix.strip()) != pid:
        raise ValueError('Identidade de processo inválida')
    fields = tail.split()  # Field 3 (state) follows comm, which may contain spaces and parentheses.
    user, system, started = int(fields[11]), int(fields[12]), int(fields[19])
    if min(user, system, started) < 0:
        raise ValueError('Contadores de processo inválidos')
    # utime/stime already include this process's threads; never add cutime/cstime or /task entries.
    return started, user + system


def process_cpu_snapshot():
    uid = os.getuid()
    counters = {}
    for entry in Path('/proc').iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            with (entry / 'stat').open(encoding='utf-8', errors='replace') as stream:
                pid = int(entry.name)
                started, ticks = process_cpu_time(stream.read(), pid)
                if os.fstat(stream.fileno()).st_uid != uid:
                    # Non-dumpable processes may expose root-owned proc files despite having our UID.
                    owners = [line.split()[1] for line in (entry / 'status').read_text().splitlines() if line.startswith('Uid:')]
                    if not owners or int(owners[0]) != uid:
                        continue
                    same_start, ticks = process_cpu_time((entry / 'stat').read_text(), pid)
                    if same_start != started:
                        continue
                counters[pid, started] = ticks
        except (OSError, ValueError, IndexError):
            continue  # A process may exit or become inaccessible while /proc is being scanned.
    return counters


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
        self.cpu = None
        self.cpu_source = None
        self.cores = os.cpu_count()
        self.clock_ticks = None
        try:
            configured = os.sysconf('SC_NPROCESSORS_CONF')
            self.cores = configured if configured > 0 else self.cores
            ticks = os.sysconf('SC_CLK_TCK')
            self.clock_ticks = ticks if ticks > 0 else None
        except (AttributeError, OSError, ValueError):
            pass
        self.cpu_processes = 0
        self.cpu_message = 'Aguardando duas amostras de processos do Termux.'
        self.battery = dict(percentage=None, temperature=None, status='UNKNOWN', charging=None, available=False, message='Aguardando Termux:API')

    async def update_cpu(self):
        self.cpu = None
        try:
            if not self.clock_ticks or not self.cores:
                raise OSError('Temporização de processos indisponível')
            now = time.monotonic()
            try:
                boot = time.clock_gettime(time.CLOCK_BOOTTIME) * self.clock_ticks
            except (AttributeError, OSError, ValueError):
                boot = None
            current = process_cpu_snapshot()
            if not current:
                raise OSError('Nenhum processo acessível')
        except (OSError, AttributeError):
            self.previous = None
            self.cpu_source = None
            self.cpu_processes = 0
            self.cpu_message = 'Não foi possível ler os processos do usuário Termux.'
            return
        self.cpu_source = 'proc_pid_stat'
        self.cpu_processes = len(current)
        self.cpu_message = 'Aguardando duas amostras de processos do Termux.'
        if self.previous:
            clock, previous_boot, previous = self.previous
            elapsed = now - clock
            if 0 < elapsed <= 10:
                used = 0
                for identity, ticks in current.items():
                    if identity in previous:
                        used += max(0, ticks - previous[identity])
                    elif previous_boot is not None and identity[1] >= previous_boot:
                        used += ticks  # This process was born inside the measured interval.
                self.cpu = round(max(0, min(100, 100 * used / self.clock_ticks / elapsed / self.cores)), 1)
                self.cpu_message = 'Soma dos processos acessíveis do usuário Termux; não é a CPU global do Android.'
        self.previous = now, boot, current

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
        return dict(cpu=self.cpu, cpu_cores=self.cores, cpu_source=self.cpu_source, cpu_processes=self.cpu_processes, cpu_message=self.cpu_message, battery=self.battery.copy(), memory=memory, disk=dict(total=disk.total, used=disk.used, percent=round(disk.used / disk.total * 100, 1)), uptime=uptime, server_uptime=uptime, device=platform.node(), platform=platform.system(), time=time.time())


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
