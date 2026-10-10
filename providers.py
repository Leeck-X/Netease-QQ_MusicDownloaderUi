"""音源工厂：按 source 返回并缓存 Provider 实例（网易云 / QQ 音乐）。

两个音源对外提供一致的同步接口：
    source / set_cookie() / cookie_str() / account()
    search(keyword, limit, offset) -> list[Song]
    playlist_songs(playlist_id) -> (title, list[Song])
    song_url(song, level, encode_type) -> {'url', 'type', 'size'}
    lyric(song) -> {'lyric', 'trans'}
"""
from api import NetEaseAPI
from qq_api import QQMusicAPI

SOURCES = [('netease', '网易云音乐'), ('qq', 'QQ 音乐')]
SOURCE_LABELS = dict(SOURCES)

_CACHE = {}


def get_provider(source: str, cfg: dict):
    """取得指定音源的 Provider（同一音源复用同一实例，保留登录态）。"""
    source = source if source in SOURCE_LABELS else 'netease'
    api = _CACHE.get(source)
    if api is not None:
        return api
    if source == 'qq':
        api = QQMusicAPI(cfg.get('qq_credential') or {})
    else:
        api = NetEaseAPI(cfg.get('cookie', ''))
    _CACHE[source] = api
    return api