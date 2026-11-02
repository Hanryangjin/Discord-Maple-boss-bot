from decimal import Decimal, InvalidOperation
import re
import unicodedata


class UserError(Exception):
    """Safe, user-facing domain error."""


def key(value: str) -> str:
    return unicodedata.normalize('NFC', value.strip()).casefold()


def label(value: str, limit: int = 50) -> str:
    value = value.strip()
    if not 1 <= len(value) <= limit or any(ord(c) < 32 for c in value):
        raise UserError(f'이름은 공백뿐이지 않은 1~{limit}자로 입력해 주세요.')
    return value


def character_name(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r'[A-Za-z0-9가-힣]+', value):
        raise UserError('캐릭터명은 완성형 한글·영문·숫자만 허용합니다.')
    if len(value) < 2 or sum(2 if '가' <= c <= '힣' else 1 for c in value) > 12:
        raise UserError('캐릭터명은 최소 2자, 한글 2·영문/숫자 1 기준 합계 12 이하여야 합니다.')
    return value


def hexa(value: str) -> int:
    text = value.strip()
    if len(text) > 64 or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', text):
        raise UserError('헥환은 만 단위 숫자로 입력해 주세요. 예: 5.25')
    try:
        number = Decimal(text)
        if number <= 0 or number > Decimal(9223372036854775800) / 10000:
            raise UserError('헥환 값이 허용 범위를 벗어났습니다.')
        fraction = text.partition('.')[2].rstrip('0')
        if len(fraction) > 2:
            raise UserError('헥환은 소수 둘째 자리까지 입력할 수 있습니다.')
        return int(number * 10000)
    except InvalidOperation as exc:
        raise UserError('올바른 헥환을 입력해 주세요.') from exc


def show_hexa(value: int | None) -> str:
    return '미등록' if value is None else f'{Decimal(value) / 10000:.2f}만'
