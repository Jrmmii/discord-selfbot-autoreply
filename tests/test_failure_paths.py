"""Hermes and delivery failure-path tests; no live requests."""
import io
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from jr_indonesia_v1 import hermes_llm, runner
from test_public_runner import FakeReadAPI, env_values, message


class AdapterTests(unittest.TestCase):
    def test_final_only_no_reasoning_or_tools(self):
        result = {'type': 'result', 'exit_code': 0, 'text': 'with curry?'}
        self.assertEqual(hermes_llm.parse_result(json.dumps(result), 0), 'with curry?')
        for output, code in [
            ('{"type":"text","text":"partial"}', 0),
            (json.dumps(result), 1),
            ('{"type":"result","exit_code":1,"text":"failure"}', 0),
            ('{"type":"tool_use"}\n' + json.dumps(result), 0),
            ('not json', 0),
        ]:
            self.assertIsNone(hermes_llm.parse_result(output, code))

    def test_stdin_transport_hides_discord_token(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = runner.Settings.from_env(env_values(directory))
            stop = runner.StopFlag()
            client = hermes_llm.HermesLLM(settings, stop)
            with patch.object(hermes_llm.shutil, 'which', return_value='/bin/hermes'), patch.object(hermes_llm.subprocess, 'Popen') as popen, patch.dict(hermes_llm.os.environ, {'DISCORD_TOKEN': 'test-secret', 'TMPDIR': directory}):
                proc = popen.return_value
                proc.returncode = 0
                proc.communicate.return_value = (json.dumps({'type': 'result', 'exit_code': 0, 'text': 'with curry?'}), '')
                self.assertEqual(client.generate('rules', '$(untrusted)'), 'with curry?')
                args, kwargs = popen.call_args
                self.assertNotIn('shell', kwargs)
                self.assertNotIn('DISCORD_TOKEN', kwargs['env'])
                self.assertIn('--query-file', args[0])
                self.assertIn('--oneshot', args[0])
                self.assertEqual(proc.communicate.call_args.kwargs['input'], 'rules\n\n$(untrusted)')

    def test_timeout_kills_process_and_backs_off(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = runner.Settings.from_env(env_values(directory))
            client = hermes_llm.HermesLLM(settings, runner.StopFlag(), timeout=-1)
            with patch.object(hermes_llm.shutil, 'which', return_value='/bin/hermes'), patch.object(hermes_llm.subprocess, 'Popen') as popen, patch.object(hermes_llm.os, 'killpg') as kill, patch.dict(hermes_llm.os.environ, {'TMPDIR': directory}):
                popen.return_value.pid = 123
                self.assertIsNone(client.generate('rules', 'target'))
                kill.assert_called_once_with(123, hermes_llm.signal.SIGKILL)
                self.assertGreater(client.cooldown_until, time.time() + 50)
                self.assertIsNone(client.generate('rules', 'target'))
                popen.assert_called_once()

    def test_stopped_adapter_does_not_spawn(self):
        settings = runner.Settings.from_env(env_values('.runtime/test'))
        stop = runner.StopFlag()
        stop.request()
        with patch.object(hermes_llm.subprocess, 'Popen') as popen:
            self.assertIsNone(hermes_llm.HermesLLM(settings, stop).generate('rules', 'target'))
        popen.assert_not_called()


class DeliveryTests(unittest.TestCase):
    def setup_case(self, directory):
        settings = runner.Settings.from_env(env_values(directory))
        target = message('1', 'Chicken katsu')
        api = FakeReadAPI({'id': settings.user_id, 'username': settings.username}, [target])
        api.allow_posts = True
        state = runner.new_state(runner.utc_day_key())
        return settings, target, api, state

    def test_success_persists_exact_target_and_count(self):
        with tempfile.TemporaryDirectory() as directory:
            settings, target, api, state = self.setup_case(directory)
            with patch.object(runner, 'log'):
                self.assertTrue(runner.send_one(settings, api, state, target, 'with curry?', runner.StopFlag()))
            saved = runner.load_state(settings)
            self.assertEqual(saved['day_count'], 1)
            self.assertEqual(saved['total_sent'], 1)
            self.assertEqual(saved['replied_ids'], ['1', 'response-id'])
            self.assertEqual(saved['last_replies'], ['with curry?'])

    def test_cap_prevents_any_network_call(self):
        with tempfile.TemporaryDirectory() as directory:
            settings, target, api, state = self.setup_case(directory)
            state['day_count'] = runner.MAX_DAILY
            self.assertFalse(runner.send_one(settings, api, state, target, 'with curry?', runner.StopFlag()))
            self.assertEqual(api.calls, [])

    def test_edited_and_stale_targets_never_post(self):
        with tempfile.TemporaryDirectory() as directory:
            settings, target, api, state = self.setup_case(directory)
            for changed in (message('1', 'Different topic'), message('1', 'Chicken katsu', age=100)):
                api.messages = [changed]
                api.calls = []
                self.assertFalse(runner.send_one(settings, api, state, target, 'with curry?', runner.StopFlag()))
                self.assertTrue(all(method == 'GET' for method, _ in api.calls))

    def test_rate_limit_honors_retry_after_without_counting_send(self):
        with tempfile.TemporaryDirectory() as directory:
            settings, target, api, state = self.setup_case(directory)
            api.send_message = lambda *args: (429, {'retry_after': 120})
            with self.assertRaises(runner.DeliveryError) as caught:
                runner.send_one(settings, api, state, target, 'with curry?', runner.StopFlag())
            self.assertEqual(caught.exception.retry_after, 120)
            self.assertEqual(state['day_count'], 0)
            self.assertFalse((Path(directory) / 'state.json').exists())

    def test_ambiguous_send_is_not_retried_blindly(self):
        with tempfile.TemporaryDirectory() as directory:
            settings, target, api, state = self.setup_case(directory)
            api.send_message = lambda *args: (0, {})
            with self.assertRaises(runner.DeliveryError):
                runner.send_one(settings, api, state, target, 'with curry?', runner.StopFlag())
            self.assertIn('1', runner.load_state(settings)['replied_ids'])
            self.assertEqual(state['day_count'], 0)

    def test_corrupt_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = runner.Settings.from_env(env_values(directory))
            (Path(directory) / 'state.json').write_text('{"day_count": 0}')
            with self.assertRaises(runner.ConfigError):
                runner.load_state(settings)

    def test_skip_and_overlong_text_are_rejected_without_truncation(self):
        for text in ('SKIP', '[SKIP]', 'Let us make this reply much longer than the allowed number of words today', 'same rn'):
            self.assertEqual(runner.trim_reply(text), '')
        self.assertEqual(runner.trim_reply('show me how then'), 'show me how then')

    def test_transport_write_gate(self):
        settings = runner.Settings.from_env(env_values('.runtime/test'))
        with patch.object(runner.urllib.request, 'urlopen') as network:
            with self.assertRaises(RuntimeError):
                runner.DiscordAPI(settings).send_message(message('1', 'A topic'), 'what time?')
        network.assert_not_called()

    def test_terminal_failures_exit_two_not_one(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = runner.Settings.from_env(env_values(directory))
            for exc in (runner.ConfigError('boom'), runner.IdentityError('boom'), runner.DeliveryError(403, {})):
                settings.data_dir.mkdir(parents=True, exist_ok=True)
                with patch.dict(runner.os.environ, env_values(directory)):
                    with patch.object(runner, 'run_live', side_effect=exc):
                        with patch.object(runner, 'log'):
                            self.assertEqual(runner.main([]), 2)
            with patch.dict(runner.os.environ, env_values(directory)):
                with patch.object(runner, 'run_live', side_effect=OSError('boom')):
                    with patch.object(runner, 'log'):
                        self.assertEqual(runner.main([]), 1)

    def test_service_suppresses_restart_for_terminal_exit(self):
        unit = (Path(__file__).resolve().parent.parent / 'deploy' / 'discord-autoreply.service').read_text()
        assert 'Restart=on-failure' in unit
        assert 'RestartPreventExitStatus=2' in unit


if __name__ == '__main__':
    unittest.main()
