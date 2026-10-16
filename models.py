"""数据模型：音质/格式映射、命名模板、Song 数据类。"""
import re
from dataclasses import dataclass

_EM_TAG = re.compile(r'</?em>', re.I)


def strip_highlight(text: str) -> str:
    """去掉搜索接口返回的高亮标签（如 <em>周杰伦</em>）。"""
    return _EM_TAG.sub('', text or '')

# 音质档位：(level, 界面标签)——按音源区分
LEVELS_NETEASE = [
    ('standard', '标准 128k'),
    ('higher', '较高 192k'),
    ('exhigh', '极高 320k'),
    ('lossless', '无损 FLAC'),
    ('hires', 'Hi-Res 24bit'),
]
LEVELS_QQ = [
    ('standard', '标准 128k'),
    ('exhigh', 'HQ 320k'),
    ('lossless', 'SQ 无损 FLAC'),
    ('hires', 'Hi-Res 母带'),
]
LEVELS_BY_SOURCE = {'netease': LEVELS_NETEASE, 'qq': LEVELS_QQ}

# 仅「标准」档可选编码；无损/Hi-Res 由服务端固定返回 flac
ENCODE_TYPES = [('mp3', 'MP3'), ('aac', 'AAC / M4A')]
LABEL_ENCODES = dict(ENCODE_TYPES)
ENCODE_LABELS = {label: enc for enc, label in ENCODE_TYPES}

# 音质档位 -> 详情接口里的码率节点（用于估算文件大小；依次回退，
# 尽量给出一个近似值，取不到才显示 '-'）——按音源区分
SIZE_QUALITY = {  # 网易云
    'standard': ('l', 'm', 'h'),
    'higher': ('m', 'l', 'h'),
    'exhigh': ('h', 'm', 'l'),
    'lossless': ('sq', 'h', 'm', 'l'),
    'hires': ('hr', 'sq', 'h', 'm', 'l'),
}
SIZE_QUALITY_QQ = {  # QQ 音乐
    'standard': ('128mp3', '96aac', '192aac'),
    'exhigh': ('320mp3', '192aac', '128mp3'),
    'lossless': ('flac', '320mp3', '128mp3'),
    'hires': ('hires', 'flac', '320mp3', '128mp3'),
}
SIZE_QUALITY_BY_SOURCE = {'netease': SIZE_QUALITY, 'qq': SIZE_QUALITY_QQ}

# 服务端返回的 type -> 文件扩展名
TYPE_EXT = {
    'mp3': '.mp3',
    'flac': '.flac',
    'mp4': '.m4a',
    'm4a': '.m4a',
    'aac': '.m4a',
    'wav': '.wav',
}

_INVALID = re.compile(r'[\\/:*?"<>|\r\n\t]')


def sanitize(name: str) -> str:
    """去掉文件名里的非法字符。"""
    cleaned = _INVALID.sub('_', name or '').strip().strip('.')
    return cleaned or 'unknown'


def human_size(size) -> str:
    """字节数 -> 可读字符串；无效值返回 '-'。"""
    try:
        value = float(size)
    except (TypeError, ValueError):
        return '-'
    if value <= 0:
        return '-'
    units = ('B', 'KB', 'MB', 'GB')
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1
    return f'{value:.0f} {units[index]}' if index == 0 else f'{value:.1f} {units[index]}'


@dataclass
class Song:
    id: int
    name: str
    artists: str
    album: str = ''
    cover_url: str = ''
    duration: int = 0  # 毫秒
    fee: int = 0
    sizes: dict = None  # {'l':..,'m':..,'h':..,'sq':..,'hr':..} 各码率字节数
    source: str = 'netease'  # 'netease' | 'qq'
    mid: str = ''            # QQ 音乐的 songmid（网易云为空）
    media_mid: str = ''      # QQ 音乐的媒体文件 mid，用于拼接下载文件名

    @property
    def duration_str(self) -> str:
        seconds = self.duration // 1000
        return f'{seconds // 60:02d}:{seconds % 60:02d}'

    def size_value(self, level: str) -> int:
        """按当前音质档位取近似的文件字节数（用于排序），取不到返回 0。"""
        table = SIZE_QUALITY_BY_SOURCE.get(self.source, SIZE_QUALITY)
        for key in table.get(level, ()):
            size = (self.sizes or {}).get(key)
            if size:
                return int(size)
        return 0

    def size_hint(self, level: str) -> str:
        """按当前音质档位估算文件大小（详情接口的各码率 size）。"""
        return human_size(self.size_value(level))

    def filename(self, template: str) -> str:
        """按命名模板生成不含扩展名的文件名。"""
        try:
            return sanitize(template.format(artist=self.artists, name=self.name, album=self.album))
        except (KeyError, IndexError):
            return sanitize(f'{self.artists} - {self.name}')

    @classmethod
    def from_raw(cls, raw: dict) -> 'Song':
        """兼容两种返回结构：
        * 详情接口(weapi v3)：ar / al / dt
        * 明文搜索接口：artists / album / duration
        """
        artists = '/'.join(
            item.get('name', '')
            for item in (raw.get('ar') or raw.get('artists') or [])
            if item.get('name')
        )
        album = raw.get('al') or raw.get('album') or {}
        sizes = {}
        for key in ('l', 'm', 'h', 'sq', 'hr'):
            node = raw.get(key)
            if isinstance(node, dict) and node.get('size'):
                sizes[key] = node['size']
        return cls(
            id=raw.get('id', 0),
            name=raw.get('name', ''),
            artists=artists,
            album=album.get('name', ''),
            cover_url=album.get('picUrl') or album.get('blurPicUrl') or '',
            duration=raw.get('dt') or raw.get('duration') or 0,
            fee=raw.get('fee', 0) or 0,
            sizes=sizes,
            source='netease',
        )

    @classmethod
    def from_qq(cls, raw: dict) -> 'Song':
        """映射 QQ 音乐搜索/歌单返回的单曲结构。"""
        singers = raw.get('singer') or []
        artists = '/'.join(strip_highlight(item.get('name', '')) for item in singers if item.get('name'))
        album = raw.get('album') or {}
        album_mid = album.get('mid') or ''
        file_info = raw.get('file') or {}
        sizes = {}
        for key in ('128mp3', '320mp3', 'flac', 'hires', '192aac', '96aac'):
            size = file_info.get('size_' + key)
            if size:
                sizes[key] = size
        new_sizes = file_info.get('size_new') or []
        if isinstance(new_sizes, list) and new_sizes and new_sizes[0]:
            sizes.setdefault('hires', new_sizes[0])  # 臻品母带
        pay = raw.get('pay') or {}
        return cls(
            id=raw.get('id') or 0,
            name=strip_highlight(raw.get('name') or raw.get('title') or ''),
            artists=artists,
            album=strip_highlight(album.get('name', '')),
            cover_url=(f'https://y.qq.com/music/photo_new/T002R300x300M000{album_mid}.jpg'
                       if album_mid else ''),
            duration=(raw.get('interval') or 0) * 1000,
            fee=1 if (pay.get('pay_play') or pay.get('pay_down')) else 0,
            sizes=sizes,
            source='qq',
            mid=raw.get('mid') or '',
            media_mid=file_info.get('media_mid') or '',
        )