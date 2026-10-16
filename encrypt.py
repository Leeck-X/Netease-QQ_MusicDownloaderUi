"""网易云 weapi 加密实现（AES-128-CBC 双层 + RSA）。

流程：
    b1   = b64(AES-CBC(json, KEY1, IV))
    params = b64(AES-CBC(b1, KEY2, IV))
    encSecKey = hex(pow(int(KEY2[::-1]), e, m)).zfill(256)

密钥不再是硬编码常量：可在程序的「设置 → 高级密钥」中修改，或直接写入
config.json 的 weapi_keys 字段；默认值与网易云 web 客户端一致。
"""
import base64
import json
import random
from dataclasses import dataclass

from Crypto.Cipher import AES

DEFAULT_KEY1 = '0CoJUm6Qyw8W8jud'
DEFAULT_IV = '0102030405060708'
DEFAULT_PUB_KEY = '010001'
DEFAULT_MODULUS = (
    '00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7b725152b3ab17a876a'
    'ea8a5aa76d2e417629ec4ee341f56135fccf695280104e0312ecbda92557c93870114af6c9d05c'
    '4f7f0c3685b7a46bee255932575cce10b424d813cfe4875d3e82047b97ddef52741d546b8e289d'
    'c6935b3ece0462db0a22b8e7'
)
BASE62 = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'

# 1024 bit 模数对应的定长 encSecKey 十六进制长度
ENC_SEC_KEY_HEX_LEN = 256
# weapi 只支持 1024 bit 模数，超过该位数（如 2048 bit PEM 密钥）会导致请求全部失败
MAX_MODULUS_BITS = 1024


@dataclass(frozen=True)
class WEApiKeys:
    """一组 weapi 加密密钥。"""

    key1: str = DEFAULT_KEY1
    iv: str = DEFAULT_IV
    pub_key: str = DEFAULT_PUB_KEY
    modulus: str = DEFAULT_MODULUS

    @classmethod
    def from_config(cls, data) -> 'WEApiKeys':
        """从 config.json 的 weapi_keys 构造；缺失 / 空 / 非字符串字段回退默认值。"""
        data = data if isinstance(data, dict) else {}
        defaults = cls()
        values = {}
        for field in ('key1', 'iv', 'pub_key', 'modulus'):
            value = data.get(field)
            if isinstance(value, str) and value.strip():
                values[field] = value.strip()
            else:
                values[field] = getattr(defaults, field)
        return cls(**values)

    def to_dict(self) -> dict:
        return {'key1': self.key1, 'iv': self.iv,
                'pub_key': self.pub_key, 'modulus': self.modulus}

    def validate(self) -> str | None:
        """校验密钥是否可用于 weapi；返回错误说明，None 表示可用。"""
        for name, value in (('key1', self.key1), ('iv', self.iv)):
            size = len(value.encode('utf-8'))
            if size != 16:
                return f'{name} 必须为 16 字节，当前为 {size} 字节'
        for name, value in (('pub_key', self.pub_key), ('modulus', self.modulus)):
            try:
                int(value, 16)
            except ValueError:
                return f'{name} 必须是合法的十六进制字符串'
        bits = int(self.modulus, 16).bit_length()
        if bits > MAX_MODULUS_BITS:
            return (f'模数位数不匹配：weapi 需要 {MAX_MODULUS_BITS} 位，当前为 {bits} 位'
                    f'（{bits} 位 PEM 密钥无法用于 weapi）')
        return None


def _aes_encrypt(text: bytes, key: bytes, iv: bytes) -> bytes:
    pad = 16 - len(text) % 16
    text = text + bytes([pad]) * pad
    return base64.b64encode(AES.new(key, AES.MODE_CBC, iv).encrypt(text))


def _rsa_encrypt(text: bytes, pub_key: str, modulus: str) -> str:
    result = pow(int(text[::-1].hex(), 16), int(pub_key, 16), int(modulus, 16))
    return format(result, 'x').zfill(ENC_SEC_KEY_HEX_LEN)


class WEApiCipher:
    """按指定密钥执行 weapi 加密；换密钥只需换一个实例。"""

    def __init__(self, keys: WEApiKeys = None):
        self.keys = keys or WEApiKeys()

    def encrypt(self, payload: dict) -> dict:
        """把明文 payload 加密成 weapi 需要的 params / encSecKey 两个字段。"""
        keys = self.keys
        text = json.dumps(payload, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
        iv = keys.iv.encode('utf-8')
        secret = ''.join(random.choice(BASE62) for _ in range(16)).encode('utf-8')
        params = _aes_encrypt(_aes_encrypt(text, keys.key1.encode('utf-8'), iv), secret, iv)
        return {'params': params.decode('utf-8'),
                'encSecKey': _rsa_encrypt(secret, keys.pub_key, keys.modulus)}


_DEFAULT_CIPHER = WEApiCipher()


def encrypt(payload: dict, cipher: WEApiCipher = None) -> dict:
    """兼容入口：不传 cipher 时使用默认密钥。"""
    return (cipher or _DEFAULT_CIPHER).encrypt(payload)


if __name__ == '__main__':
    out = encrypt({'s': 'test', 'type': 1, 'limit': 10})
    assert len(out['encSecKey']) == ENC_SEC_KEY_HEX_LEN
    assert isinstance(out['params'], str) and out['params']
    assert WEApiKeys().validate() is None
    assert WEApiKeys.from_config(None).to_dict() == WEApiKeys().to_dict()
    assert WEApiKeys.from_config({'key1': '', 'modulus': 123}).to_dict() == WEApiKeys().to_dict()
    assert WEApiKeys(key1='short').validate() is not None
    # 2048 bit 模数（如 credentials 里的 PEM 密钥）超出 weapi 上限，必须被拦截
    assert WEApiKeys(modulus='f' * 512).validate() is not None
    print('encrypt self-test OK')
