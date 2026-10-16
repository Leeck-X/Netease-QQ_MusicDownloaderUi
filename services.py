"""后台服务层：线程调度 + 消息投递 + 各音源 / 下载 worker。

ui.py 只负责界面渲染与事件分派；所有阻塞调用（网络请求、下载、扫码轮询）
都经 ServiceHub 在守护线程里执行，完成后把 (tag, payload) 投递回主线程。

ServiceHub 不持有 cfg / api 的引用，而是通过 get_cfg / get_api 取当前值，
这样切换音源后 worker 永远拿到最新 provider，不会用到过期快照。
"""
import concurrent.futures
import io
import queue
import threading
import time

import qrcode
import requests
from PIL import Image

from downloader import Control, DownloadCancelled, Downloader, ProgressHub, UA
from models import LEVELS_BY_SOURCE, human_size

# ---------------- 消息 tag ----------------
TAG_LOG = 'log'
TAG_STATUS = 'status'
TAG_PROGRESS = 'progress'
TAG_SONGS = 'songs'
TAG_APPEND_SONGS = 'append_songs'
TAG_SEARCH_DONE = 'search_done'
TAG_MY_PLAYLISTS = 'my_playlists'
TAG_LIKED_KEYS = 'liked_keys'
TAG_DOWNLOAD_END = 'download_end'
TAG_LOGIN_OK = 'login_ok'
TAG_QQ_LOGIN_OK = 'qq_login_ok'
TAG_QR_IMAGE = 'qr_image'
TAG_DETAIL_COVER = 'detail_cover'
TAG_PROBE_DONE = 'probe_done'
TAG_CLOSE_QR = 'close_qr'
TAG_ACCOUNT = 'account'
TAG_ERROR = 'error'

PAGE_SIZE = 50   # 每次搜索 / 加载更多请求的条数
QR_TIMEOUT = 180   # 扫码轮询总时长（秒）
QR_INTERVAL = 2    # 扫码轮询间隔（秒）


class ServiceHub:
    def __init__(self, msg_queue, get_cfg, get_api):
        self.q = msg_queue
        self.get_cfg = get_cfg
        self.get_api = get_api
        # 搜索分页状态集中在此，避免主线程与 worker 各持一份导致读写分裂
        self.search_keyword = ''
        self.search_offset = 0
        self.search_busy = False
        self.qr_stop = False

    # ---------------- 调度与投递 ----------------
    def post(self, tag, payload=None):
        self.q.put((tag, payload))

    def start(self, func, *args):
        """在守护线程里执行 func，等价于原来的 ui._bg。"""
        threading.Thread(target=func, args=args, daemon=True).start()

    def drain(self, handler):
        """把队列里累积的消息一次性交给主线程 handler，避免 UI 逐条等待。"""
        while True:
            try:
                tag, payload = self.q.get_nowait()
            except queue.Empty:
                return
            handler(tag, payload)

    # ---------------- 搜索 / 歌单 ----------------
    def search_worker(self, keyword, offset, append, tree):
        try:
            songs = self.get_api().search(keyword, limit=PAGE_SIZE, offset=offset)
            tag = TAG_APPEND_SONGS if append else TAG_SONGS
            self.post(tag, (tree, songs))
            self.search_offset = offset + len(songs)
            self.post(TAG_SEARCH_DONE, len(songs) >= PAGE_SIZE)
            if append:
                self.post(TAG_LOG, f'追加 {len(songs)} 首（累计 {self.search_offset}）')
            else:
                self.post(TAG_LOG, f'共搜索到 {len(songs)} 首')
        except Exception as exc:  # noqa: BLE001
            self.post(TAG_SEARCH_DONE, False)   # 失败也要复位忙碌状态
            self.post(TAG_LOG, f'搜索失败：{exc}')

    def playlist_worker(self, playlist_id, tree):
        try:
            title, songs = self.get_api().playlist_songs(playlist_id)
            if not songs:
                self.post(TAG_LOG, '歌单为空或加载失败')
                return
            self.post(TAG_SONGS, (tree, songs))
            self.post(TAG_LOG, f'歌单《{title}》共 {len(songs)} 首')
        except Exception as exc:  # noqa: BLE001
            self.post(TAG_LOG, f'歌单加载失败：{exc}')

    def song_link_worker(self, value, tree):
        """按单曲链接解析出的 ID / MID 取单曲并追加到列表。"""
        try:
            songs = self.get_api().song_by_id(value)
        except Exception as exc:  # noqa: BLE001
            self.post(TAG_LOG, f'单曲解析失败：{exc}')
            return
        if not songs:
            self.post(TAG_LOG, '未找到该单曲（ID / MID 可能无效）')
            return
        self.post(TAG_APPEND_SONGS, (tree, songs))
        song = songs[0]
        self.post(TAG_LOG, f'已添加单曲：{song.name} - {song.artists}')

    # ---------------- 我的歌单 / 我喜欢的音乐 ----------------
    def liked_keys_worker(self):
        try:
            self.post(TAG_LIKED_KEYS, (True, self.get_api().liked_keys()))
        except Exception as exc:  # noqa: BLE001
            self.post(TAG_LIKED_KEYS, (False, str(exc)))

    def liked_worker(self, tree):
        try:
            title, songs = self.get_api().liked_songs()
            if not songs:
                self.post(TAG_LOG, '我喜欢的音乐为空')
                return
            self.post(TAG_SONGS, (tree, songs))
            self.post(TAG_LOG, f'《{title}》共 {len(songs)} 首')
        except Exception as exc:  # noqa: BLE001
            self.post(TAG_LOG, f'获取我喜欢的音乐失败：{exc}')

    def my_playlists_worker(self):
        try:
            self.post(TAG_MY_PLAYLISTS, (True, self.get_api().user_playlists()))
        except Exception as exc:  # noqa: BLE001
            self.post(TAG_MY_PLAYLISTS, (False, str(exc)))

    # ---------------- 歌曲详情（封面 / 音质探测） ----------------
    def load_cover(self, song, label, win):
        image = None
        try:
            if song.cover_url:
                resp = requests.get(song.cover_url, timeout=15, headers={'User-Agent': UA})
                resp.raise_for_status()   # 错误页不是图片，避免被当作封面
                image = Image.open(io.BytesIO(resp.content)).convert('RGB').resize((300, 300))
        except Exception as exc:  # noqa: BLE001
            self.post(TAG_LOG, f'封面加载失败：{exc}')
        if image is None:
            self.post(TAG_PROBE_DONE, (win, label, '（无封面）'))
            return
        self.post(TAG_DETAIL_COVER, (win, label, image))

    def probe_qualities(self, song, label, win):
        """逐档探测可用音质；网易云各档独立，并发探测显著缩短等待。"""
        api = self.get_api()
        encode_type = self.get_cfg().get('encode_type', 'mp3')
        levels = list(LEVELS_BY_SOURCE.get(song.source, LEVELS_BY_SOURCE['netease']))

        def probe(level, level_label, session=None):
            try:
                info = (api.song_url(song, level, encode_type, session=session)
                        if session is not None else api.song_url(song, level, encode_type))
                detail = f"{info.get('type', '').upper()} {human_size(info.get('size'))}"
                return f'{level_label}：可用（{detail}）'
            except Exception:  # noqa: BLE001 - 探测失败即视为不可用
                return f'{level_label}：不可用'

        # 网易云各档互不依赖，用克隆会话并发请求；QQ 走单线程事件循环，保持串行
        if song.source == 'netease' and len(levels) > 1:
            sessions = [api.clone_session() for _ in levels]
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=len(levels), thread_name_prefix='probe') as pool:
                jobs = [pool.submit(probe, level, level_label, session)
                        for (level, level_label), session in zip(levels, sessions)]
                rows = [job.result() for job in jobs]
        else:
            rows = [probe(level, level_label) for level, level_label in levels]
        self.post(TAG_PROBE_DONE, (win, label, '\n'.join(rows)))

    # ---------------- 下载 ----------------
    def download_worker(self, songs, level=None, options=None, control=None):
        """多线程并发下载：线程数取自配置 workers（1~8）。

        - 取址等接口调用在 Downloader 内部串行，下载传输并行
        - ProgressHub 聚合整体进度与瞬时速度，UI 每秒最多刷新约 5 次
        - 暂停 / 取消对所有下载线程同时生效
        """
        control = control or Control()
        cfg = self.get_cfg()
        workers = max(1, min(8, int(cfg.get('workers', 3))))
        downloader = Downloader(
            self.get_api(), cfg,
            on_log=lambda text: self.post(TAG_LOG, text),
            control=control,
        )
        total = len(songs)
        hub = ProgressHub(total, lambda ratio, speed: self.post(TAG_PROGRESS, (ratio, speed)))
        state = {'success': 0, 'failed': [], 'cancelled': False, 'done': 0}
        lock = threading.Lock()

        def task(index, song):
            control.wait_if_paused()
            if control.cancelled:
                raise DownloadCancelled()
            self.post(TAG_LOG, f'[{index + 1}/{total}] {song.name} - {song.artists}')
            try:
                downloader.download(song, level=level, options=options,
                                    index=index + 1,
                                    on_progress=lambda r, d, t, _i=index: hub.update(_i, r, d))
                with lock:
                    state['success'] += 1
            except DownloadCancelled:
                with lock:
                    state['cancelled'] = True
                raise
            except Exception as exc:  # noqa: BLE001
                with lock:
                    state['failed'].append((song, str(exc)))
                self.post(TAG_LOG, f'  失败：{song.name} - {exc}')
            finally:
                hub.finish(index)   # 无论成功 / 失败，该格都记满，保证整体进度单调
                with lock:
                    state['done'] += 1
                    done_count = state['done']
                self.post(TAG_STATUS, f'已完成 {done_count}/{total} · 并发 {workers} 线程')

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers,
                                                   thread_name_prefix='dl') as pool:
            futures = [pool.submit(task, i, song) for i, song in enumerate(songs)]
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except DownloadCancelled:
                    pass   # 已在 task 内标记，未开始的任务也会快速退出
                except Exception:  # noqa: BLE001 - 已在 task 内记录
                    pass
        hub.final()
        downloader.close()   # 释放本次下载持有的网络会话，避免连接池长期占用
        # 收尾一律回到界面线程处理（含取消后的清理询问）
        self.post(TAG_DOWNLOAD_END, {
            'total': total, 'success': state['success'], 'failed': state['failed'],
            'cancelled': state['cancelled'], 'downloader': downloader,
        })

    # ---------------- 登录 ----------------
    def check_account(self):
        cfg = self.get_cfg()
        api = self.get_api()
        source = cfg.get('source', 'netease')
        # 匿名 token 已存在时不再重复注册（退出登录清空 cookie 后 deviceId 消失，会重新注册）
        if (source == 'netease' and cfg.get('use_anonimous') and not cfg.get('cookie')
                and not api.session.cookies.get('deviceId')):
            try:
                api.register_anonimous()
                self.post(TAG_LOG, '匿名注册成功（已获取匿名 token）')
            except Exception as exc:  # noqa: BLE001
                self.post(TAG_LOG, f'匿名注册失败（不影响使用）：{exc}')
        try:
            profile = api.account()
        except Exception as exc:  # noqa: BLE001
            self.post(TAG_LOG, f'账号校验失败：{exc}')
            profile = None
        # 头像拉取只走网易云（QQ provider 没有 .session）
        image = self.fetch_avatar(profile.get('avatarUrl')) if (profile and source == 'netease') else None
        self.post(TAG_ACCOUNT, (profile, image))

    def fetch_avatar(self, url):
        """拉取头像缩略图；失败不影响登录信息展示。"""
        if not url:
            return None
        try:
            resp = self.get_api().session.get(url + '?param=64y64', timeout=10)
            return Image.open(io.BytesIO(resp.content)).convert('RGB').resize((36, 36))
        except Exception:  # noqa: BLE001
            return None

    def qq_cookie_apply(self, cookie):
        """QQ Cookie 登录：只做 set_cookie 并回主线程通知，避免后台线程写 Tk。"""
        try:
            self.get_api().set_cookie(cookie)
        except Exception as exc:  # noqa: BLE001
            self.post(TAG_ERROR, f'QQ 音乐登录失败：{exc}')
            return
        self.post(TAG_QQ_LOGIN_OK, None)

    # ---------------- 扫码登录 ----------------
    def _poll_qr(self, should_stop, step):
        """扫码轮询骨架：每 QR_INTERVAL 秒执行一次 step()，step 返回 True 表示流程结束。

        网易云与 QQ 两个扫码 worker 共用此循环，避免重复维护超时与异常处理。
        """
        deadline = time.time() + QR_TIMEOUT
        while not should_stop() and time.time() < deadline:
            time.sleep(QR_INTERVAL)
            if should_stop():
                return
            try:
                if step():
                    return
            except Exception as exc:  # noqa: BLE001 - 轮询失败继续等待
                self.post(TAG_LOG, f'轮询失败：{exc}')

    def qr_worker(self, win, label):
        api = self.get_api()
        try:
            unikey = api.qrcode_unikey()
        except Exception as exc:  # noqa: BLE001
            self.post(TAG_LOG, f'获取二维码失败：{exc}')
            return
        if not unikey:
            self.post(TAG_LOG, '获取二维码失败：unikey 为空')
            return
        image = qrcode.make(f'https://music.163.com/login?codekey={unikey}').convert('RGB')
        self.post(TAG_QR_IMAGE, (win, label, image))

        def step():
            result = api.qrcode_check(unikey)
            code = result.get('code')
            if code == 800:
                self.post(TAG_LOG, '二维码已过期，请重新获取')
                return True
            if code == 802:
                self.post(TAG_STATUS, '已扫码，请在手机上确认')
            elif code == 803:
                cookie = result.get('cookie') or api.cookie_str()
                self.post(TAG_LOGIN_OK, cookie)
                self.post(TAG_CLOSE_QR, win)
                return True
            return False

        self._poll_qr(lambda: self.qr_stop, step)

    def qq_qr_worker(self, win, label, hint, session, login_type):
        api = self.get_api()
        if session['stop'] or not win.winfo_exists():
            return
        try:
            data, _identifier = api.qr_login_start(login_type)
        except Exception as exc:  # noqa: BLE001
            self.post(TAG_LOG, f'获取二维码失败：{exc}')
            return
        try:
            image = Image.open(io.BytesIO(data)).convert('RGB')
        except Exception as exc:  # noqa: BLE001
            self.post(TAG_LOG, f'二维码图片解析失败：{exc}')
            return
        self.post(TAG_QR_IMAGE, (win, label, image))

        def step():
            event, note = api.qr_login_poll()
            if event in ('scan', 'conf'):
                self.post(TAG_PROBE_DONE, (win, hint, note))
            elif event == 'done':
                self.post(TAG_QQ_LOGIN_OK, None)
                self.post(TAG_CLOSE_QR, win)
                return True
            elif event in ('timeout', 'refuse'):
                self.post(TAG_PROBE_DONE, (win, hint, note))
                return True
            return False

        self._poll_qr(lambda: session['stop'] or not win.winfo_exists(), step)
