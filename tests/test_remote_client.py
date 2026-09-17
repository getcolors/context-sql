import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

CLIENT_PATH = Path(__file__).resolve().parents[1] / 'skills/sql-context/scripts/remote.py'
spec = importlib.util.spec_from_file_location('remote_client', CLIENT_PATH)
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)


class RemoteClientTests(unittest.TestCase):
    def test_only_https_without_embedded_secrets(self):
        for value in ('http://example.org', 'https://u:p@example.org', 'https://example.org/?token=x',
                      'https://example.org/#token', 'https://example.org:bad', 'https://example.org\n'):
            with self.subTest(value=value), self.assertRaises(client.ClientError):
                client.service_url(value)
        self.assertEqual(client.service_url('https://example.org/context/'), 'https://example.org/context')

    def test_private_token_files_and_symlink_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'token'
            path.write_text(json.dumps({'account_id': 'id', 'token': 'secret-token'}))
            path.chmod(0o600)
            self.assertEqual(client.token_value(str(path)), 'secret-token')
            path.chmod(0o644)
            with self.assertRaises(client.ClientError):
                client.token_value(str(path))
            path.chmod(0o600)
            link = Path(directory) / 'link'
            link.symlink_to(path)
            with self.assertRaises(client.ClientError):
                client.token_value(str(link))

    def test_refuses_redirect(self):
        with self.assertRaises(client.ClientError):
            client.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.example')

    def test_error_does_not_echo_remote_body_or_url(self):
        error = urllib.error.HTTPError('https://secret-url', 403, 'secret body', {}, io.BytesIO(b'secret-token'))
        with patch.object(client.urllib.request.OpenerDirector, 'open', side_effect=error):
            with self.assertRaises(client.ClientError) as result:
                client.request('https://example.org', 'secret-token', 'GET', '/v1/schema')
        self.assertNotIn('secret', str(result.exception))
        self.assertIn('403', str(result.exception))

    def test_prompt_exact_text_and_parent(self):
        prompt_id = '7d504bdf-a2ac-41ce-a1a0-31d549aeae44'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'prompt.txt'
            path.write_bytes(b'Exact text\r\nsecond line\n')
            output = io.StringIO()
            with patch.dict(os.environ, {'CONTEXT_SQL_TOKEN': 'token'}), patch.object(client, 'request', return_value={'prompt_id': prompt_id}) as request, contextlib.redirect_stdout(output):
                self.assertEqual(client.main(['--url', 'https://example.org', 'prompt', '--file', str(path), '--project', 'a/b', '--task', 'task', '--parent-id', prompt_id]), 0)
            body = request.call_args.args[4]
            self.assertEqual(body['text'], 'Exact text\r\nsecond line\n')
            self.assertEqual(body['parent_id'], prompt_id)
            self.assertEqual(body['kind'], 'prompt')

    def test_sql_bound_params_and_explicit_writer(self):
        with patch.dict(os.environ, {'CONTEXT_SQL_TOKEN': 'token'}), patch.object(client, 'request', return_value={'outcome': 'success'}) as request, contextlib.redirect_stdout(io.StringIO()):
            code = client.main(['--url', 'https://example.org', 'sql', '--query', 'SELECT %s', '--params', '["secret value"]', '--prompt-id', '7d504bdf-a2ac-41ce-a1a0-31d549aeae44', '--role', 'writer', '--capture-result'])
        self.assertEqual(code, 0)
        body = request.call_args.args[4]
        self.assertEqual(body['params'], ['secret value'])
        self.assertEqual(body['role'], 'writer')
        self.assertTrue(body['capture_result'])

    def test_invalid_params_are_not_sent(self):
        with patch.dict(os.environ, {'CONTEXT_SQL_TOKEN': 'token'}), patch.object(client, 'request') as request, contextlib.redirect_stderr(io.StringIO()):
            code = client.main(['--url', 'https://example.org', 'sql', '--query', 'SELECT 1', '--params', '42', '--prompt-id', '7d504bdf-a2ac-41ce-a1a0-31d549aeae44'])
        self.assertEqual(code, 1)
        request.assert_not_called()

    def test_request_cap_before_network(self):
        with patch.object(client.urllib.request, 'build_opener') as opener:
            with self.assertRaises(client.ClientError):
                client.request('https://example.org', 'token', 'POST', '/v1/sql', {'sql': 'x' * client.MAX_REQUEST_BYTES})
        opener.assert_not_called()

    def test_response_cap_and_bearer(self):
        with patch.object(client.urllib.request, 'build_opener') as build:
            response = build.return_value.open.return_value.__enter__.return_value
            response.status = 200
            response.read.return_value = b'x' * (client.MAX_RESPONSE_BYTES + 1)
            with self.assertRaises(client.ClientError):
                client.request('https://example.org', 'token', 'GET', '/v1/schema')
            req = build.return_value.open.call_args.args[0]
            self.assertEqual(req.headers['Authorization'], 'Bearer token')
            response.read.assert_called_once_with(client.MAX_RESPONSE_BYTES + 1)


if __name__ == '__main__':
    unittest.main()
