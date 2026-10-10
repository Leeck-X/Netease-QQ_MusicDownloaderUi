"""网易云音乐 API 封装。

实测结论（2026-10）：
  * weapi 签名接口可用：song/enhance/player/url/v1、v3/song/detail、v6/playlist/detail、song/lyric
  * /api/* 路径不接收 weapi 加密体（返回 400），必须走明文：
      搜索、账号校验、二维码登录、匿名注册
"""
import base64
import hashlib
import json
import random
import time

import requests

from encrypt import encrypt
from models import Song

BASE = 'https://music.163.com'
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36')

# 匿名注册里用于混淆设备号的固定异或密钥
ID_XOR_KEY_1 = '3go8&$8*3*3h0k(2)2'


def parse_cookie(cookie_str: str) -> dict:
    result = {}
    for part in (cookie_str or '').split(';'):
        part = part.strip()
        if not part or '=' not in part:
            continue
        key, value = part.split('=', 1)
        result[key.strip()] = value.strip()
    return result


def _cloudmusic_dll_encode_id(some_id: str) -> str:
    xored = ''.join(
        chr(ord(ch) ^ ord(ID_XOR_KEY_1[i % len(ID_XOR_KEY_1)]))
        for i, ch in enumerate(some_id)
    )
    return base64.b64encode(hashlib.md5(xored.encode('utf-8')).digest()).decode('utf-8')


def _generate_device_id() -> str:
    return ''.join(random.choice('0123456789abcdef') for _ in range(16))


class NetEaseAPI:
    source = 'netease'

    def __init__(self, cookie: str = ''):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': UA,
            'Referer': BASE + '/',
            'Origin': BASE,
        })
        self.set_cookie(cookie)

    # ---------------- Cookie ----------------
    def set_cookie(self, cookie_str: str) -> None:
        self.session.cookies.clear()
        for key, value in parse_cookie(cookie_str).items():
            self.session.cookies.set(key, value, domain='music.163.com')
        # 默认客户端标识，降低被风控的概率
        if not self.session.cookies.get('os'):
            self.session.cookies.set('os', 'pc', domain='music.163.com')
        if not self.session.cookies.get('appver'):
            self.session.cookies.set('appver', '8.9.75', domain='music.163.com')

    def cookie_str(self) -> str:
        return '; '.join(f'{c.name}={c.value}' for c in self.session.cookies)

    # ---------------- 基础请求 ----------------
    def _csrf_token(self) -> str:
        """遍历 cookie 取 __csrf，避免同名多域 cookie 触发 CookieConflictError。"""
        for cookie in self.session.cookies:
            if cookie.name == '__csrf':
                return cookie.value
        return ''

    def _weapi(self, path: str, payload: dict) -> dict:
        """weapi 签名请求（加密 body）。"""
        params = {'csrf_token': self._csrf_token()}
        resp = self.session.post(BASE + path, data=encrypt(payload), params=params, timeout=20)
        resp.raise_for_status()
        return resp.json()

    def _plain_get(self, path: str, params: dict = None) -> dict:
        resp = self.session.get(BASE + path, params=params or {}, timeout=20)
        resp.raise_for_status()
        return resp.json()

    def _plain_post(self, path: str, data: dict = None, cookies: dict = None) -> dict:
        resp = self.session.post(BASE + path, data=data or {}, cookies=cookies, timeout=20)
        resp.raise_for_status()
        return resp.json()

    # ---------------- 业务接口 ----------------
    def search(self, keyword: str, limit: int = 50, offset: int = 0) -> list:
        """明文搜索拿到 id 列表，再用详情接口补齐封面/时长/收费/大小信息。"""
        res = self._plain_get('/api/search/get/web', {
            's': keyword, 'type': 1, 'limit': limit, 'offset': offset,
        })
        songs = (res.get('result') or {}).get('songs') or []
        ids = [item['id'] for item in songs if item.get('id')]
        if not ids:
            return []
        try:
            detail = self.song_detail(ids)
        except Exception:  # noqa: BLE001 - 详情失败则回退到明文结果
            return [Song.from_raw(item) for item in songs]
        return detail or [Song.from_raw(item) for item in songs]

    def song_detail(self, ids) -> list:
        ids = list(ids)
        songs = []
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            res = self._weapi('/weapi/v3/song/detail', {
                'c': json.dumps([{'id': i} for i in chunk]),
                'ids': json.dumps(chunk),
            })
            songs.extend(res.get('songs') or [])
            time.sleep(0.3)
        return [Song.from_raw(item) for item in songs]

    def playlist_detail(self, playlist_id) -> dict:
        res = self._weapi('/weapi/v6/playlist/detail', {
            'id': playlist_id, 'n': 1000, 's': 8,
        })
        return res.get('playlist') or {}

    def playlist_songs(self, playlist_id) -> tuple[str, list]:
        """返回 (歌单名, 歌曲列表)，优先用详情接口补全大小与封面。"""
        playlist = self.playlist_detail(playlist_id)
        title = playlist.get('name') or ''
        tracks = playlist.get('tracks') or []
        track_ids = [item['id'] for item in (playlist.get('trackIds') or []) if item.get('id')]
        if not track_ids:
            return title, [Song.from_raw(item) for item in tracks]
        try:
            details = self.song_detail(track_ids)
        except Exception:  # noqa: BLE001
            details = []
        if not details:
            details = [Song.from_raw(item) for item in tracks]
        return title, details

    def song_url(self, song, level: str = 'exhigh', encode_type: str = 'mp3') -> dict:
        if level in ('lossless', 'hires'):
            encode_type = 'flac'
        res = self._weapi('/weapi/song/enhance/player/url/v1', {
            'ids': json.dumps([song.id]),
            'level': level,
            'encodeType': encode_type,
        })
        data = res.get('data') or []
        item = data[0] if data else {}
        if not item.get('url'):
            raise RuntimeError('无版权或该音质不可用')
        return {
            'url': item['url'],
            'type': (item.get('type') or 'mp3').lower(),
            'size': item.get('size') or 0,
        }

    def lyric(self, song) -> dict:
        res = self._weapi('/weapi/song/lyric', {
            'id': song.id, 'lv': -1, 'kv': -1, 'tv': -1,
        })
        return {
            'lyric': (res.get('lrc') or {}).get('lyric') or '',
            'trans': (res.get('tlyric') or {}).get('lyric') or '',
        }

    def account(self):
        """返回当前登录用户的 profile；未登录返回 None。"""
        return self._plain_get('/api/nuser/account/get').get('profile')

    # ---------------- 扫码登录 ----------------
    def qrcode_unikey(self):
        return self._plain_get('/api/login/qrcode/unikey', {'type': 1}).get('unikey')

    def qrcode_check(self, key: str) -> dict:
        return self._plain_get('/api/login/qrcode/client/login', {'key': key, 'type': 1})

    # ---------------- 匿名注册 ----------------
    def register_anonimous(self) -> dict:
        """用设备号换取匿名 token，失败不影响匿名浏览/下载。

        设备相关 cookie 只在本次请求使用，避免污染后续请求的 os/appver。
        """
        device_id = _generate_device_id()
        encoded = base64.b64encode(
            f'{device_id} {_cloudmusic_dll_encode_id(device_id)}'.encode('utf-8')
        ).decode('utf-8')
        result = self._plain_post(
            '/api/register/anonimous',
            {'username': encoded},
            cookies={'deviceId': device_id, 'os': 'android', 'appver': '8.9.70'},
        )
        self.session.cookies.set('deviceId', device_id, domain='music.163.com')
        return result