"""名称转小写 ASCII 拼音编号；纯本地转换，供部门和业务方向共用。"""
import re
import unicodedata

from pypinyin import lazy_pinyin


def pinyin_code(name: str, prefix: str = 'bd') -> str:
    value = ''.join(lazy_pinyin(unicodedata.normalize('NFKC', name))).lower()
    code = re.sub(r'[^a-z0-9]+', '_', value).strip('_')
    if code and code[0].isdigit():
        code = prefix + '_' + code
    if not code:
        raise ValueError('无法自动生成字母编号，请手动填写')
    return code[:64].rstrip('_')
