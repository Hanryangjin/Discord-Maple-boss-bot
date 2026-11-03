# Windows에서 토큰·테스트 서버 ID 저장하기

프로젝트 폴더의 PowerShell에서 실행합니다. 토큰은 Windows 자격 증명 관리자에, 비밀 정보가 아닌 서버 ID는 `config.local.json`에 저장됩니다. 둘 다 GitHub에서 코드를 다시 받아도 유지됩니다. 프로젝트 폴더 자체를 삭제하면 로컬 설정 파일은 사라지지만 자격 증명 관리자 항목은 남습니다.

## 최초 설정

의존성을 업데이트합니다. 가상환경이 이미 있으면 새로 만들 필요는 없습니다.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
```

토큰을 등록합니다. 토큰은 명령어 뒤에 붙이지 말고, 실행 후 나오는 입력창에 붙여 넣습니다. 입력 문자는 화면에 나타나지 않습니다.

```powershell
.\.venv\Scripts\python.exe -m maplebot --token-set
```

Discord 개발자 모드를 켜고 대상 서버를 우클릭하여 서버 ID를 복사합니다. 아래 숫자를 본인 서버 ID로 바꾸어 실행합니다.

```powershell
.\.venv\Scripts\python.exe -m maplebot --test-guild-set 123456789012345678
```

이 두 명령은 설정만 저장하며 봇에 로그인하거나 DB를 수정하지 않습니다. 봇이 실행 중이면 종료 후 다시 실행해야 새 설정이 적용됩니다.

## 이후 실행

같은 프로젝트 폴더에서 실행합니다. 터미널·VS Code·PC를 다시 켜도 토큰과 서버 ID를 다시 입력할 필요가 없습니다.

```powershell
.\.venv\Scripts\python.exe -m maplebot
```

실행 디렉터리가 달라지는 경우 `MAPLEBOT_CONFIG` 환경 변수에 설정 파일의 절대 경로를 지정할 수 있습니다. 기본 파일은 Git에서 제외되지만 다른 파일명으로 지정했다면 그 파일도 Git에서 직접 제외해야 합니다. DB·아이콘 폴더의 상대 경로 역시 실행 디렉터리를 기준으로 합니다.

## 우선순위

- 토큰: `DISCORD_TOKEN` 환경 변수 → Windows 자격 증명 관리자 → 오류 안내.
- 테스트 서버: `DISCORD_TEST_GUILD_ID` 환경 변수 → 로컬 설정 → 전역 명령 동기화.

현재 터미널의 오래된 환경 변수 때문에 저장한 설정이 적용되지 않으면 아래 명령으로 해당 터미널에서만 제거합니다.

```powershell
Remove-Item Env:DISCORD_TOKEN -ErrorAction SilentlyContinue
Remove-Item Env:DISCORD_TEST_GUILD_ID -ErrorAction SilentlyContinue
```

Windows 사용자·시스템 환경 변수에 영구 등록했다면 Windows 설정에서도 제거해야 새 터미널에서 다시 생기지 않습니다. 토큰이나 환경 변수 전체를 화면에 출력할 필요는 없습니다.

로컬 파일 예시는 다음과 같습니다. 토큰을 여기에 작성하지 마세요. 잘못된 JSON·빈 ID·범위를 벗어난 ID는 오류로 처리하며 전역 동기화로 대신 진행하지 않습니다. 단, 환경 변수로 서버 ID를 명시한 경우에는 그 값이 파일보다 우선합니다.

```json
{
  "test_guild_id": "123456789012345678"
}
```

테스트 서버 ID는 명령어 등록 범위를 지정하며 다른 서버에서 봇을 사용할 수 없게 하는 보안 설정은 아닙니다. 전역 명령어와 서버 명령어는 Discord에 따로 등록되므로, 범위를 바꾸어도 이전 범위의 명령어를 자동 삭제하지 않습니다.

## 변경·삭제

토큰을 교체하려면 `--token-set`, 서버를 바꾸려면 `--test-guild-set 새서버ID`를 다시 실행합니다.

```powershell
.\.venv\Scripts\python.exe -m maplebot --token-delete
.\.venv\Scripts\python.exe -m maplebot --test-guild-delete
```

첫 명령은 PC에 저장된 토큰만 삭제합니다. Discord 토큰 자체를 무효화하려면 Developer Portal에서 재발급해야 합니다. 두 번째 명령은 저장된 서버 ID를 삭제하며 **환경 변수도 없으면 다음 실행에서 전역 명령 동기화**를 사용합니다.

## 보안·지원 범위

토큰은 서비스 `Discord-Maple-boss-bot`, 계정 `DISCORD_TOKEN` 항목으로 현재 Windows 사용자에게 저장합니다. Windows 기본 암호화 저장소만 명시적으로 사용하고 일반 텍스트 저장소로 대체하지 않습니다. 저장소가 잠기거나 접근에 실패하면 오류로 안내합니다. 입력을 숨길 수 없는 환경에서는 토큰 등록을 중단합니다.

같은 Windows 사용자로 실행한 다른 복제본도 같은 토큰 항목을 사용합니다. 여러 봇을 운영하려면 각각의 실행 환경 변수로 구분해야 합니다. 다른 PC·Windows 사용자·서비스 계정으로 실행한다면 그 계정에서 따로 등록하세요. 같은 계정으로 실행되는 프로그램의 접근까지 막는 기능은 아니므로 PC 계정 보안은 유지해야 합니다.

Windows 외 환경은 기존 환경 변수 방식을 사용합니다. `.env`는 자동으로 읽지 않습니다. `--check`·`--backup`은 토큰 없이 계속 사용할 수 있습니다.
