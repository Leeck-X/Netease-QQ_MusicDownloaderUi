"""网易云 weapi 加密实现（AES-128-CBC 双层 + RSA）。

流程：
    b1   = b64(AES-CBC(json, KEY1, IV))
    params = b64(AES-CBC(b1, KEY2, IV))
    encSecKey = hex(pow(int(KEY2[::-1]), e, m)).zfill(256)
"""
import base64
import json
import random

from Crypto.Cipher import AES

KEY1 = b'0CoJUm6Qyw8W8jud'
IV = b'0102030405060708'
PUB_KEY = '010001'
MODULUS = (
    '00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7b725152b3ab17a876a'
    'ea8a5aa76d2e417629ec4ee341f56135fccf695280104e0312ecbda92557c93870114af6c9d05c'
    '4f7f0c3685b7a46bee255932575cce10b424d813cfe4875d3e82047b97ddef52741d546b8e289d'
    'c6935b3ece0462db0a22b8e7'
)
BASE62 = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'


def _aes_encrypt(text: bytes, key: bytes) -> bytes:
    pad = 16 - len(text) % 16
    text = text + bytes([pad]) * pad
    return base64.b64encode(AES.new(key, AES.MODE_CBC, IV).encrypt(text))


def _rsa_encrypt(text: bytes) -> str:
    result = pow(int(text[::-1].hex(), 16), int(PUB_KEY, 16), int(MODULUS, 16))
    return format(result, 'x').zfill(256)


def encrypt(payload: dict) -> dict:
    """把明文 payload 加密成 weapi 需要的 params / encSecKey 两个字段。"""
    text = json.dumps(payload, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    secret = ''.join(random.choice(BASE62) for _ in range(16)).encode('utf-8')
    params = _aes_encrypt(_aes_encrypt(text, KEY1), secret)
    return {'params': params.decode('utf-8'), 'encSecKey': _rsa_encrypt(secret)}


if __name__ == '__main__':
    out = encrypt({'s': 'test', 'type': 1, 'limit': 10})
    assert len(out['encSecKey']) == 256
    assert isinstance(out['params'], str) and out['params']
    print('encrypt self-test OK')