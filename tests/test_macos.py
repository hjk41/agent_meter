import hashlib
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7
from agent_meter import platform_support as platform, secure_storage, providers, storage
from agent_meter import doubao_session, connections
from agent_meter.claude_oauth import OAuthError


class MacTests(unittest.TestCase):
    def test_finder_launch_finds_standalone_codex_without_shell_path(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            cli = home / '.local/bin/codex'
            cli.parent.mkdir(parents=True); cli.touch(); cli.chmod(0o755)
            with patch('sys.platform','darwin'), patch('pathlib.Path.home',return_value=home), \
                 patch.object(providers.shutil,'which',return_value=None), \
                 patch.dict(providers.os.environ,{'AGENT_METER_CODEX_EXE':''}):
                self.assertEqual(providers.codex_command(),[str(cli)])
                self.assertEqual(providers.cursor_database(),home/'Library/Application Support/Cursor/User/globalStorage/state.vscdb')
                with patch.dict(storage.os.environ,{'AGENT_METER_DATA_DIR':''}):
                    self.assertEqual(storage.data_dir(),home/'Library/Application Support/AgentMeter')

    def test_cursor_opens_user_app_with_launchservices(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory); app = home/'Applications/Cursor.app'; app.mkdir(parents=True)
            with patch('sys.platform','darwin'), patch('pathlib.Path.home',return_value=home), \
                 patch.object(connections.subprocess,'Popen') as launch:
                self.assertTrue(connections.open_local_cursor())
                launch.assert_called_once_with(['/usr/bin/open',str(app)])

    def test_keychain_encryption_survives_restart_and_rejects_tampering(self):
        values = {}
        def backend():
            result = Mock()
            result.get_password.side_effect = lambda s,a: values.get((s,a))
            result.set_password.side_effect = lambda s,a,p: values.__setitem__((s,a),p)
            return result
        with patch.object(secure_storage,'keychain_backend',side_effect=backend):
            blob = secure_storage.mac_protect(b'private-test-token')
            self.assertNotIn(b'private-test-token',blob)
            self.assertEqual(secure_storage.mac_protect(blob,True),b'private-test-token')
            with self.assertRaises(OAuthError):
                secure_storage.mac_protect(blob[:-1]+bytes([blob[-1]^1]),True)
            values.clear()
            with self.assertRaises(OAuthError): secure_storage.mac_protect(blob,True)
            self.assertEqual(values,{})  # Decrypting cannot replace a missing key.
        with patch.object(secure_storage,'keychain_backend',side_effect=RuntimeError('private details')):
            with self.assertRaises(OAuthError) as caught: secure_storage.mac_protect(b'token')
            self.assertNotIn('private',str(caught.exception))

    def test_macos_chromium_cookie_and_host_digest(self):
        host='.doubao.com'; password='test-safe-storage'
        key=hashlib.pbkdf2_hmac('sha1',password.encode(),b'saltysalt',1003,16)
        raw=hashlib.sha256(host.encode()).digest()+b'test-session'
        padder=PKCS7(128).padder(); padded=padder.update(raw)+padder.finalize()
        encryptor=Cipher(algorithms.AES(key),modes.CBC(b' '*16)).encryptor()
        blob=b'v10'+encryptor.update(padded)+encryptor.finalize()
        self.assertEqual(doubao_session.decrypt_cookie(blob,key,host,mac=True),'test-session')
        with self.assertRaises(doubao_session.SessionError):
            doubao_session.decrypt_cookie(b'v11'+blob[3:],key,host,mac=True)

    def test_startup_bundle_arguments_and_only_owned_launchagent(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'LaunchAgents/com.hjk41.agent-meter.plist'
            binary=Path(directory)/'Agent Meter.app/Contents/MacOS/AgentMeter'
            with patch.object(platform,'startup_path',return_value=path), \
                 patch.object(platform.sys,'executable',str(binary)), \
                 patch.object(platform.sys,'frozen',True,create=True), \
                 patch.object(platform.os,'getuid',return_value=501,create=True), \
                 patch.object(platform.subprocess,'run') as launch:
                platform.set_mac_startup(True)
                self.assertTrue(platform.mac_startup_enabled())
                data=plistlib.loads(path.read_bytes())
                self.assertEqual(data['ProgramArguments'],['/usr/bin/open','-g',str(binary.resolve().parents[2])])
                self.assertNotIn('KeepAlive',data)
                self.assertEqual(launch.call_args.args[0][1],'bootstrap')
                platform.set_mac_startup(False)
                self.assertFalse(platform.mac_startup_enabled())
                self.assertFalse(path.exists())


if __name__=='__main__': unittest.main()
