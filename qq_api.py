"""QQ 音乐音源封装，基于现成库 qqmusic-api-python。

该库是纯异步实现，本模块用一个专属后台线程常驻事件循环与 Client，
对外提供与网易云音源一致的同步接口，方便被 Tkinter 的工作线程调用。

用法要点：
- 搜索/歌单走 `music.search.SearchCgiService` / `music.srfDissInfo.DissInfo`
- 播放地址走 `music.vkey.GetVkey`，purl 需拼上 CDN 域名
- 登录支持扫码（QQ / 微信 / 手机）与 Cookie 导入，凭证以 Credential 结构持久化
"""
from __future__ import annotations

import asyncio
import threading

from qqmusic_api import Client, Credential
from qqmusic_api.models.login import QRCodeLoginEvents, QRLoginType
from qqmusic_api.modules.search import SearchType
from qqmusic_api.modules.song import SongFileInfo, SongFileType

from api import parse_cookie
from models import Song, strip_highlight

# 档位 -> QQ 文件类型（母带为加密格式，无法直接下载，统一回退到无损）
LEVEL_FILE_TYPES = {
    'standard': SongFileType.MP3_128,
    'exhigh': SongFileType.MP3_320,
    'lossless': SongFileType.FLAC,
    'hires': SongFileType.FLAC,
}
# 档位 -> 文件大小取值键（对应 Song.sizes）
LEVEL_SIZE_KEYS = {
    'standard': '128mp3',
    'exhigh': '320mp3',
    'lossless': 'flac',
    'hires': 'hires',
}
# 难以取到 origin CDN 时的兜底域名
FALLBACK_CDN = 'https://isure.stream.qqmusic.com/'

QR_TYPES = {'qq': QRLoginType.QQ, 'wx': QRLoginType.WX, 'mobile': QRLoginType.MOBILE}


class _LoopThread:
    """专属线程 + 常驻事件循环，承载库的异步 Client。"""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.client: Client | None = None
        self._thread = threading.Thread(target=self._serve, name='qq-api-loop', daemon=True)
        self._thread.start()

    def _serve(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run(self, coro):
        """在后台循环里同步执行协程并返回结果（异常原样抛出）。"""
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()


class QQMusicAPI:
    """QQ 音乐音源实现。"""

    source = 'qq'

    def __init__(self, credential: dict | None = None):
        self._rt = _LoopThread()
        self._credential = Credential.model_validate(credential) if credential else Credential()
        self._cdn = FALLBACK_CDN
        self._pending_qr = None

    # ---------------- 内部：异步调度 ----------------
    async def _ensure(self) -> Client:
        if self._rt.client is None:
            self._rt.client = Client(credential=self._credential)
        else:
            self._rt.client.credential = self._credential
        return self._rt.client

    async def _dispatch(self, job):
        return await job(await self._ensure())

    def _run(self, job):
        return self._rt.run(self._dispatch(job))

    # ---------------- 登录态 ----------------
    def is_logged_in(self) -> bool:
        return self._credential.is_valid()

    def credential_dict(self) -> dict:
        """导出可写入 config.json 的凭证字典。"""
        return self._credential.model_dump()

    def set_credential(self, data: dict | None):
        self._credential = Credential.model_validate(data) if data else Credential()

    def cookie_str(self) -> str:
        """QQ 音乐不使用 cookie 字符串，返回空串以兼容统一接口。"""
        return ''

    def set_cookie(self, cookie: str):
        """从浏览器 cookie 导入登录态（需含 uin 与 qm_keyst/qqmusic_key）。"""
        jar = parse_cookie(cookie)
        uin = jar.get('uin') or jar.get('wxuin') or jar.get('luin') or ''
        key = jar.get('qm_keyst') or jar.get('qqmusic_key') or ''

        def _clean(value: str) -> str:
            value = (value or '').strip().strip('"')
            return value[1:] if value.startswith('o') and value[1:].isdigit() else value

        uin, key = _clean(uin), _clean(key)
        if not uin.isdigit() or not key:
            raise ValueError('cookie 中缺少 uin 或 qm_keyst / qqmusic_key')
        self._credential = Credential(musicid=int(uin), str_musicid=uin, musickey=key, login_type=2)

        async def job(client):
            return await client.login.check_expired()

        if self._run(job):
            self._credential = Credential()
            raise ValueError('cookie 已失效，请重新获取或改用扫码登录')

    def qr_login_start(self, login_type: str = 'qq') -> tuple[bytes, str]:
        """生成登录二维码，返回 (PNG 字节, 标识符)。"""
        qr_type = QR_TYPES.get(login_type, QRLoginType.QQ)

        async def job(client):
            return await client.login.get_qrcode(qr_type)

        qr = self._run(job)
        self._pending_qr = qr
        return qr.data, qr.identifier

    def qr_login_poll(self) -> tuple[str, str]:
        """轮询扫码状态，返回 (事件名, 提示)。事件名见 _EVENT_NAMES。"""
        if self._pending_qr is None:
            return 'other', '尚未生成二维码'

        async def job(client):
            return await client.login.check_qrcode(self._pending_qr)

        try:
            result = self._run(job)
        except Exception as exc:  # noqa: BLE001
            return 'other', str(exc)

        event = _EVENT_NAMES.get(result.event, 'other')
        note = {
            'done': '登录成功',
            'scan': '二维码未扫描',
            'conf': '已扫描，请在手机上确认',
            'timeout': '二维码已过期，请重新生成',
            'refuse': '已取消登录',
        }.get(event, event)
        if result.done and result.credential:
            self._credential = result.credential
            self._pending_qr = None
        elif event in ('timeout', 'refuse'):
            self._pending_qr = None
        return event, note

    def account(self) -> dict | None:
        """获取当前账号信息，未登录返回 None。"""
        if not self.is_logged_in():
            return None

        async def job(client):
            try:
                return await client.execute(client.user.get_vip_info())
            except Exception:  # noqa: BLE001
                return None

        vip = self._run(job)
        nickname = ''
        vip_type = 0
        if vip is not None:
            nickname = (getattr(vip, 'nickname', '') or getattr(vip, 'name', '')
                        or getattr(vip, 'vip_name', ''))
            if getattr(vip, 'is_vip', 0) or getattr(vip, 'vip_type', 0):
                vip_type = 11
        return {
            'nickname': nickname or 'QQ音乐用户',
            'userId': self._credential.musicid,
            'vipType': vip_type,
        }

    # ---------------- 曲库 ----------------
    def search(self, keyword: str, limit: int = 50, offset: int = 0) -> list[Song]:
        page = offset // limit + 1 if limit else 1

        async def job(client):
            req = client.search.search_by_type(keyword, SearchType.SONG, limit, page)
            return await client.execute(req)

        resp = self._run(job)
        return [Song.from_qq(item.model_dump()) for item in (resp.song or [])]

    def playlist_songs(self, playlist_id) -> tuple[str, list[Song]]:
        async def job(client):
            req = client.songlist.get_detail(int(playlist_id), num=1000, page=1)
            return await client.execute(req)

        resp = self._run(job)
        info = getattr(resp, 'info', None)
        title = strip_highlight(getattr(info, 'title', '') or '') if info is not None else ''
        songs = [Song.from_qq(item.model_dump()) for item in (resp.songs or [])]
        return title, songs

    def song_url(self, song: Song, level: str = 'exhigh', encode_type: str = 'mp3') -> dict:
        file_type = LEVEL_FILE_TYPES.get(level, SongFileType.MP3_320)
        is_lossless = level in ('lossless', 'hires')

        async def job(client):
            if self._cdn == FALLBACK_CDN:
                try:
                    dispatched = await client.execute(client.song.get_cdn_dispatch())
                    sip = getattr(dispatched, 'sip', None) or []
                    if sip:
                        self._cdn = sip[0] if sip[0].endswith('/') else sip[0] + '/'
                except Exception:  # noqa: BLE001
                    pass
            info = SongFileInfo(mid=song.mid, file_type=file_type,
                                media_mid=song.media_mid or None)
            return await client.execute(client.song.get_song_urls([info], file_type=file_type))

        resp = self._run(job)
        item = (resp.data or [None])[0]
        if item is None or not item.purl:
            code = getattr(item, 'result', None)
            raise RuntimeError(f'无法获取播放地址（code={code}），可能为付费/版权受限，请登录后重试')
        return {
            'url': self._cdn + item.purl,
            'type': 'flac' if is_lossless else 'mp3',
            'size': (song.sizes or {}).get(LEVEL_SIZE_KEYS.get(level, ''), 0) or 0,
        }

    def lyric(self, song: Song) -> dict:
        async def job(client):
            return await client.execute(client.lyric.get_lyric(song.mid, 1))

        resp = self._run(job)
        return {'lyric': resp.lyric or '', 'trans': resp.trans or ''}


_EVENT_NAMES = {
    QRCodeLoginEvents.DONE: 'done',
    QRCodeLoginEvents.SCAN: 'scan',
    QRCodeLoginEvents.CONF: 'conf',
    QRCodeLoginEvents.TIMEOUT: 'timeout',
    QRCodeLoginEvents.REFUSE: 'refuse',
}