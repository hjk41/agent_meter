import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from agent_meter.droid import LIMITS_URL, collect_droid, local_access_token, parse_droid
from agent_meter.providers import collect
from agent_meter.storage import normalize


class DroidTests(unittest.TestCase):
    def test_preserves_distinct_pools_and_omits_missing_windows(self):
        snapshot = parse_droid({'limits': {
            'standard': {
                'fiveHour': {'usedPercent': 12.5, 'windowEnd': '2026-11-17T12:00:00Z'},
                'weekly': {'windowEnd': '2026-11-18T12:00:00Z'},
                'monthly': {'usedPercent': 101},
            },
            'core': {
                'fiveHour': {'usedPercent': False},
                'weekly': {'usedPercent': -1},
                'monthly': {'usedPercent': 0, 'windowEnd': None},
            },
        }}, now=100)
        self.assertEqual(snapshot.status, 'live')
        self.assertEqual(snapshot.collected_at, 100)
        self.assertEqual([(q.name, q.remaining, q.reset_at) for q in snapshot.quotas], [
            ('标准 · 5 小时', 87.5, 1794916800),
            ('标准 · 30 天', 0, None),
            ('Core · 30 天', 100, None),
        ])
        unavailable = parse_droid({'limits': {'notAvailable': True}})
        self.assertEqual((unavailable.status, unavailable.quotas), ('waiting', []))

    def test_reads_factory_envelope_without_changing_it(self):
        key = AESGCM.generate_key(bit_length=256)
        iv = bytes(range(16))
        plaintext = json.dumps({'access_token': 'local-token', 'refresh_token': 'unused'}).encode()
        sealed = AESGCM(key).encrypt(iv, plaintext, None)
        envelope = b':'.join(base64.b64encode(part) for part in (iv, sealed[-16:], sealed[:-16]))
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            path = home/'.factory/auth.v2.keyring'
            path.parent.mkdir()
            path.write_bytes(envelope)
            with patch('agent_meter.droid.Path.home', return_value=home), \
                 patch('agent_meter.droid.local_encryption_key', return_value=base64.b64encode(key)):
                self.assertEqual(local_access_token(), 'local-token')
            self.assertEqual(path.read_bytes(), envelope)

    def test_collector_respects_proxy_and_does_not_follow_redirects(self):
        config = normalize({'network': {'mode': 'custom', 'url': 'http://localhost:7890', 'agents': ['droid']}})
        with patch.dict('os.environ', {'FACTORY_API_KEY': ''}), \
             patch('agent_meter.droid.local_access_token', return_value='local-token'), \
             patch('agent_meter.providers.http_session') as sessions:
            request = sessions.return_value.__enter__.return_value.get
            request.return_value.status_code = 200
            request.return_value.json.return_value = {'limits': {'standard': {'fiveHour': {'usedPercent': 20}}}}
            snapshot = collect('droid', config, '/unused')
        sessions.assert_called_once_with({'mode': 'custom', 'url': 'http://localhost:7890'})
        request.assert_called_once_with(LIMITS_URL,
            headers={'Authorization': 'Bearer local-token', 'Accept': 'application/json'},
            timeout=(10, 20), allow_redirects=False)
        self.assertEqual(snapshot.primary.remaining, 80)

    def test_missing_and_rejected_credentials(self):
        with patch.dict('os.environ', {'FACTORY_API_KEY': ''}), \
             patch('agent_meter.droid.local_access_token', return_value=None), \
             patch('agent_meter.providers.http_session') as sessions:
            snapshot = collect_droid({'mode': 'direct'})
            self.assertEqual(snapshot.status, 'waiting')
            sessions.assert_not_called()

        with patch.dict('os.environ', {'FACTORY_API_KEY': 'api-key'}), \
             patch('agent_meter.droid.local_access_token') as local, \
             patch('agent_meter.providers.http_session') as sessions:
            request = sessions.return_value.__enter__.return_value.get
            request.return_value.status_code = 401
            snapshot = collect_droid({'mode': 'direct'})
            self.assertEqual(snapshot.status, 'error')
            self.assertNotIn('api-key', snapshot.message)
            local.assert_not_called()
