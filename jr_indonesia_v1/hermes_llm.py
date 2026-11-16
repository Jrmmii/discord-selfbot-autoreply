"""Supported Hermes CLI transport with bounded calls and final-only output."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time


def parse_result(stdout, returncode):
    if returncode:
        return None
    final = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        if event.get('type') in ('tool_use', 'tool_result'):
            return None
        if event.get('type') == 'result':
            final = event
    if not final or final.get('exit_code') != 0 or final.get('error'):
        return None
    text = final.get('text')
    return text.strip() if isinstance(text, str) and text.strip() else None


def command_for(executable, settings):
    return [executable, 'chat', '--oneshot', '--query-file', '-',
            '--format', 'stream-json', '--ignore-rules', '--source', 'tool',
            '--toolsets', 'none', '--max-turns', '1', '--run-budget', '45',
            '--reasoning', 'low', '--provider', settings.hermes_provider,
            '--model', settings.hermes_model]


class HermesLLM:
    def __init__(self, settings, stop, timeout=65):
        self.settings = settings
        self.stop = stop
        self.timeout = timeout
        self.cooldown_until = 0

    @staticmethod
    def _kill(proc):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()

    def generate(self, system, user):
        if self.stop.is_set() or time.time() < self.cooldown_until:
            return None
        executable = shutil.which('hermes')
        if not executable:
            candidate = Path.home() / '.local/bin/hermes'
            executable = str(candidate) if candidate.is_file() else None
        if not executable:
            self.cooldown_until = time.time() + 300
            return None
        scratch = Path(os.environ.get('TMPDIR') or (Path.home() / '.hermes/cache/scratch'))
        env = dict(os.environ)
        # Discord authentication is not needed by the model client.
        env.pop('DISCORD_TOKEN', None)
        started = time.monotonic()
        proc = None
        try:
            scratch.mkdir(parents=True, exist_ok=True)
            proc = subprocess.Popen(command_for(executable, self.settings),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, cwd=str(scratch), start_new_session=True, env=env)
            first = True
            while True:
                if self.stop.is_set() or time.monotonic() - started >= self.timeout:
                    self._kill(proc)
                    self.cooldown_until = time.time() + 60
                    return None
                try:
                    if first:
                        first = False
                        stdout, _ = proc.communicate(input=system + '\n\n' + user, timeout=.5)
                    else:
                        stdout, _ = proc.communicate(timeout=.5)
                    break
                except subprocess.TimeoutExpired:
                    continue
            text = parse_result(stdout, proc.returncode)
            if text is None:
                self.cooldown_until = time.time() + 60
            return text
        except (OSError, subprocess.SubprocessError):
            if proc is not None and proc.poll() is None:
                self._kill(proc)
            self.cooldown_until = time.time() + 60
            return None
