"""Local, non-secret settings and explicit Windows Credential Manager access."""
import getpass
import json
import os
from pathlib import Path
import sys
import tempfile
import warnings

SERVICE = 'Discord-Maple-boss-bot'
ACCOUNT = 'DISCORD_TOKEN'


class SettingsError(Exception):
    """A message that is safe to display without credentials or backend details."""


def config_path():
    return Path(os.getenv('MAPLEBOT_CONFIG', 'config.local.json'))


def guild_id(value):
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal() or len(value)>20:
        raise SettingsError('서버 ID는 양의 정수로 입력하세요.')
    result = int(value)
    if not 0 < result < 2**64:
        raise SettingsError('서버 ID는 1~18446744073709551615 범위여야 합니다.')
    return result


def read_config():
    try:
        data = json.loads(config_path().read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise SettingsError('로컬 설정 파일을 읽을 수 없습니다. UTF-8 JSON 형식과 접근 권한을 확인하세요.') from None
    if not isinstance(data, dict) or set(data) - {'test_guild_id'}:
        raise SettingsError('로컬 설정에는 test_guild_id 항목만 사용할 수 있습니다. 토큰은 저장하지 마세요.')
    if 'test_guild_id' in data:
        guild_id(data['test_guild_id'])
    return data


def save_config(data):
    path = config_path()
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.maple-config-', suffix='.tmp', delete=False) as output:
            temporary = Path(output.name)
            json.dump(data, output, ensure_ascii=False, indent=2)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except OSError:
        raise SettingsError('로컬 설정을 저장하지 못했습니다. 폴더 접근 권한을 확인하세요.') from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def set_test_guild(value):
    guild_id(value)
    data = read_config()
    data['test_guild_id'] = value
    save_config(data)


def delete_test_guild():
    data = read_config()
    data.pop('test_guild_id', None)
    save_config(data)


def test_guild():
    if 'DISCORD_TEST_GUILD_ID' in os.environ:
        return guild_id(os.environ['DISCORD_TEST_GUILD_ID'])
    data = read_config()
    return guild_id(data['test_guild_id']) if 'test_guild_id' in data else None


def windows_vault():
    if sys.platform != 'win32':
        raise SettingsError('자격 증명 저장 기능은 Windows 전용입니다. 이 환경에서는 DISCORD_TOKEN 환경 변수를 사용하세요.')
    try:
        # Select the encrypted native backend explicitly. Never auto-select a
        # third-party plaintext backend or run keyring's plugin discovery.
        from keyring.backends.Windows import WinVaultKeyring
        backend = WinVaultKeyring()
        if backend.priority <= 0:
            raise RuntimeError('unavailable')
        backend.persist = 'local machine'
        return backend
    except Exception:
        raise SettingsError('Windows 자격 증명 관리자를 사용할 수 없습니다. requirements.lock 설치와 Windows 계정 접근 권한을 확인하세요.') from None


def valid_token(value):
    value = value.strip()
    if not value or any(c.isspace() for c in value):
        raise SettingsError('토큰은 비어 있거나 중간에 공백을 포함할 수 없습니다.')
    return value


def set_token():
    backend = windows_vault()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            value = getpass.getpass('Discord 봇 토큰 입력 (화면에 표시되지 않음): ')
    except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        raise SettingsError('토큰 입력을 취소했습니다. 저장하지 않았습니다. 대화형 터미널에서 다시 실행하세요.') from None
    value = valid_token(value)
    try:
        backend.set_password(SERVICE, ACCOUNT, value)
    except Exception:
        raise SettingsError('토큰을 저장하지 못했습니다. Windows 자격 증명 관리자 접근 권한을 확인하세요.') from None


def delete_token():
    backend = windows_vault()
    try:
        if backend.get_password(SERVICE, ACCOUNT) is not None:
            backend.delete_password(SERVICE, ACCOUNT)
    except Exception:
        raise SettingsError('저장된 토큰을 삭제하지 못했습니다. Windows 자격 증명 관리자 접근 권한을 확인하세요.') from None


def load_token():
    value = os.getenv('DISCORD_TOKEN')
    if value:
        return valid_token(value)
    if sys.platform == 'win32':
        backend = windows_vault()
        try:
            value = backend.get_password(SERVICE, ACCOUNT)
        except Exception:
            raise SettingsError('저장된 토큰을 읽지 못했습니다. Windows 자격 증명 관리자 접근 권한을 확인하세요.') from None
        if value:
            return valid_token(value)
    raise SettingsError('봇 토큰이 없습니다. Windows에서는 python -m maplebot --token-set 으로 등록하거나 DISCORD_TOKEN 환경 변수를 설정하세요.')
