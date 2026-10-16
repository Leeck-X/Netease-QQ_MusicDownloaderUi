"""下载器：取地址 → 流式下载 → 进度 → 重试 → 歌词 → 封面与标签。

支持多线程并发调用：produced / pending 由锁保护；
进度回调签名为 on_progress(ratio, done, total)，并做了节流（变化 ≥1% 才上报）。
"""
import os
import threading
import time
from collections import deque

import requests

from models import TYPE_EXT, sanitize

CHUNK = 128 * 1024
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36')


def _is_png(cover: bytes) -> bool:
    """按 PNG 魔数判断封面格式，用于写出正确的 MIME（否则一律标成 JPEG 会与实际不符）。"""
    return cover[:8] == b'\x89PNG\r\n\x1a\n'


class DownloadError(Exception):
    pass


class DownloadCancelled(Exception):
    """用户主动取消下载（不是失败，不参与重试）。"""


class Control:
    """下载暂停 / 取消控制，供界面线程与多个下载线程共享。"""

    def __init__(self):
        self._cancel = threading.Event()
        self._pause = threading.Event()

    @property
    def paused(self) -> bool:
        return self._pause.is_set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def pause(self) -> None:
        self._pause.set()

    def resume(self) -> None:
        self._pause.clear()

    def cancel(self) -> None:
        self._cancel.set()
        self._pause.clear()   # 取消时先解除暂停，避免下载线程卡在等待

    def wait_if_paused(self) -> None:
        while self._pause.is_set() and not self._cancel.is_set():
            time.sleep(0.1)


def _idle_progress(_ratio, _done, _total):
    """默认进度回调（丢弃）。"""


class Downloader:
    def __init__(self, api, cfg, on_log=None, on_progress=None, control=None):
        self.api = api
        self.cfg = cfg
        self.on_log = on_log or (lambda message: None)
        self.on_progress = on_progress or _idle_progress
        self.control = control or Control()
        self._lock = threading.Lock()      # 保护 produced / pending
        self._api_lock = threading.Lock()  # 取址 / 歌词等接口调用串行化，避免会话竞争
        self._produced = []                # 已完成文件（音频 / 歌词）
        self._pending = set()              # 下载中的 .part 文件
        self._cover_session = requests.Session()  # 封面下载复用连接池
        # 无会话音源（如 QQ）下载时复用的连接池；有会话的音源仍走其会话以保留登录 cookie
        self._dl_session = requests.Session()
        self._dl_session.headers.update({'User-Agent': UA})

    # ---- 线程安全的状态访问 ----
    @property
    def produced(self) -> list:
        with self._lock:
            return list(self._produced)

    @property
    def pending(self) -> set:
        with self._lock:
            return set(self._pending)

    def download(self, song, level=None, encode_type=None, options=None,
                 on_progress=None, index: int = 0) -> str:
        progress = on_progress or self.on_progress
        level = level or self.cfg['level']
        encode_type = encode_type or self.cfg['encode_type']
        options = options or {}
        self._check_control()

        # 取址走接口会话，多线程下串行调用（耗时可忽略，下载本身仍是并行的）
        with self._api_lock:
            info = self.api.song_url(song, level, encode_type)
        url = info.get('url')
        if not url:
            raise DownloadError('无版权或该音质不可用')

        ext = TYPE_EXT.get((info.get('type') or '').lower(), '.mp3')
        path = self._target_path(song, ext, index)
        os.makedirs(os.path.dirname(path), exist_ok=True)

        if self.cfg.get('skip_existing') and os.path.exists(path) and os.path.getsize(path) > 0:
            self.on_log(f'  跳过（已存在）：{self._rel_name(path)}')
            return path

        retry = int(self.cfg.get('retry', 3))
        last_error = None
        for attempt in range(1, retry + 1):
            try:
                # 仅同 URL 的重试才续传 .part；首次尝试删除陈旧半成品，
                # 避免换音质后把不同码率的旧 .part 拼接导致文件损坏
                self._fetch(url, path, song, progress, resume=(attempt > 1))
                last_error = None
                break
            except DownloadCancelled:
                raise
            except Exception as exc:  # noqa: BLE001 - 需要重试任何网络异常
                last_error = exc
                self.on_log(f'  第 {attempt} 次下载失败：{exc}')
                time.sleep(1)
        if last_error is not None:
            raise DownloadError(f'下载失败：{last_error}')

        # 单次下载选项优先于全局配置
        want_lyric = options.get('save_lyric', self.cfg.get('save_lyric'))
        embed_tag = options.get('embed_tag', self.cfg.get('embed_tag'))
        embed_cover = options.get('embed_cover', self.cfg.get('embed_cover'))
        embed_lyric = options.get('embed_lyric', self.cfg.get('embed_lyric'))
        # 需要写 .lrc 或嵌入标签时只取一次歌词，避免重复请求
        lyric_text = ''
        if want_lyric or embed_lyric:
            with self._api_lock:
                lyric_text = self._fetch_lyric(song)
        if want_lyric and lyric_text:
            self._save_lyric_file(path, lyric_text)
        if embed_tag or embed_cover or embed_lyric:
            self._write_tags(song, path, embed_cover,
                             lyric_text if embed_lyric else '')

        self.on_log(f'  已保存：{self._rel_name(path)}')
        self._check_control()   # 取消请求若在收尾阶段到达，按取消处理
        return path

    def _target_path(self, song, ext: str, index: int) -> str:
        """按命名模板 + 文件组织方式生成完整保存路径（子目录不存在时由调用方创建）。"""
        out_dir = self.cfg['download_dir']
        structure = self.cfg.get('folder_structure', 'none')
        folders = []
        if structure in ('artist', 'artist_album'):
            folders.append(sanitize(song.artists or '未知歌手'))
        if structure in ('album', 'artist_album'):
            folders.append(sanitize(song.album or '未知专辑'))
        base = song.filename(self.cfg['name_template'], index)
        return os.path.join(out_dir, *folders, base + ext)

    def _rel_name(self, path: str) -> str:
        """相对下载目录展示文件名（分文件夹后带子目录路径，日志更清晰）。"""
        try:
            return os.path.relpath(path, self.cfg['download_dir'])
        except ValueError:  # 跨盘符时 relpath 会失败，退回文件名
            return os.path.basename(path)

    def cleanup(self) -> int:
        """删除本次下载产生的文件（含未完成的 .part），返回删除数量。"""
        with self._lock:
            targets = list(self._produced) + list(self._pending)
            self._produced, self._pending = [], set()
        removed = 0
        for path in targets:
            try:
                if path and os.path.exists(path):
                    os.remove(path)
                    removed += 1
            except OSError as exc:  # noqa: BLE001 - 删除失败不影响主流程
                self.on_log(f'  删除失败：{path}（{exc}）')
        return removed

    def close(self) -> None:
        """释放下载器自己持有的网络会话（音源会话由其 Provider 长期持有，不在此关闭）。"""
        for session in (self._cover_session, self._dl_session):
            try:
                session.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响主流程
                pass

    # ---------------- 内部实现 ----------------
    def _check_control(self) -> None:
        """暂停时在此等待；取消时抛异常中断下载。"""
        self.control.wait_if_paused()
        if self.control.cancelled:
            raise DownloadCancelled('已取消下载')

    def _fetch(self, url: str, path: str, song, progress, resume: bool = False) -> None:
        referer = 'https://y.qq.com/' if getattr(song, 'source', 'netease') == 'qq' else 'https://music.163.com/'
        headers = {'User-Agent': UA, 'Referer': referer}
        session = getattr(self.api, 'session', None) or self._dl_session
        tmp_path = path + '.part'
        with self._lock:
            # 失败 / 取消时 .part 保留在 pending：重试可 Range 续传，取消清理时可删除
            self._pending.add(tmp_path)
        # 暂停后恢复下载：优先用 Range 续传已写入的部分
        offset = os.path.getsize(tmp_path) if os.path.exists(tmp_path) else 0
        if offset and not resume:
            os.remove(tmp_path)
            offset = 0
        if offset:
            headers['Range'] = f'bytes={offset}-'
        done = offset
        with session.get(url, headers=headers, stream=True, timeout=30) as resp:
            resp.raise_for_status()
            if offset and resp.status_code != 206:   # 服务端不支持断点续传
                offset = 0
                done = 0
            total = int(resp.headers.get('Content-Length') or 0) + offset
            last_ratio = -1.0
            if total:
                progress(min(done / total, 1.0), done, total)
            with open(tmp_path, 'ab' if offset else 'wb') as handle:
                for chunk in resp.iter_content(CHUNK):
                    self._check_control()
                    if not chunk:
                        continue
                    handle.write(chunk)
                    done += len(chunk)
                    if total:
                        ratio = min(done / total, 1.0)
                        if ratio - last_ratio >= 0.01 or ratio >= 1.0:
                            last_ratio = ratio
                            progress(ratio, done, total)
        self._check_control()
        os.replace(tmp_path, path)
        with self._lock:
            self._pending.discard(tmp_path)
            self._produced.append(path)
        progress(1.0, done, done)

    def _fetch_lyric(self, song) -> str:
        """取歌词文本（原文 + 翻译，若有）；失败返回空串，不影响主流程。"""
        try:
            data = self.api.lyric(song)
        except Exception as exc:  # noqa: BLE001 - 歌词失败不影响主流程
            self.on_log(f'  歌词获取失败：{exc}')
            return ''
        lrc = (data.get('lyric') or '').rstrip()
        if not lrc.strip():
            return ''
        translated = (data.get('trans') or '').rstrip()
        return lrc + ('\n\n' + translated if translated.strip() else '')

    def _save_lyric_file(self, audio_path: str, content: str) -> None:
        """把歌词写成与音频同名的 .lrc 文件。"""
        try:
            lrc_path = os.path.splitext(audio_path)[0] + '.lrc'
            with open(lrc_path, 'w', encoding='utf-8') as handle:
                handle.write(content)
            with self._lock:
                self._produced.append(lrc_path)
        except Exception as exc:  # noqa: BLE001 - 歌词失败不影响主流程
            self.on_log(f'  歌词保存失败：{exc}')

    def _write_tags(self, song, path: str, embed_cover: bool = True,
                    lyric: str = '') -> None:
        try:
            cover = b''
            if embed_cover and song.cover_url:
                resp = self._cover_session.get(
                    song.cover_url, timeout=15,
                    headers={'User-Agent': UA, 'Referer': 'https://y.qq.com/'}
                    if song.source == 'qq' else {'User-Agent': UA})
                resp.raise_for_status()   # 错误页不是图片，避免被当作封面嵌入
                cover = resp.content
            ext = os.path.splitext(path)[1].lower()
            if ext == '.mp3':
                self._tag_mp3(path, song, cover, lyric)
            elif ext == '.flac':
                self._tag_flac(path, song, cover, lyric)
            elif ext == '.m4a':
                self._tag_m4a(path, song, cover, lyric)
        except Exception as exc:  # noqa: BLE001 - 标签失败不影响文件可用
            self.on_log(f'  标签写入失败：{exc}')

    @staticmethod
    def _tag_mp3(path: str, song, cover: bytes, lyric: str = '') -> None:
        from mutagen.id3 import APIC, ID3, ID3NoHeaderError, TALB, TIT2, TPE1, USLT
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        tags.delall('TIT2')
        tags.add(TIT2(encoding=3, text=song.name))
        tags.delall('TPE1')
        tags.add(TPE1(encoding=3, text=song.artists))
        tags.delall('TALB')
        tags.add(TALB(encoding=3, text=song.album))
        if lyric:
            tags.delall('USLT')
            tags.add(USLT(encoding=3, lang='eng', desc='', text=lyric))
        if cover:
            tags.delall('APIC')
            tags.add(APIC(encoding=3, mime='image/png' if _is_png(cover) else 'image/jpeg',
                          type=3, desc='Cover', data=cover))
        tags.save(path)

    @staticmethod
    def _tag_flac(path: str, song, cover: bytes, lyric: str = '') -> None:
        from mutagen.flac import FLAC, Picture
        audio = FLAC(path)
        audio['title'] = song.name
        audio['artist'] = song.artists
        audio['album'] = song.album
        if lyric:
            audio['LYRICS'] = lyric
        if cover:
            picture = Picture()
            picture.type = 3
            picture.mime = 'image/png' if _is_png(cover) else 'image/jpeg'
            picture.data = cover
            audio.clear_pictures()
            audio.add_picture(picture)
        audio.save()

    @staticmethod
    def _tag_m4a(path: str, song, cover: bytes, lyric: str = '') -> None:
        from mutagen.mp4 import MP4, MP4Cover
        audio = MP4(path)
        audio['\xa9nam'] = song.name
        audio['\xa9ART'] = song.artists
        audio['\xa9alb'] = song.album
        if lyric:
            audio['\xa9lyr'] = lyric
        if cover:
            imageformat = MP4Cover.FORMAT_PNG if _is_png(cover) else MP4Cover.FORMAT_JPEG
            audio['covr'] = [MP4Cover(cover, imageformat=imageformat)]
        audio.save()


def fmt_speed(bps: float) -> str:
    """字节/秒 -> 可读速度。"""
    if bps <= 0:
        return ''
    units = ('B/s', 'KB/s', 'MB/s', 'GB/s')
    value = float(bps)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f'{value:.1f} {unit}' if unit != 'B/s' else f'{value:.0f} {unit}'
        value /= 1024
    return ''


class ProgressHub:
    """聚合多个下载线程的进度：整体完成度 + 滑动窗口瞬时速度。

    每首歌占一格（完成度 0~1），整体进度 = 各格之和 / 总数；
    速度按最近 1~3 秒内累计字节数差值计算，UI 上报节流到约 5 次/秒。
    """

    WINDOW = 3.0      # 速度滑动窗口（秒）
    EMIT_INTERVAL = 0.2

    def __init__(self, total: int, emit):
        self._lock = threading.Lock()
        self._total = max(total, 1)
        self._emit = emit
        self._slots = {}                    # 序号 -> 完成度
        self._bytes = {}                    # 序号 -> 累计字节
        self._sum_bytes = 0                 # sum(self._bytes.values())，增量维护，避免每次遍历
        self._sum_slots = 0.0               # sum(self._slots.values())，同上
        self._samples = deque()             # (时间戳, 累计字节)
        self._last_emit = 0.0

    def update(self, index: int, ratio: float, done: int) -> None:
        now = time.time()
        with self._lock:
            prev_bytes = self._bytes.get(index, 0)
            if done > prev_bytes:
                self._bytes[index] = done
                self._sum_bytes += done - prev_bytes
            prev_ratio = self._slots.get(index, 0.0)
            if ratio > prev_ratio:
                self._slots[index] = ratio
                self._sum_slots += ratio - prev_ratio
            self._samples.append((now, self._sum_bytes))
            while self._samples and now - self._samples[0][0] > self.WINDOW:
                self._samples.popleft()
            if now - self._last_emit < self.EMIT_INTERVAL:
                return
            self._last_emit = now
            overall = min(self._sum_slots / self._total, 1.0)
            speed = self._speed_locked(now)
        self._emit(overall, speed)

    def finish(self, index: int) -> None:
        """某首歌结束（成功 / 跳过 / 失败），该格记满。"""
        now = time.time()
        with self._lock:
            self._sum_slots += 1.0 - self._slots.get(index, 0.0)
            self._slots[index] = 1.0
            overall = min(self._sum_slots / self._total, 1.0)
            speed = self._speed_locked(now)
        self._emit(overall, speed)

    def final(self) -> None:
        with self._lock:
            overall = min(self._sum_slots / self._total, 1.0)
        self._emit(overall, 0.0)

    def _speed_locked(self, now: float) -> float:
        """须持锁调用：与窗口内最早样本比较得到瞬时速度。"""
        for t, b in self._samples:
            dt = now - t
            if dt >= 0.5:
                return max((self._sum_bytes - b) / dt, 0.0)
        return 0.0
