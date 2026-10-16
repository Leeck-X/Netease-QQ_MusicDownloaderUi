"""网易云音乐 API 封装。

实测结论（2026-10）：
  * weapi 签名接口可用：song/enhance/player/url/v1、v3/song/detail、v6/playlist/detail、song/lyric
  * /api/* 路径不接收 weapi 加密体（返回 400），必须走明文：
      搜索、账号校验、二维码登录、匿名注册
"""
import base64
import concurrent.futures
import hashlib
import json
import random

import requests

from encrypt import WEApiCipher, WEApiKeys
from models import Song

BASE = 'https://music.163.com'
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36')

# 匿名注册里用于混淆设备号的固定异或密钥
ID_XOR_KEY_1 = '3go8&$8*3*3h0k(2)2'

DETAIL_BATCH = 500        # 歌曲详情接口每批最多 id 数
DETAIL_MAX_WORKERS = 3    # 长歌单分批并发上限（过低无收益，过高易触发风控）


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

    def __init__(self, cookie: str = '', weapi_keys=None):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': UA,
            'Referer': BASE + '/',
            'Origin': BASE,
        })
        # weapi 加密密钥：来自 config.json 的 weapi_keys，缺失时回退内置默认值
        self._cipher = WEApiCipher(WEApiKeys.from_config(weapi_keys))
        self.set_cookie(cookie)

    def set_weapi_keys(self, weapi_keys) -> None:
        """替换 weapi 加密密钥；已登录的 cookie / 匿名 token 不受影响。"""
        self._cipher = WEApiCipher(WEApiKeys.from_config(weapi_keys))

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

    def _weapi(self, path: str, payload: dict, session=None) -> dict:
        """weapi 签名请求（加密 body）。

        session 给定时改用该会话发送，供并发请求避免共享 Session 竞争。
        """
        params = {'csrf_token': self._csrf_token()}
        resp = (session or self.session).post(
            BASE + path, data=self._cipher.encrypt(payload), params=params, timeout=20)
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
        """批量取歌曲详情（每 DETAIL_BATCH 首一批）。

        单批直接走主会话；多批时并发请求（各批独立会话），缩短长歌单加载时间；
        并发任一批失败则回退为串行，保证与旧行为一致。
        """
        ids = list(ids)
        if not ids:
            return []
        chunks = [ids[start:start + DETAIL_BATCH]
                  for start in range(0, len(ids), DETAIL_BATCH)]
        if len(chunks) == 1:
            raws = [self._detail_chunk(chunks[0], self.session)]
        else:
            raws = self._detail_chunks_concurrent(chunks)
            if raws is None:   # 并发失败，串行重试
                raws = [self._detail_chunk(chunk, self.session) for chunk in chunks]
        songs = []
        for raw in raws:
            songs.extend(raw or [])
        return [Song.from_raw(item) for item in songs]

    def _detail_chunk(self, chunk, session) -> list:
        """取一批歌曲详情的原始结构（该接口只接受 weapi 加密体）。"""
        res = session.post(
            BASE + '/weapi/v3/song/detail',
            data=self._cipher.encrypt({
                'c': json.dumps([{'id': i} for i in chunk]),
                'ids': json.dumps(chunk),
            }),
            params={'csrf_token': self._csrf_token()},
            timeout=20,
        )
        res.raise_for_status()
        return res.json().get('songs') or []

    def _detail_chunks_concurrent(self, chunks) -> list:
        """并发取多批详情并按原顺序返回；任一批失败返回 None 交给调用方串行回退。"""
        try:
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(DETAIL_MAX_WORKERS, len(chunks)),
                    thread_name_prefix='detail') as pool:
                jobs = [pool.submit(self._detail_chunk, chunk, self._new_session_like())
                        for chunk in chunks]
                return [job.result() for job in jobs]
        except Exception:  # noqa: BLE001 - 并发失败不影响可用性
            return None

    def _new_session_like(self):
        """为并发批请求复制一个会话（共享请求头与 cookie，避免并发共享 Session）。"""
        session = requests.Session()
        session.headers.update(self.session.headers)
        for cookie in self.session.cookies:
            session.cookies.set(cookie.name, cookie.value,
                                domain=cookie.domain or 'music.163.com',
                                path=cookie.path or '/')
        return session

    def clone_session(self):
        """对外暴露的会话复制，供上层并发请求使用（避免并发共享 Session）。"""
        return self._new_session_like()

    def song_by_id(self, value) -> list:
        """按歌曲 ID 取单曲（供粘贴单曲链接添加）。"""
        try:
            song_id = int(value)
        except (TypeError, ValueError):
            raise RuntimeError('歌曲 ID 无效')  # noqa: TRY003
        return self.song_detail([song_id])

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

    def song_url(self, song, level: str = 'exhigh', encode_type: str = 'mp3',
                 session=None) -> dict:
        if level in ('lossless', 'hires'):
            encode_type = 'flac'
        res = self._weapi('/weapi/song/enhance/player/url/v1', {
            'ids': json.dumps([song.id]),
            'level': level,
            'encodeType': encode_type,
        }, session=session)
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

    # ---------------- 我的歌单 / 我喜欢的音乐 ----------------
    def _my_playlists_raw(self) -> tuple[int, list]:
        """返回 (用户 ID, 歌单原始列表)。"""
        profile = self.account()
        if not profile:
            raise RuntimeError('请先登录网易云账号')
        uid = profile.get('userId')
        res = self._plain_get('/api/user/playlist', {'uid': uid, 'limit': 1000, 'offset': 0})
        return uid, (res.get('playlist') or [])

    def user_playlists(self) -> list[dict]:
        """返回当前账号的歌单，区分「我创建的」与「我收藏的」。"""
        uid, playlists = self._my_playlists_raw()
        result = []
        for item in playlists:
            creator = (item.get('creator') or {}).get('userId')
            result.append({
                'id': item.get('id'),
                'title': item.get('name') or '',
                'count': item.get('trackCount') or 0,
                'cover': item.get('coverImgUrl') or '',
                'kind': 'created' if creator == uid else 'fav',
            })
        return result

    def liked_songs(self) -> tuple[str, list]:
        """「我喜欢的音乐」用 specialType=5 标记定位，其歌单 ID 不等于用户 ID。"""
        uid, playlists = self._my_playlists_raw()
        special = next((item for item in playlists if item.get('specialType') == 5), None)
        return self.playlist_songs(special.get('id') if special else uid)

    def liked_keys(self) -> set:
        """「我喜欢的音乐」的歌曲标识集合，元素形如 ('netease', 歌曲ID)。"""
        uid, playlists = self._my_playlists_raw()
        special = next((item for item in playlists if item.get('specialType') == 5), None)
        if special is None:
            return set()
        detail = self.playlist_detail(special.get('id'))
        return {('netease', int(item['id']))
                for item in (detail.get('trackIds') or []) if item.get('id')}

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