"""No LAN requests: verify the actual CLI refuses unloaded models before any POST."""
import contextlib
import io
import sys
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import client
from dispatcher import should_block_worker


class ClientTests(unittest.TestCase):
    def catalog(self, loaded=True):
        return {'models': [
            {'type': 'llm', 'key': 'chosen', 'loaded_instances':
             [{'id': 'chosen-instance', 'config': {'context_length': 51456}}] if loaded else []},
            {'type': 'llm', 'key': 'chosen@bf16', 'loaded_instances': []},
            {'type': 'embedding', 'key': 'embed', 'loaded_instances': [{'id': 'embed'}]},
        ]}

    def invoke(self, action, model, responses):
        with tempfile.TemporaryDirectory() as directory:
            prompt = Path(directory) / 'prompt.txt'
            prompt.write_text('Review source', encoding='utf-8')
            args = ['client', '--via-dispatcher', '--model', model]
            args += ['--warmup'] if action == 'warmup' else ['--prompt-file', str(prompt)]
            if action == 'native':
                args += ['--reasoning', 'off']
            with patch.object(sys, 'argv', args), patch.object(client, 'request_json', side_effect=responses) as request:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    status = client.main()
                return status, request.call_args_list

    def test_unloaded_or_similar_model_never_sends_generation(self):
        for action in ('openai', 'native', 'warmup'):
            for model in ('chosen@bf16', 'cho', 'missing'):
                with self.subTest(action=action, model=model):
                    status, calls = self.invoke(action, model, [self.catalog()])
                    self.assertEqual(status, 1)
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(calls[0].args[1:3], ('/api/v1/models', None))
        self.assertTrue(should_block_worker('uncertain', {'error': 'Loaded model unavailable: chosen'}))

    def test_exact_loaded_instance_and_no_load_configuration(self):
        answer = {'choices': [{'message': {'content': 'OK'}, 'finish_reason': 'stop'}]}
        for action in ('openai', 'warmup'):
            status, calls = self.invoke(action, 'chosen', [self.catalog(), answer])
            self.assertEqual(status, 0)
            payload = calls[1].args[2]
            self.assertEqual(payload['model'], 'chosen-instance')
            self.assertNotIn('context_length', payload)
        native = {'output': [{'type': 'message', 'content': 'OK'}], 'stats': {'total_output_tokens': 1}}
        status, calls = self.invoke('native', 'chosen-instance', [self.catalog(), native])
        self.assertEqual(status, 0)
        self.assertEqual(calls[1].args[2]['model'], 'chosen-instance')

    def test_failed_model_probe_never_sends_generation(self):
        for failure in (OSError('API unavailable'), {'models': None}):
            status, calls = self.invoke('openai', 'chosen', [failure])
            self.assertEqual(status, 1)
            self.assertEqual(len(calls), 1)

    def test_health_excludes_unloaded_models(self):
        with patch.object(client, 'request_json', return_value=self.catalog()):
            self.assertEqual(client.loaded_models('http://localhost:1234/v1', 1),
                             {'chosen': 'chosen-instance', 'chosen-instance': 'chosen-instance'})


if __name__ == '__main__':
    unittest.main()
