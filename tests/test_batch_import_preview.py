#!/usr/bin/env python3
import json
import socket
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_server


def get_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def post_json(port, path, payload):
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        f'http://127.0.0.1:{port}{path}',
        data=data,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as err:
        body = err.read().decode('utf-8')
        return err.code, json.loads(body) if body else {}


def test_batch_import_preview_has_line_reason_and_import_is_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        config_path = Path(tmp) / 'config.json'
        group_id = 'relay-group-id'
        config_path.write_text(json.dumps({
            'groups': [{
                'id': group_id,
                'name': 'relay',
                'provider_type': 'relay',
                'base_url': 'https://relay.example/v1',
                'route_key': 'lr-relay',
            }],
            'models': [{
                'id': 'existing-id',
                'name': 'existing-model',
                'ep_id': 'existing-model',
                'group_id': group_id,
                'upstream_model': 'existing-model',
                'api_key': 'sk-old',
                'usable': True,
            }],
        }, ensure_ascii=False), encoding='utf-8')

        server, port, _ = create_server('127.0.0.1', get_free_port(), config_path)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            payload = {
                'group_id': group_id,
                'format': 'lines',
                'text': 'new-model\nexisting-model\nnew-model\nbad,name\n',
                'defaults': {'api_key': 'sk-new', 'price_input': 0.01, 'price_output': 0.02, 'usable': True},
                'preview': True,
            }
            status, preview = post_json(port, '/api/models/batch', payload)
            assert status == 200
            assert preview['summary'] == {'total': 4, 'new': 1, 'duplicate': 2, 'invalid': 1}
            invalid = [item for item in preview['items'] if item['status'] == 'invalid'][0]
            assert invalid['line'] == 4
            assert '模型名不能包含' in invalid['reason']
            duplicate = [item for item in preview['items'] if item['status'] == 'duplicate'][0]
            assert duplicate['reason']

            payload['text'] = 'new-model\nexisting-model\nnew-model\n'
            payload['preview'] = False
            status, imported = post_json(port, '/api/models/batch', payload)
            assert status == 200
            assert imported['added'] == 1
            assert imported['skipped'] == 2

            payload['preview'] = True
            status, second_preview = post_json(port, '/api/models/batch', payload)
            assert status == 200
            assert second_preview['summary']['new'] == 0
            assert second_preview['summary']['duplicate'] == 3
        finally:
            server.shutdown()
            server.server_close()
