"""下载器：取地址 → 流式下载 → 进度 → 重试 → 歌词 → 封面与标签。"""
import os
import threading
import time

import requests

from models import TYPE_EXT

CHUNK = 64 * 1024
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36')


class DownloadError(Exception):
    pass


class DownloadCancelled(Exception):
    """用户主动取消下载（不是失败，不参与重试）。"""


class Control:
    """下载暂停 / 取消控制，供界面线程与下载线程共享。"""

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


class Downloader:
    def __init__(self, api, cfg, on_log=None, on_progress=None, control=None):
        self.api = api
        self.cfg = cfg
        self.on_log = on_log or (lambda message: None)
        self.on_progress = on_progress or (lambda ratio: None)
        self.control = control or Control()
        self.produced = []   # 本次已完成的文件（音频 / 歌词）
        self.pending = None  # 正在下载的临时文件，完成后清空

    def download(self, song, level=None, encode_type=None, options=None) -> str:
        level = level or self.cfg['level']
        encode_type = encode_type or self.cfg['encode_type']
        options = options or {}
        out_dir = self.cfg['download_dir']
        os.makedirs(out_dir, exist_ok=True)
        self._check_control()

        info = self.api.song_url(song, level, encode_type)
        url = info.get('url')
        if not url:
            raise DownloadError('无版权或该音质不可用')

        ext = TYPE_EXT.get((info.get('type') or '').lower(), '.mp3')
        base = song.filename(self.cfg['name_template'])
        path = os.path.join(out_dir, base + ext)

        if self.cfg.get('skip_existing') and os.path.exists(path) and os.path.getsize(path) > 0:
            self.on_log(f'  跳过（已存在）：{base}{ext}')
            return path

        retry = int(self.cfg.get('retry', 3))
        last_error = None
        for attempt in range(1, retry + 1):
            try:
                self._fetch(url, path, song)
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
        if options.get('save_lyric', self.cfg.get('save_lyric')):
            self._save_lyric(song, path)
        if options.get('embed_tag', self.cfg.get('embed_tag')):
            embed_cover = options.get('embed_cover', self.cfg.get('embed_cover'))
            self._write_tags(song, path, embed_cover)

        self.on_log(f'  已保存：{base}{ext}')
        self._check_control()   # 取消请求若在收尾阶段到达，按取消处理
        return path

    def cleanup(self) -> int:
        """删除本次下载产生的文件（含未完成的 .part），返回删除数量。"""
        removed = 0
        targets = list(self.produced) + ([self.pending] if self.pending else [])
        for path in targets:
            try:
                if path and os.path.exists(path):
                    os.remove(path)
                    removed += 1
            except OSError as exc:  # noqa: BLE001 - 删除失败不影响主流程
                self.on_log(f'  删除失败：{path}（{exc}）')
        self.produced, self.pending = [], None
        return removed

    # ---------------- 内部实现 ----------------
    def _check_control(self) -> None:
        """暂停时在此等待；取消时抛异常中断下载。"""
        self.control.wait_if_paused()
        if self.control.cancelled:
            raise DownloadCancelled('已取消下载')

    def _fetch(self, url: str, path: str, song) -> None:
        referer = 'https://y.qq.com/' if getattr(song, 'source', 'netease') == 'qq' else 'https://music.163.com/'
        headers = {'User-Agent': UA, 'Referer': referer}
        session = getattr(self.api, 'session', None) or requests
        tmp_path = path + '.part'
        self.pending = tmp_path
        # 暂停后恢复下载：优先用 Range 续传已写入的部分
        offset = os.path.getsize(tmp_path) if os.path.exists(tmp_path) else 0
        if offset:
            headers['Range'] = f'bytes={offset}-'
        with session.get(url, headers=headers, stream=True, timeout=30) as resp:
            resp.raise_for_status()
            if offset and resp.status_code != 206:   # 服务端不支持断点续传
                offset = 0
            total = (int(resp.headers.get('Content-Length') or 0) + offset)
            done = offset
            with open(tmp_path, 'ab' if offset else 'wb') as handle:
                for chunk in resp.iter_content(CHUNK):
                    self._check_control()
                    if not chunk:
                        continue
                    handle.write(chunk)
                    done += len(chunk)
                    if total:
                        self.on_progress(min(done / total, 1.0))
        self._check_control()
        os.replace(tmp_path, path)
        self.pending = None
        self.produced.append(path)
        self.on_progress(1.0)

    def _save_lyric(self, song, audio_path: str) -> None:
        try:
            data = self.api.lyric(song)
            lrc = data.get('lyric') or ''
            if not lrc.strip():
                return
            translated = data.get('trans') or ''
            content = lrc.rstrip() + ('\n\n' + translated.rstrip() if translated.strip() else '')
            lrc_path = os.path.splitext(audio_path)[0] + '.lrc'
            with open(lrc_path, 'w', encoding='utf-8') as handle:
                handle.write(content)
            self.produced.append(lrc_path)
        except Exception as exc:  # noqa: BLE001 - 歌词失败不影响主流程
            self.on_log(f'  歌词保存失败：{exc}')

    def _write_tags(self, song, path: str, embed_cover: bool = True) -> None:
        try:
            cover = b''
            if embed_cover and song.cover_url:
                cover = requests.get(song.cover_url, timeout=15).content
            ext = os.path.splitext(path)[1].lower()
            if ext == '.mp3':
                self._tag_mp3(path, song, cover)
            elif ext == '.flac':
                self._tag_flac(path, song, cover)
            elif ext == '.m4a':
                self._tag_m4a(path, song, cover)
        except Exception as exc:  # noqa: BLE001 - 标签失败不影响文件可用
            self.on_log(f'  标签写入失败：{exc}')

    @staticmethod
    def _tag_mp3(path: str, song, cover: bytes) -> None:
        from mutagen.id3 import APIC, ID3, ID3NoHeaderError, TALB, TIT2, TPE1
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
        if cover:
            tags.delall('APIC')
            tags.add(APIC(encoding=3, mime='image/jpeg', type=3, desc='Cover', data=cover))
        tags.save(path)

    @staticmethod
    def _tag_flac(path: str, song, cover: bytes) -> None:
        from mutagen.flac import FLAC, Picture
        audio = FLAC(path)
        audio['title'] = song.name
        audio['artist'] = song.artists
        audio['album'] = song.album
        if cover:
            picture = Picture()
            picture.type = 3
            picture.mime = 'image/jpeg'
            picture.data = cover
            audio.clear_pictures()
            audio.add_picture(picture)
        audio.save()

    @staticmethod
    def _tag_m4a(path: str, song, cover: bytes) -> None:
        from mutagen.mp4 import MP4, MP4Cover
        audio = MP4(path)
        audio['\xa9nam'] = song.name
        audio['\xa9ART'] = song.artists
        audio['\xa9alb'] = song.album
        if cover:
            audio['covr'] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_JPEG)]
        audio.save()