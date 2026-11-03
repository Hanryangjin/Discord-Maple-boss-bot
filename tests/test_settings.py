"""Credential operations use mocks, never a developer's real credential store."""
import contextlib
import getpass
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from maplebot import settings
from maplebot.__main__ import main


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'config.local.json'
        self.env = patch.dict(os.environ, {'MAPLEBOT_CONFIG':str(self.path)}, clear=True)
        self.env.start(); self.addCleanup(self.env.stop)

    def command(self, *args):
        with patch.object(sys,'argv',['maplebot',*args]), contextlib.redirect_stdout(io.StringIO()) as out:
            main()
        return out.getvalue()

    def test_id_persists_and_delete_restores_global(self):
        settings.set_test_guild('123456789012345678')
        self.assertEqual(settings.test_guild(),123456789012345678)
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8')),{'test_guild_id':'123456789012345678'})
        settings.set_test_guild('456')
        self.assertEqual(settings.test_guild(),456)
        settings.delete_test_guild();settings.delete_test_guild()
        self.assertIsNone(settings.test_guild())

    def test_environment_overrides_local_without_rewriting(self):
        settings.set_test_guild('123')
        with patch.dict(os.environ,{'DISCORD_TEST_GUILD_ID':'456'}):
            self.assertEqual(settings.test_guild(),456)
        self.assertEqual(settings.test_guild(),123)

    def test_invalid_ids_never_become_global_or_overwrite(self):
        settings.set_test_guild('123')
        for value in ('','0','-1','1.2','abc','１２３','1\n',str(2**64),'9'*5000,True,123,None):
            with self.subTest(value=str(value)[:20]):
                with self.assertRaises(settings.SettingsError):settings.set_test_guild(value)
        self.assertEqual(settings.test_guild(),123)
        with patch.dict(os.environ,{'DISCORD_TEST_GUILD_ID':''}):
            with self.assertRaises(settings.SettingsError):settings.test_guild()

    def test_broken_settings_fail_closed(self):
        for content in ('broken','[]','{"test_guild_id":null}','{"DISCORD_TOKEN":"fake-secret"}'):
            self.path.write_text(content,encoding='utf-8')
            with self.assertRaises(settings.SettingsError) as error:settings.test_guild()
            self.assertNotIn('fake-secret',str(error.exception))

    def test_failed_atomic_save_preserves_old_file_and_removes_temporary(self):
        settings.set_test_guild('123')
        with patch('maplebot.settings.os.replace',side_effect=OSError('private detail')):
            with self.assertRaises(settings.SettingsError):settings.set_test_guild('456')
        self.assertEqual(settings.test_guild(),123)
        self.assertEqual(list(self.path.parent.glob('.maple-config-*')),[])

    def test_environment_token_skips_store(self):
        with patch.dict(os.environ,{'DISCORD_TOKEN':'env-secret'}), patch.object(settings,'windows_vault') as vault:
            self.assertEqual(settings.load_token(),'env-secret')
            vault.assert_not_called()

    def test_load_saved_token_and_missing_token(self):
        vault=Mock();vault.get_password.return_value='stored-secret'
        with patch.object(settings.sys,'platform','win32'),patch.object(settings,'windows_vault',return_value=vault):
            self.assertEqual(settings.load_token(),'stored-secret')
            vault.get_password.assert_called_with(settings.SERVICE,settings.ACCOUNT)
            vault.get_password.return_value=None
            with self.assertRaisesRegex(settings.SettingsError,'--token-set'):settings.load_token()

    def test_non_windows_uses_environment_without_keyring(self):
        with patch.object(settings.sys,'platform','linux'),patch.object(settings,'windows_vault') as vault:
            with self.assertRaises(settings.SettingsError):settings.load_token()
            vault.assert_not_called()
        with patch.object(settings.sys,'platform','linux'):
            with self.assertRaisesRegex(settings.SettingsError,'Windows 전용'):settings.windows_vault()

    def test_registration_uses_hidden_prompt_and_never_writes_file(self):
        vault=Mock()
        with patch.object(settings,'windows_vault',return_value=vault),patch.object(settings.getpass,'getpass',return_value='stored-secret'):
            output=self.command('--token-set')
        vault.set_password.assert_called_once_with(settings.SERVICE,settings.ACCOUNT,'stored-secret')
        self.assertNotIn('stored-secret',output)
        self.assertFalse(self.path.exists())

    def test_empty_cancelled_or_echoing_prompt_does_not_save(self):
        vault=Mock()
        with patch.object(settings,'windows_vault',return_value=vault):
            for error in (EOFError(),KeyboardInterrupt(),getpass.GetPassWarning()):
                with patch.object(settings.getpass,'getpass',side_effect=error):
                    with self.assertRaises(settings.SettingsError):settings.set_token()
            for value in ('','  ','fake secret'):
                with patch.object(settings.getpass,'getpass',return_value=value):
                    with self.assertRaises(settings.SettingsError):settings.set_token()
        vault.set_password.assert_not_called()

    def test_native_backend_is_explicit_and_machine_persistent(self):
        backend=Mock(priority=1)
        factory=Mock(return_value=backend)
        module=types.ModuleType('keyring.backends.Windows');module.WinVaultKeyring=factory
        with patch.object(settings.sys,'platform','win32'),patch.dict(sys.modules,{'keyring.backends.Windows':module}):
            self.assertIs(settings.windows_vault(),backend)
            self.assertEqual(backend.persist,'local machine')
            factory.assert_called_once_with()

    def test_no_native_backend_fails_without_plaintext_fallback(self):
        module=types.ModuleType('keyring.backends.Windows');module.WinVaultKeyring=Mock(side_effect=RuntimeError('secret-detail'))
        with patch.object(settings.sys,'platform','win32'),patch.dict(sys.modules,{'keyring.backends.Windows':module}):
            with self.assertRaises(settings.SettingsError) as error:settings.windows_vault()
        self.assertNotIn('secret-detail',str(error.exception))

    def test_backend_errors_never_expose_secret(self):
        vault=Mock()
        vault.get_password.side_effect=RuntimeError('secret-detail')
        vault.set_password.side_effect=RuntimeError('secret-detail')
        with patch.object(settings.sys,'platform','win32'),patch.object(settings,'windows_vault',return_value=vault),patch.object(settings.getpass,'getpass',return_value='fake-secret'):
            for action in (settings.load_token,settings.set_token,settings.delete_token):
                with self.assertRaises(settings.SettingsError) as error:action()
                self.assertNotIn('secret-detail',str(error.exception))
                self.assertNotIn('fake-secret',str(error.exception))

    def test_delete_is_idempotent(self):
        vault=Mock();vault.get_password.return_value='fake-secret'
        with patch.object(settings,'windows_vault',return_value=vault):
            settings.delete_token()
            vault.delete_password.assert_called_once_with(settings.SERVICE,settings.ACCOUNT)
            vault.get_password.return_value=None
            settings.delete_token()
            self.assertEqual(vault.delete_password.call_count,1)

    def test_settings_commands_do_not_open_db_or_login(self):
        with patch('maplebot.__main__.Database') as db,patch('maplebot.__main__.run_bot') as run:
            self.command('--test-guild-set','123')
            self.command('--test-guild-delete')
            with patch.object(settings,'set_token'),patch.object(settings,'delete_token'):
                self.command('--token-set');self.command('--token-delete')
            db.assert_not_called();run.assert_not_called()

    def test_override_notice_without_secret_value(self):
        with patch.dict(os.environ,{'DISCORD_TOKEN':'env-secret'}),patch.object(settings,'set_token'):
            output=self.command('--token-set')
        self.assertIn('우선',output);self.assertNotIn('env-secret',output)
        with patch.dict(os.environ,{'DISCORD_TEST_GUILD_ID':'456'}):
            self.assertIn('우선',self.command('--test-guild-set','123'))

    def test_check_and_backup_do_not_access_credentials(self):
        with patch.object(settings,'load_token') as token,patch('maplebot.__main__.Database') as db:
            db.return_value.backup.return_value='backup.sqlite3'
            self.command('--backup',self.temp.name)
            token.assert_not_called()
        from maplebot.discord_app import MapleBot
        with patch.object(settings,'load_token') as token,patch.object(MapleBot,'close',new_callable=AsyncMock):
            with patch.dict(os.environ,{'MAPLEBOT_DB':str(Path(self.temp.name)/'db.sqlite3')}):
                self.command('--check')
            token.assert_not_called()

    def test_startup_passes_saved_guild_and_token_to_bot(self):
        settings.set_test_guild('123')
        with patch.object(settings,'load_token',return_value='fake-secret'),patch('maplebot.__main__.Database'),patch('maplebot.discord_app.MapleBot') as factory:
            self.command()
            self.assertEqual(factory.call_args.kwargs['test_guild_id'],123)
            factory.return_value.run.assert_called_once_with('fake-secret',log_handler=None)

    def test_login_failure_has_safe_actionable_message(self):
        from discord import LoginFailure
        with patch.object(settings,'load_token',return_value='fake-secret'),patch('maplebot.__main__.Database'),patch('maplebot.discord_app.MapleBot') as factory:
            factory.return_value.run.side_effect=LoginFailure('fake-secret')
            with contextlib.redirect_stderr(io.StringIO()) as err:
                with self.assertRaises(SystemExit):self.command()
            self.assertNotIn('fake-secret',err.getvalue())
            self.assertIn('재등록',err.getvalue())


class SyncTargetTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolved_guild_controls_discord_sync_scope(self):
        from maplebot.db import Database
        from maplebot.discord_app import MapleBot
        with tempfile.TemporaryDirectory() as directory:
            for target in (123,None):
                bot=MapleBot(Database(str(Path(directory)/'db.sqlite3')),test_guild_id=target)
                bot.add_cog=AsyncMock()
                bot.tree.sync=AsyncMock()
                bot.tree.copy_global_to=Mock()
                bot.run_worker=AsyncMock()
                bot.maintenance=AsyncMock()
                try:
                    await bot.setup_hook()
                    if target:
                        guild=bot.tree.sync.call_args.kwargs['guild']
                        self.assertEqual(guild.id,target)
                        bot.tree.copy_global_to.assert_called_once_with(guild=guild)
                    else:
                        bot.tree.sync.assert_awaited_once_with()
                        bot.tree.copy_global_to.assert_not_called()
                finally:
                    await bot.close()
