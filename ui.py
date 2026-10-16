"""Tkinter 图形界面：登录栏 + 搜索页 + 歌单页 + 设置页 + 进度与日志。"""
import json
import os
import queue
import re
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from PIL import ImageTk

from downloader import Control, fmt_speed
from encrypt import WEApiKeys
from models import (ENCODE_LABELS, ENCODE_TYPES, LABEL_ENCODES,
                    LEVELS_BY_SOURCE, human_size)
from providers import SOURCE_LABELS, SOURCES, apply_weapi_keys, get_provider
from services import (ServiceHub, TAG_ACCOUNT, TAG_APPEND_SONGS, TAG_CLOSE_QR,
                      TAG_DETAIL_COVER, TAG_DOWNLOAD_END, TAG_ERROR,
                      TAG_LIKED_KEYS, TAG_LOG, TAG_LOGIN_OK, TAG_MY_PLAYLISTS,
                      TAG_PROGRESS, TAG_PROBE_DONE, TAG_QQ_LOGIN_OK,
                      TAG_QR_IMAGE, TAG_SEARCH_DONE, TAG_SONGS, TAG_STATUS)

# ---------------- 配置读写 ----------------
APP_DIR = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, 'frozen', False):
    # 打包为单文件 exe 后，__file__ 指向临时解包目录，
    # 配置与下载目录应跟随 exe 所在目录，否则配置无法持久化。
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
CONFIG_PATH = os.path.join(APP_DIR, 'config.json')

# 默认配置；凭证等敏感信息保存在同目录的 config.json 中，不写入源码
DEFAULT_CONFIG = {
    'source': 'netease',          # 当前音源：netease | qq
    'cookie': '',                 # 网易云 cookie
    'qq_credential': {},          # QQ 音乐登录凭证（Credential.model_dump）
    'credentials': {},            # 网易云 API 凭证（当前不参与任何请求，仅为留档）
    'weapi_keys': WEApiKeys().to_dict(),   # weapi 加密密钥（真正生效，可在设置页修改）
    'download_dir': os.path.join(APP_DIR, 'downloads'),
    'level': 'exhigh',
    'encode_type': 'mp3',
    'name_template': '{artist} - {name}',
    'folder_structure': 'none',   # 文件组织：none | artist | album | artist_album
    'save_lyric': True,
    'embed_tag': True,
    'embed_cover': True,
    'embed_lyric': True,          # 把歌词写入音频标签（除 .lrc 外）
    'cover_fallback': True,
    'skip_existing': True,
    'retry': 3,
    'workers': 3,                 # 并发下载线程数（1~8）
    'cancel_cleanup': 'ask',      # 取消下载后：ask 每次询问 | delete 总是删除 | keep 总是保留
    'open_dir_after_download': False,   # 下载成功后自动打开下载目录
    'notify_on_finish': True,     # 下载结束后弹出通知浮窗 + 提示音
    'use_anonimous': True,
}


def load_config() -> dict:
    """读取同目录 config.json，缺失的字段用默认值补齐。"""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))   # 深拷贝
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                saved = json.load(f)
            for key, value in saved.items():
                cfg[key] = value
        except (OSError, ValueError):
            pass
    return cfg


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# 列标题与列宽（表头可点击排序，勾选列除外）
HEAD_LABELS = {'pick': '选', 'idx': '序号', 'name': '歌名', 'artist': '歌手',
               'album': '专辑', 'dur': '时长', 'size': '大小≈', 'mark': '标记',
               'like': '喜欢'}
COL_WIDTHS = {'pick': 40, 'idx': 50, 'name': 250, 'artist': 160,
              'album': 190, 'dur': 60, 'size': 80, 'mark': 50, 'like': 50}

# 树控件列顺序，以及列名 -> 列索引（Treeview values 元组按此顺序取值）
COLUMNS = ('pick', 'idx', 'name', 'artist', 'album', 'dur', 'size', 'mark', 'like')
COL = {name: index for index, name in enumerate(COLUMNS)}
PICK_MARK, UNPICK_MARK = '☑', '☐'

PRELOAD_ROWS = 50    # 视口下方剩余不足该行数时，自动预加载下一页

# 取消下载后的文件处理方式（设置页可选）
CANCEL_CLEANUP_LABELS = {'ask': '每次询问', 'delete': '总是删除文件', 'keep': '总是保留文件'}
LABEL_CANCEL_CLEANUP = {label: key for key, label in CANCEL_CLEANUP_LABELS.items()}

# 文件组织方式（设置页可选）：值 -> 中文标签
FOLDER_STRUCTURE_LABELS = {
    'none': '不分文件夹',
    'artist': '按歌手分文件夹',
    'album': '按专辑分文件夹',
    'artist_album': '按歌手 / 专辑分两级',
}
LABEL_FOLDER_STRUCTURE = {label: key for key, label in FOLDER_STRUCTURE_LABELS.items()}


def song_key(song):
    """去重用的歌曲标识：QQ 用 media/songmid，网易云用 id。"""
    return (song.source, song.mid or song.id)


def song_haystack(song) -> str:
    """过滤用的检索文本（歌名 / 歌手 / 专辑），统一小写便于匹配。"""
    return f'{song.name} {song.artists} {song.album}'.lower()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('音乐下载器（网易云 / QQ 音乐）')
        self.cfg = load_config()
        self.cfg.setdefault('source', 'netease')
        self.minsize(880, 620)
        # 记住上次窗口大小与位置，避免每次启动都要重新调整
        self.geometry(self.cfg.get('window_geometry') or '1040x740')
        self.protocol('WM_DELETE_WINDOW', self._on_close)
        self.api = get_provider(self.cfg['source'], self.cfg)
        self.msg_queue = queue.Queue()
        self.tree_songs = {}          # 树控件 -> 当前显示的歌曲列表（过滤 / 排序后）
        self._all_songs = {}          # 树控件 -> 全部已载入歌曲（过滤与排序的基准，保持载入顺序）
        self._picked_keys = {}        # 树控件 -> 已勾选歌曲标识集合（不随行号重建而失效）
        self._tree_seen = {}          # 树控件 -> 已载入歌曲标识集合（去重，避免每次追加重建）
        self._filters = {}            # 树控件 -> 当前过滤关键词（小写）
        self._filter_vars = {}        # 树控件 -> 过滤输入框变量
        self._filter_jobs = {}        # 树控件 -> 待执行的过滤防抖任务 id
        self._downloading = False
        self._dl_control = None       # 当前下载的暂停/取消控制
        self._has_more = False        # 搜索结果是否还有下一页（供滚动预加载判断，免每次读控件状态）
        self.logged_in = False        # 当前音源是否已登录
        self.logged_sources = {}      # 音源 -> 登录 profile（用于下载前校验）
        # 后台服务层：线程调度与消息投递，worker 实现见 services.py
        self.hub = ServiceHub(self.msg_queue, lambda: self.cfg, lambda: self.api)
        self.pick_labels = {}         # 树控件 -> 已选计数标签
        self.stats_labels = {}        # 树控件 -> 列表统计标签（首数 · 总时长 · 总大小）
        self._sort_state = {}         # 树控件 -> (排序列, 状态)；状态 0 原序 / 1 升序 / 2 降序
        self._size_level = {}         # 树控件 -> 上次刷新「大小」列所用音质
        self._liked_snapshot = {}     # 树控件 -> 上次刷新「喜欢」列所用集合
        self.liked_keys = set()       # 「我喜欢的音乐」标识集合（当前音源）
        self._liked_loaded_source = None   # 已加载「喜欢」集合的音源，避免重复拉取
        # 消息 tag -> 处理函数（主线程分派，见 _handle）
        self._handlers = {
            TAG_LOG: self._append_log,
            TAG_STATUS: self._set_status,
            TAG_PROGRESS: self._on_progress,
            TAG_SONGS: self._on_songs,
            TAG_APPEND_SONGS: self._on_append_songs,
            TAG_SEARCH_DONE: self._on_search_done,
            TAG_MY_PLAYLISTS: self._show_my_playlists,
            TAG_LIKED_KEYS: self._apply_liked_keys,
            TAG_DOWNLOAD_END: self._finish_download,
            TAG_LOGIN_OK: self._apply_login,
            TAG_QQ_LOGIN_OK: self._on_qq_login,
            TAG_QR_IMAGE: self._on_qr_image,
            TAG_DETAIL_COVER: self._on_detail_cover,
            TAG_PROBE_DONE: self._on_probe_done,
            TAG_CLOSE_QR: self._on_close_qr,
            TAG_ACCOUNT: self._show_account,
            TAG_ERROR: self._on_error,
        }

        self._build_top()
        self._build_tabs()
        self._build_bottom()
        self._bind_shortcuts()

        self._poll_queue()
        self.after(400, lambda: self.hub.start(self.hub.check_account))

    def _on_close(self):
        """关闭前保存窗口大小 / 位置，下次启动恢复。"""
        try:
            self.cfg['window_geometry'] = self.geometry()
            save_config(self.cfg)
        except Exception:  # noqa: BLE001 - 保存失败不应阻止关闭
            pass
        self.destroy()

    def _bind_shortcuts(self):
        """常用快捷键：Ctrl+F 定位搜索框，Ctrl+1/2/3 切换标签页。"""
        self.bind('<Control-f>', lambda _event: self._focus_entry(self.search_entry))
        self.bind('<Control-F>', lambda _event: self._focus_entry(self.search_entry))
        self.bind('<Control-l>', lambda _event: self._focus_entry(self.playlist_entry))
        self.bind('<Control-L>', lambda _event: self._focus_entry(self.playlist_entry))
        for index, key in enumerate(('1', '2', '3')):
            self.bind(f'<Control-Key-{key}>',
                      lambda _event, i=index: self._select_tab(i))

    def _focus_entry(self, entry):
        self._select_tab(self.nb.index(self._tab_of(entry)))
        entry.focus_set()
        entry.select_range(0, 'end')

    def _tab_of(self, widget):
        """向上找到某个控件所属的标签页（master 为 notebook 的那层 Frame）。"""
        node = widget
        while node is not None and node.master is not self.nb:
            node = node.master
        return node

    def _select_tab(self, index):
        try:
            self.nb.select(index)
        except tk.TclError:
            pass

    # ---------------- 界面构建 ----------------
    def _build_top(self):
        bar = ttk.Frame(self, padding=(10, 8))
        bar.pack(fill='x')
        # 登录信息列可伸缩，窗口变窄时优先压缩它，左右两侧控件始终不被挤出
        bar.columnconfigure(3, weight=1)
        ttk.Label(bar, text='音源').grid(row=0, column=0, sticky='w')
        self.source_var = tk.StringVar(value=SOURCE_LABELS[self.cfg['source']])
        source_box = ttk.Combobox(bar, textvariable=self.source_var, state='readonly',
                                  values=[label for _, label in SOURCES], width=12)
        source_box.grid(row=0, column=1, sticky='w', padx=(4, 12))
        source_box.bind('<<ComboboxSelected>>', lambda _event: self._switch_source())
        self.avatar_label = ttk.Label(bar)
        self.avatar_label.grid(row=0, column=2, sticky='w', padx=(0, 8))
        self.login_label = ttk.Label(bar, text='未登录（匿名模式）', anchor='w')
        self.login_label.grid(row=0, column=3, sticky='ew')

        # 登录按钮集中放在固定宽度的容器里，永不被登录信息挤掉
        self.login_box = ttk.Frame(bar)
        self.login_box.grid(row=0, column=4, sticky='e')

        self.logout_btn = ttk.Button(self.login_box, text='退出登录', command=self.do_logout)
        self.cookie_btn = ttk.Button(self.login_box, text='Cookie 登录',
                                     command=self.open_cookie_login)
        self.qr_btn = ttk.Button(self.login_box, text='扫码登录', command=self.open_qr_login)
        self._sync_login_buttons()

    def _sync_login_buttons(self):
        """按钮文案随音源变化，并按登录状态决定显示哪些。"""
        is_qq = self.cfg.get('source') == 'qq'
        self.qr_btn.config(text='QQ 扫码登录' if is_qq else '网易云扫码登录')
        self.cookie_btn.config(text='QQ Cookie 登录' if is_qq else '网易云 Cookie 登录')
        for btn in (self.logout_btn, self.cookie_btn, self.qr_btn):
            btn.pack_forget()
        if self.logged_in:
            self.logout_btn.pack(side='left', padx=4)
        else:
            self.cookie_btn.pack(side='left', padx=4)
            self.qr_btn.pack(side='left', padx=4)

    def _switch_source(self):
        source = next(s for s, label in SOURCES if label == self.source_var.get())
        if source == self.cfg['source']:
            return
        self.cfg['source'] = source
        save_config(self.cfg)
        self.api = get_provider(source, self.cfg)
        self.logged_in = bool(self.logged_sources.get(source))
        self.liked_keys = set()   # 音源变了，之前的「喜欢」集合作废
        self._liked_loaded_source = None   # 新音源需重新拉取「喜欢」集合
        self._refresh_likes(self.search_tree)
        self._refresh_likes(self.playlist_tree)
        self._sync_login_buttons()
        self._sync_levels()
        self._append_log(f'已切换到 {SOURCE_LABELS[source]}')
        self.hub.start(self.hub.check_account)

    def _sync_levels(self):
        """切换音源后校正音质档位：当前档位在新音源下非法时回退到「极高 320k」。"""
        levels = LEVELS_BY_SOURCE.get(self.cfg.get('source', 'netease'), LEVELS_BY_SOURCE['netease'])
        if self.cfg.get('level') not in dict(levels):
            self.cfg['level'] = 'exhigh'
        # 音质决定「大小≈」估算，切换后刷新
        self._refresh_sizes(self.search_tree)
        self._refresh_sizes(self.playlist_tree)

    def _build_tabs(self):
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill='both', expand=True, padx=10)
        self._build_search_tab()
        self._build_playlist_tab()
        self._build_settings_tab()

    def _make_song_tree(self, parent):
        tree = ttk.Treeview(parent, columns=COLUMNS, show='headings', selectmode='extended')
        for col in COLUMNS:
            tree.heading(col, text=HEAD_LABELS[col])
            tree.column(col, width=COL_WIDTHS[col],
                        anchor='center' if col in ('pick', 'idx', 'dur', 'size', 'mark', 'like')
                        else 'w')
            if col != 'pick':   # 点击表头排序（勾选列除外）
                tree.heading(col, command=lambda c=col, t=tree: self._sort_tree(t, c))
        tree.bind('<Button-1>', lambda event, t=tree: self._toggle_pick(t, event))
        tree.bind('<Button-3>', lambda event, t=tree: self._show_song_menu(t, event))
        tree.bind('<Double-1>', lambda event, t=tree: self._open_detail_at(t, event))
        scroll = ttk.Scrollbar(parent, orient='vertical', command=tree.yview)
        tree.configure(yscrollcommand=lambda first, last, sb=scroll, t=tree:
                       self._on_tree_scroll(t, sb, first, last))
        tree.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')
        return tree

    def _build_search_tab(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text='搜索')
        top = ttk.Frame(tab, padding=8)
        top.pack(fill='x')
        top.columnconfigure(0, weight=1)   # 输入行可伸缩，操作按钮组固定宽度
        row = ttk.Frame(top)
        row.grid(row=0, column=0, sticky='ew')
        ttk.Label(row, text='关键词').pack(side='left')
        # 先打包固定宽度控件，输入框最后打包并 expand：窗口变窄时优先压缩输入框
        self.more_btn = ttk.Button(row, text='加载更多', command=self.load_more, state='disabled')
        self.more_btn.pack(side='right', padx=(6, 0))
        ttk.Button(row, text='搜索', command=self.do_search).pack(side='right')
        self.search_entry = ttk.Entry(row)
        self.search_entry.pack(side='left', fill='x', expand=True, padx=6)
        self.search_entry.bind('<Return>', lambda _event: self.do_search())
        wrap = ttk.Frame(tab)
        self.search_tree = self._make_song_tree(wrap)
        self._build_filter_row(tab, self.search_tree)
        wrap.pack(fill='both', expand=True, padx=8, pady=(0, 8))
        self._build_list_actions(top, self.search_tree).grid(
            row=0, column=1, sticky='e', padx=(10, 0))

    def _build_playlist_tab(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text='歌单')
        top = ttk.Frame(tab, padding=8)
        top.pack(fill='x')
        top.columnconfigure(0, weight=1)
        row = ttk.Frame(top)
        row.grid(row=0, column=0, sticky='ew')
        ttk.Label(row, text='歌单 / 单曲 链接或 ID').pack(side='left')
        ttk.Button(row, text='我喜欢', command=self.load_liked).pack(side='right', padx=(6, 0))
        ttk.Button(row, text='我的歌单',
                   command=self.open_my_playlists).pack(side='right')
        ttk.Button(row, text='加载 / 添加', command=self.do_load_playlist).pack(side='right')
        self.playlist_entry = ttk.Entry(row)
        self.playlist_entry.pack(side='left', fill='x', expand=True, padx=6)
        self.playlist_entry.bind('<Return>', lambda _event: self.do_load_playlist())
        wrap = ttk.Frame(tab)
        self.playlist_tree = self._make_song_tree(wrap)
        self._build_filter_row(tab, self.playlist_tree)
        wrap.pack(fill='both', expand=True, padx=8, pady=(0, 8))
        self._build_list_actions(top, self.playlist_tree).grid(
            row=0, column=1, sticky='e', padx=(10, 0))

    def _build_list_actions(self, parent, tree):
        """搜索页 / 歌单页共用的列表操作区，保证两处交互完全一致。
        返回固定宽度的按钮组容器，由调用方右对齐放入网格：
        视觉顺序为 已选计数 · 去重 · 全选 · 清空勾选 · 下载选中 · 下载全部。"""
        holder = ttk.Frame(parent)
        ttk.Button(holder, text='下载全部', command=self.download_all).pack(side='right', padx=4)
        ttk.Button(holder, text='下载选中', command=self.download_selected).pack(side='right', padx=4)
        ttk.Button(holder, text='清空勾选',
                   command=lambda: self._set_all_pick(tree, False)).pack(side='right', padx=4)
        ttk.Button(holder, text='全选',
                   command=lambda: self._set_all_pick(tree, True)).pack(side='right', padx=4)
        ttk.Button(holder, text='去重',
                   command=lambda: self._dedupe_tree(tree)).pack(side='right', padx=4)
        sel_label = ttk.Label(holder, text='已选 0 首')
        sel_label.pack(side='right', padx=(0, 10))
        self.pick_labels[tree] = sel_label
        return holder

    def _build_filter_row(self, tab, tree):
        """过滤行：输入即按 歌名/歌手/专辑 过滤当前列表，右侧显示统计。"""
        row = ttk.Frame(tab, padding=(8, 0, 8, 4))
        row.pack(fill='x')
        ttk.Label(row, text='过滤').pack(side='left')
        var = tk.StringVar()
        entry = ttk.Entry(row, textvariable=var)
        stats = ttk.Label(row, text='', foreground='#666')
        stats.pack(side='right')
        ttk.Button(row, text='清空', width=6,
                   command=lambda: var.set('')).pack(side='right', padx=(6, 0))
        entry.pack(side='left', fill='x', expand=True, padx=6)
        self._filters[tree] = ''
        self._filter_vars[tree] = var
        self.stats_labels[tree] = stats
        var.trace_add('write', lambda *_args, t=tree: self._schedule_filter(t))

    def _schedule_filter(self, tree):
        """过滤输入防抖：连续输入只在停顿后重建一次列表，避免大列表逐键卡顿。"""
        job = self._filter_jobs.pop(tree, None)
        if job is not None:
            self.after_cancel(job)
        self._filter_jobs[tree] = self.after(220, lambda: self._apply_filter(tree))

    def _apply_filter(self, tree):
        self._filter_jobs.pop(tree, None)
        keyword = self._filter_vars[tree].get().strip().lower()
        if self._filters.get(tree) == keyword:
            return
        self._filters[tree] = keyword
        self._render_tree(tree)

    def _build_settings_tab(self):
        tab = ttk.Frame(self.nb, padding=16)
        self.nb.add(tab, text='设置')
        row = 0
        tab.columnconfigure(1, weight=1)   # 输入区随窗口伸缩，窄窗口不溢出

        def line(label):
            nonlocal row
            ttk.Label(tab, text=label).grid(row=row, column=0, sticky='w', pady=7)
            holder = ttk.Frame(tab)
            holder.grid(row=row, column=1, sticky='ew')
            row += 1
            return holder

        holder = line('下载目录')
        self.dir_var = tk.StringVar(value=self.cfg['download_dir'])
        ttk.Entry(holder, textvariable=self.dir_var, width=30).pack(side='left', fill='x', expand=True)
        ttk.Button(holder, text='选择…', command=self._choose_dir).pack(side='left', padx=6)

        holder = line('编码格式')
        self.encode_var = tk.StringVar(
            value=LABEL_ENCODES.get(self.cfg['encode_type'], ENCODE_TYPES[0][1]))
        self.encode_box = ttk.Combobox(holder, textvariable=self.encode_var, state='readonly',
                                       values=[label for _, label in ENCODE_TYPES], width=22)
        self.encode_box.pack(side='left')
        ttk.Label(holder, text='仅「标准」档可选；无损 / Hi-Res 固定 FLAC').pack(side='left', padx=8)

        holder = line('命名模板')
        self.tpl_var = tk.StringVar(value=self.cfg['name_template'])
        ttk.Entry(holder, textvariable=self.tpl_var, width=30).pack(side='left', fill='x', expand=True)
        ttk.Label(holder, text='可用 {artist} {name} {album} {index}').pack(side='left', padx=8)

        holder = line('文件组织')
        self.folder_var = tk.StringVar(
            value=FOLDER_STRUCTURE_LABELS.get(self.cfg.get('folder_structure', 'none'),
                                              FOLDER_STRUCTURE_LABELS['none']))
        ttk.Combobox(holder, textvariable=self.folder_var, state='readonly',
                     values=list(FOLDER_STRUCTURE_LABELS.values()), width=22).pack(side='left')
        ttk.Label(holder, text='在下载目录下按歌手 / 专辑建子目录').pack(side='left', padx=8)

        holder = line('失败重试次数')
        self.retry_var = tk.IntVar(value=int(self.cfg['retry']))
        ttk.Spinbox(holder, from_=0, to=10, textvariable=self.retry_var, width=6).pack(side='left')

        holder = line('下载并发数')
        self.workers_var = tk.IntVar(value=max(1, min(8, int(self.cfg.get('workers', 3)))))
        ttk.Spinbox(holder, from_=1, to=8, textvariable=self.workers_var, width=6).pack(side='left')
        ttk.Label(holder, text='同时下载的歌曲数（1~8，过大可能触发限速）').pack(side='left', padx=8)

        holder = line('取消下载后')
        self.cancel_cleanup_var = tk.StringVar(
            value=CANCEL_CLEANUP_LABELS.get(self.cfg.get('cancel_cleanup', 'ask'),
                                            CANCEL_CLEANUP_LABELS['ask']))
        ttk.Combobox(holder, textvariable=self.cancel_cleanup_var, state='readonly',
                     values=list(CANCEL_CLEANUP_LABELS.values()), width=22).pack(side='left')
        ttk.Label(holder, text='处理本次已下载 / 未完成的文件').pack(side='left', padx=8)

        holder = line('下载完成后')
        self.open_dir_var = tk.BooleanVar(value=bool(self.cfg.get('open_dir_after_download')))
        ttk.Checkbutton(holder, text='自动打开下载目录',
                        variable=self.open_dir_var).pack(side='left')
        self.notify_var = tk.BooleanVar(value=bool(self.cfg.get('notify_on_finish')))
        ttk.Checkbutton(holder, text='弹出完成通知',
                        variable=self.notify_var).pack(side='left', padx=(12, 0))

        holder = line('高级密钥')
        ttk.Button(holder, text='加密密钥与 API 凭证…',
                   command=self._open_keys_dialog).pack(side='left')
        ttk.Label(holder, text='weapi 加密密钥即时生效；API 凭证当前不参与请求',
                  foreground='#666').pack(side='left', padx=8)

        ttk.Label(tab, text='音质、歌词、标签、封面、跳过已存在等选项在下载时于「下载选项」窗口中选择，'
                            '确认后会记住为下次默认。',
                  foreground='#666', wraplength=520).grid(row=row, column=0, columnspan=2,
                                                          sticky='w', pady=(14, 0))

        ttk.Button(tab, text='保存设置', command=self.save_settings).grid(
            row=row + 1, column=1, sticky='w', pady=12)

    def _build_bottom(self):
        bottom = ttk.Frame(self, padding=(10, 4))
        bottom.pack(fill='x')
        line = ttk.Frame(bottom)
        line.pack(fill='x')
        # 先打包右侧固定宽度控件（side='right'），进度条最后打包并 expand，
        # 这样窗口变窄时被压缩的是进度条，而不是按钮被挤出
        ttk.Button(line, text='清空日志', command=self._clear_log).pack(side='right', padx=6)
        ttk.Button(line, text='打开下载目录', command=self._open_download_dir).pack(
            side='right', padx=(0, 1))
        self.cancel_btn = ttk.Button(line, text='取消', command=self._cancel_download, state='disabled')
        self.cancel_btn.pack(side='right', padx=(0, 8))
        self.pause_btn = ttk.Button(line, text='暂停', command=self._toggle_pause, state='disabled')
        self.pause_btn.pack(side='right')
        self.progress_label = ttk.Label(line, text='0%', width=14, anchor='e')
        self.progress_label.pack(side='left', padx=(8, 10))
        self.progress = ttk.Progressbar(line, mode='determinate', maximum=100)
        self.progress.pack(side='left', fill='x', expand=True)
        self.status_label = ttk.Label(bottom, text='就绪')
        self.status_label.pack(anchor='w', pady=(4, 0))
        log_frame = ttk.LabelFrame(self, text='日志')
        log_frame.pack(fill='both', padx=10, pady=(2, 10))
        self.log_box = scrolledtext.ScrolledText(log_frame, height=6, state='disabled')
        self.log_box.pack(fill='both', expand=True)

    # ---------------- 下载控制 ----------------
    def _sync_download_buttons(self):
        state = 'normal' if self._downloading else 'disabled'
        self.pause_btn.config(state=state)
        self.cancel_btn.config(state=state)
        if not self._downloading:
            self.pause_btn.config(text='暂停')

    def _toggle_pause(self):
        if not self._downloading or self._dl_control is None:
            return
        if self._dl_control.paused:
            self._dl_control.resume()
            self.pause_btn.config(text='暂停')
            self._append_log('已继续下载')
        else:
            self._dl_control.pause()
            self.pause_btn.config(text='继续')
            self._append_log('已暂停下载（可随时继续）')

    def _cancel_download(self):
        if not self._downloading or self._dl_control is None:
            return
        if not messagebox.askyesno('取消下载', '确定取消当前下载吗？'):
            return
        self.cancel_btn.config(state='disabled')
        self._append_log('正在取消下载…')
        self._dl_control.cancel()

    def _open_download_dir(self):
        path = self.cfg.get('download_dir') or '.'
        try:
            os.makedirs(path, exist_ok=True)
            os.startfile(path)   # Windows 资源管理器中打开
        except (OSError, AttributeError) as exc:
            messagebox.showerror('错误', f'无法打开目录：{exc}')

    def _clear_log(self):
        self.log_box.config(state='normal')
        self.log_box.delete('1.0', 'end')
        self.log_box.config(state='disabled')

    # ---------------- 队列与线程 ----------------
    def _poll_queue(self):
        # 把队列里累积的消息一次性交给主线程处理，避免逐条等待
        self.hub.drain(self._handle)
        self.after(100, self._poll_queue)

    def _handle(self, kind, payload):
        """主线程消息分派：查表调用对应处理函数（见 __init__ 的 _handlers）。

        单个消息处理异常只记录日志，不向 drain 抛出：否则会中断本轮消息消费，
        并让 _poll_queue 末尾的重排 after 跑不到，导致消息轮询永久停止。
        """
        handler = self._handlers.get(kind)
        if handler is None:
            return
        try:
            handler(payload)
        except Exception as exc:  # noqa: BLE001 - 单条消息异常不应中断消息轮询
            self._append_log(f'处理「{kind}」消息出错：{exc}')

    def _set_status(self, text):
        self.status_label.config(text=text)

    def _on_progress(self, payload):
        ratio, speed = payload
        self.progress['value'] = ratio * 100
        text = f'{int(ratio * 100)}%'
        if speed:
            text += f' · {fmt_speed(speed)}'
        self.progress_label.config(text=text)

    def _on_songs(self, payload):
        tree, songs = payload
        self._fill_tree(tree, songs)

    def _on_append_songs(self, payload):
        tree, songs = payload
        self._append_tree(tree, songs)

    def _on_search_done(self, payload):
        self.hub.search_busy = False
        self._has_more = bool(payload)
        self.more_btn.config(state='normal' if payload else 'disabled')

    def _on_qr_image(self, payload):
        win, label, image = payload
        if win.winfo_exists():   # 用户可能在图片到达前已关掉扫码窗
            self._show_qr(win, label, image)

    def _on_detail_cover(self, payload):
        win, label, image = payload
        if win.winfo_exists():
            photo = ImageTk.PhotoImage(image)
            win._photo = photo   # 保持引用，避免被回收
            label.config(image=photo, text='')

    def _on_probe_done(self, payload):
        win, label, text = payload
        if win.winfo_exists():
            label.config(text=text)

    def _on_close_qr(self, payload):
        if payload.winfo_exists():   # 扫码窗可能已被用户手动关闭
            payload.destroy()

    def _on_error(self, payload):
        messagebox.showerror('错误', payload)

    def _append_log(self, text):
        self.log_box.config(state='normal')
        self.log_box.insert('end', text + '\n')
        self.log_box.see('end')
        self.log_box.config(state='disabled')

    # ---------------- 搜索 / 歌单 ----------------
    def do_search(self):
        keyword = self.search_entry.get().strip()
        if not keyword:
            return
        self._append_log(f'搜索：{keyword}')
        self.hub.search_keyword = keyword
        self.hub.search_offset = 0
        self.hub.search_busy = True
        self._has_more = False
        self.more_btn.config(state='disabled')
        self.hub.start(self.hub.search_worker, keyword, 0, False, self.search_tree)

    def load_more(self):
        if not self.hub.search_keyword or self.hub.search_busy:
            return
        self.hub.search_busy = True
        self.more_btn.config(state='disabled')
        self._append_log(f'加载更多（偏移 {self.hub.search_offset}）')
        self.hub.start(self.hub.search_worker, self.hub.search_keyword,
                       self.hub.search_offset, True, self.search_tree)

    def _on_tree_scroll(self, tree, scrollbar, first, last):
        """同步滚动条；搜索列表滑到底部（提前 PRELOAD_ROWS 行）时自动加载下一页。"""
        scrollbar.set(first, last)
        if tree is not getattr(self, 'search_tree', None):
            return
        if self._filters.get(tree):   # 过滤时显示的不是全部结果，禁用自动预加载
            return
        if self.hub.search_busy or not self.hub.search_keyword:
            return
        if not self._has_more:   # 已到结果末尾（状态由 _on_search_done 维护，免每次读控件）
            return
        total = len(self.tree_songs.get(tree) or ())   # 行数与歌曲数一致，O(1) 取得
        if total == 0:
            return
        remaining = (1.0 - float(last)) * total          # 视口下方还有多少行
        if remaining <= PRELOAD_ROWS:
            self.load_more()

    def _parse_playlist_id(self, text: str) -> str:
        """从歌单 ID 或链接里提取 ID；优先 id= 参数，QQ 链接兼容 /playlist/xxx。"""
        match = re.search(r'[?&#]id=(\d+)', text)
        if not match and self.cfg.get('source') == 'qq':
            match = re.search(r'/playlist/(\d+)', text)
        if match:
            return match.group(1)
        nums = re.findall(r'\d+', text)
        return max(nums, key=len) if nums else ''

    def _parse_song_link(self, text: str):
        """识别单曲链接，返回 (音源, ID/MID)；不是单曲链接则返回 None。

        网易云：/song?id=xxx；QQ：/songDetail/<songmid>。
        """
        match = re.search(r'song\?id=(\d+)', text)
        if match:
            return 'netease', match.group(1)
        match = re.search(r'/songDetail/([0-9A-Za-z]+)', text)
        if match:
            return 'qq', match.group(1)
        return None

    def do_load_playlist(self):
        text = self.playlist_entry.get().strip()
        # 单曲链接优先识别：解析出 ID / MID 后作为单曲追加到列表
        song_link = self._parse_song_link(text)
        if song_link:
            source, value = song_link
            if source != self.cfg.get('source', 'netease'):
                messagebox.showinfo(
                    '提示', f'该链接属于 {SOURCE_LABELS.get(source, source)}，'
                            f'请先在顶部切换到该音源后再添加')
                return
            self._append_log(f'添加单曲链接：{value}')
            self.hub.start(self.hub.song_link_worker, value, self.playlist_tree)
            return
        # 优先取链接里的 id= 参数，避免误抓到域名中的数字（如 music.163.com 的 163）
        playlist_id = self._parse_playlist_id(text)
        if not playlist_id:
            messagebox.showinfo('提示', '请输入歌单 / 单曲链接或歌单 ID')
            return
        self._append_log(f'加载歌单：{playlist_id}')
        self.hub.start(self.hub.playlist_worker, playlist_id, self.playlist_tree)

    # ---------------- 我的歌单 / 我喜欢的音乐 ----------------
    def _require_login(self) -> bool:
        if self.logged_in:
            return True
        source = self.cfg.get('source', 'netease')
        messagebox.showinfo('提示', f'请先登录{SOURCE_LABELS.get(source, source)}账号')
        return False

    def _load_liked_keys(self):
        """后台拉取「我喜欢的音乐」标识集合，用于列表「喜欢」列。

        当前音源已加载过则跳过，避免每次账号校验都重复请求。
        """
        if self.logged_in and self._liked_loaded_source != self.cfg.get('source', 'netease'):
            self.hub.start(self.hub.liked_keys_worker)

    def _apply_liked_keys(self, payload):
        ok, data = payload
        if not ok:
            self._append_log(f'获取「喜欢」状态失败：{data}')
            return
        self.liked_keys = data
        self._liked_loaded_source = self.cfg.get('source', 'netease')
        self._refresh_likes(self.search_tree)
        self._refresh_likes(self.playlist_tree)
        self._append_log(f'已同步「我喜欢的音乐」：{len(data)} 首')

    def _refresh_likes(self, tree):
        # 「喜欢」集合未变化时跳过全表重写，避免大列表无谓刷新卡顿
        snapshot = frozenset(self.liked_keys)
        if self._liked_snapshot.get(tree) == snapshot:
            return
        self._liked_snapshot[tree] = snapshot
        for iid in tree.get_children():
            song = self._song_at(tree, iid)
            if song is None:
                continue
            tree.set(iid, 'like', '♥' if song_key(song) in snapshot else '')   # 单格写入，免读改写

    def load_liked(self):
        """一键把我喜欢的音乐加载到列表。"""
        if not self._require_login():
            return
        self._append_log('正在获取我喜欢的音乐…')
        self.hub.start(self.hub.liked_worker, self.playlist_tree)

    def open_my_playlists(self):
        """打开「我的歌单」列表窗口，双击或点按钮加载到歌单页。"""
        if not self._require_login():
            return
        source = self.cfg.get('source', 'netease')
        win = tk.Toplevel(self)
        win.title(f'我的歌单（{SOURCE_LABELS.get(source, source)}）')
        win.geometry('560x460')
        win.transient(self)
        tree = ttk.Treeview(win, columns=('title', 'count', 'kind'),
                            show='headings', selectmode='browse')
        tree.heading('title', text='歌单名称')
        tree.heading('count', text='歌曲数')
        tree.heading('kind', text='类型')
        tree.column('title', width=330)
        tree.column('count', width=80, anchor='center')
        tree.column('kind', width=100, anchor='center')
        tree.pack(fill='both', expand=True, padx=10, pady=(10, 6))
        bar = ttk.Frame(win, padding=(10, 0, 10, 10))
        bar.pack(fill='x')
        status = ttk.Label(bar, text='正在获取…')
        status.pack(side='left')

        def load_selected(_event=None):
            iid = tree.focus()
            playlist_id = self._my_pl_ids.get(iid) if iid else None
            if not playlist_id:
                return
            win.destroy()
            self._append_log(f'加载我的歌单：{playlist_id}')
            self.hub.start(self.hub.playlist_worker, playlist_id, self.playlist_tree)

        ttk.Button(bar, text='加载此歌单', command=load_selected).pack(side='right')
        ttk.Button(bar, text='关闭', command=win.destroy).pack(side='right', padx=6)
        tree.bind('<Double-1>', load_selected)
        self._my_pl_win, self._my_pl_tree, self._my_pl_status = win, tree, status
        self._my_pl_ids = {}
        self.hub.start(self.hub.my_playlists_worker)

    def _show_my_playlists(self, payload):
        ok, data = payload
        win = getattr(self, '_my_pl_win', None)
        if win is None or not win.winfo_exists():
            return
        if not ok:
            self._append_log(f'获取我的歌单失败：{data}')
            self._my_pl_status.config(text=f'获取失败：{data}')
            return
        tree, created, fav = self._my_pl_tree, [], []
        for item in data:
            (created if item['kind'] == 'created' else fav).append(item)
        for rows, kind_label in ((created, '我创建的'), (fav, '我收藏的')):
            for item in rows:
                iid = tree.insert('', 'end', values=(item['title'], item['count'], kind_label))
                self._my_pl_ids[iid] = item['id']
        self._my_pl_status.config(
            text=f'共 {len(data)} 个歌单（创建 {len(created)} · 收藏 {len(fav)}）')

    def _song_at(self, tree, iid):
        """按行标识回查歌曲，保证排序/去重后依然正确（不依赖行位置）。"""
        if not iid:
            return None
        try:
            index = int(iid) - 1
        except ValueError:
            return None
        songs = self.tree_songs.get(tree, [])
        return songs[index] if 0 <= index < len(songs) else None

    def _fill_tree(self, tree, songs):
        """整表重载：按歌曲标识去重后重置勾选 / 排序 / 过滤状态并重建。"""
        seen = set()
        unique = []
        for song in songs:
            key = song_key(song)
            if key in seen:
                continue
            seen.add(key)
            unique.append(song)
        self._all_songs[tree] = unique
        self._picked_keys[tree] = set()
        self._tree_seen[tree] = seen
        self._sort_state.pop(tree, None)   # 重新载入后清除排序标记
        self._filters[tree] = ''
        var = self._filter_vars.get(tree)
        if var is not None and var.get():
            var.set('')   # 清空过滤框（随后的 _render_tree 会立刻按新数据重建）
        self._render_tree(tree)

    def _append_tree(self, tree, songs):
        """追加歌曲到「全部歌曲」；过滤 / 排序下整表重建，否则增量插入（保留滚动位置）。"""
        all_songs = self._all_songs.setdefault(tree, [])
        # 去重集合按树持久化，追加时无需重建整表集合（大列表下省 O(n)）
        seen = self._tree_seen.get(tree)
        if seen is None:
            seen = {song_key(song) for song in all_songs}
            self._tree_seen[tree] = seen
        added = []
        for song in songs:
            key = song_key(song)
            if key in seen:
                continue
            seen.add(key)
            added.append(song)
        skipped = len(songs) - len(added)
        if not added:
            if skipped:
                self._append_log(f'  已跳过 {skipped} 首重复歌曲')
            return
        all_songs.extend(added)
        column, state = self._sort_state.get(tree, (None, 0))
        if self._filters.get(tree) or state:
            self._render_tree(tree)   # 已有过滤 / 排序：整表重建以保持顺序一致
        else:
            level = self.cfg.get('level', 'exhigh')
            existing = self.tree_songs.get(tree) or []
            picked = self._picked_keys.get(tree, set())
            for offset, song in enumerate(added):
                index = len(existing) + offset + 1
                key = song_key(song)
                tree.insert('', 'end', iid=str(index), values=(
                    PICK_MARK if key in picked else UNPICK_MARK,
                    index, song.name, song.artists, song.album, song.duration_str,
                    song.size_hint(level), 'VIP' if song.fee in (1, 4) else '',
                    '♥' if key in self.liked_keys else ''))
            self.tree_songs[tree] = existing + added   # 新追加的行都未勾选，计数不变
            self._update_pick_label(tree)
            self._update_stats(tree)
        if skipped:
            self._append_log(f'  已跳过 {skipped} 首重复歌曲')

    def _render_tree(self, tree):
        """按「全部歌曲 + 排序 + 过滤」重建列表：勾选 / 喜欢状态由集合派生，不依赖行号。

        排序与过滤都只影响显示：基础顺序始终取 _all_songs（载入顺序），
        因此「还原原始载入顺序」与「清空过滤」都能可靠回到初始视图。
        """
        all_songs = self._all_songs.get(tree, [])
        level = self.cfg.get('level', 'exhigh')
        column, state = self._sort_state.get(tree, (None, 0))
        if not state:
            ordered = list(all_songs)
        elif column == 'idx':   # 序号即 all_songs 中的载入位置
            position = {song_key(song): index for index, song in enumerate(all_songs)}
            ordered = sorted(all_songs, key=lambda s: position.get(song_key(s), 0),
                             reverse=(state == 2))
        else:
            ordered = sorted(all_songs, key=lambda s: self._sort_key(s, column, level),
                             reverse=(state == 2))
        keyword = self._filters.get(tree, '')
        visible = ordered if not keyword else [s for s in ordered if keyword in song_haystack(s)]
        picked = self._picked_keys.setdefault(tree, set())
        tree.delete(*tree.get_children())
        for index, song in enumerate(visible, 1):
            key = song_key(song)
            tree.insert('', 'end', iid=str(index), values=(
                PICK_MARK if key in picked else UNPICK_MARK,
                index, song.name, song.artists, song.album, song.duration_str,
                song.size_hint(level), 'VIP' if song.fee in (1, 4) else '',
                '♥' if key in self.liked_keys else ''))
        self.tree_songs[tree] = visible
        self._size_level[tree] = level   # 大小列已按当前音质写好
        self._update_pick_label(tree)
        self._update_stats(tree)
        self._update_headers(tree)

    def _update_stats(self, tree):
        """刷新统计标签：当前显示首数 · 总时长 · 总大小≈。"""
        label = self.stats_labels.get(tree)
        if label is None:
            return
        level = self.cfg.get('level', 'exhigh')
        songs = self.tree_songs.get(tree) or []
        seconds = sum((song.duration or 0) for song in songs) // 1000
        size = sum(song.size_value(level) for song in songs)
        hours, minutes = divmod(seconds // 60, 60)
        duration = (f'{hours}:{minutes:02d}:{seconds % 60:02d}' if hours
                    else f'{minutes}:{seconds % 60:02d}')
        label.config(text=f'{len(songs)} 首 · 总时长 {duration} · 总大小≈ {human_size(size)}')

    def _dedupe_tree(self, tree):
        """按歌曲标识去重并重建列表。"""
        songs = self._all_songs.get(tree, [])
        if not songs:
            return
        seen = set()
        unique = []
        for song in songs:
            key = song_key(song)
            if key in seen:
                continue
            seen.add(key)
            unique.append(song)
        if len(unique) == len(songs):
            self._append_log('未发现重复歌曲')
            return
        self._all_songs[tree] = unique
        self._tree_seen[tree] = seen
        self._render_tree(tree)
        self._append_log(f'去重完成：{len(songs)} → {len(unique)} 首')

    # ---------------- 排序 ----------------
    def _sort_key(self, song, column, level):
        if column == 'name':
            return song.name.lower()
        if column == 'artist':
            return song.artists.lower()
        if column == 'album':
            return song.album.lower()
        if column == 'dur':
            return song.duration
        if column == 'size':
            return song.size_value(level)
        if column == 'mark':
            return song.fee
        if column == 'like':
            return 1 if song_key(song) in self.liked_keys else 0
        return 0

    def _sort_tree(self, tree, column):
        """点击表头排序：升序 → 降序 → 还原原始载入顺序，三态循环。"""
        if not self._all_songs.get(tree):
            return
        prev_column, prev_state = self._sort_state.get(tree, (None, 0))
        if prev_column != column:
            state = 1                 # 首次点击该列 → 升序
        elif prev_state == 1:
            state = 2                 # 升序 → 降序
        else:
            state = 0                 # 降序 → 还原原始载入顺序
        self._sort_state[tree] = (column if state else None, state)
        self._render_tree(tree)

    def _update_headers(self, tree):
        column, state = self._sort_state.get(tree, (None, 0))
        for col, label in HEAD_LABELS.items():
            if col == 'pick':
                continue
            if col == column and state == 1:
                text = label + ' ▲'
            elif col == column and state == 2:
                text = label + ' ▼'
            else:
                text = label
            tree.heading(col, text=text)

    def _toggle_pick(self, tree, event=None):
        """点击「选」列切换勾选状态。"""
        if event is None or tree.identify_region(event.x, event.y) != 'cell':
            return
        if tree.identify_column(event.x) != f'#{COL["pick"] + 1}':   # 勾选列
            return
        iid = tree.identify_row(event.y)
        if not iid:
            return
        mark = UNPICK_MARK if tree.set(iid, 'pick') == PICK_MARK else PICK_MARK
        self._set_pick(tree, iid, mark)

    def _set_pick(self, tree, iid, mark):
        """切换单行勾选，并按歌曲标识维护勾选集合（重建行号也不会丢失）。"""
        song = self._song_at(tree, iid)
        if song is None:
            return
        tree.set(iid, 'pick', mark)
        picked = self._picked_keys.setdefault(tree, set())
        if mark == PICK_MARK:
            picked.add(song_key(song))
        else:
            picked.discard(song_key(song))
        self._update_pick_label(tree)

    def _set_all_pick(self, tree, picked):
        """全选 / 清空勾选：以「当前显示的行」为范围。"""
        mark = PICK_MARK if picked else UNPICK_MARK
        for iid in tree.get_children():
            tree.set(iid, 'pick', mark)
        if picked:
            self._picked_keys[tree] = {song_key(song)
                                       for song in self.tree_songs.get(tree, [])}
        else:
            self._picked_keys[tree] = set()
        self._update_pick_label(tree)

    def _picked_songs(self, tree):
        """返回勾选的歌曲（按载入顺序）；没勾选时回退到高亮选中的行。

        勾选集合按歌曲标识维护，过滤 / 排序后隐藏的歌曲仍会被计入与下载。
        """
        picked = self._picked_keys.get(tree, set())
        result = [song for song in self._all_songs.get(tree, [])
                  if song_key(song) in picked]
        if result:
            return result
        return [song for iid in tree.selection()
                if (song := self._song_at(tree, iid)) is not None]

    def _update_pick_label(self, tree):
        label = self.pick_labels.get(tree)
        if label is None:
            return
        label.config(text=f'已选 {len(self._picked_keys.get(tree, ()))} 首')

    def _active_tree(self):
        index = self.nb.index(self.nb.select())
        if index == 0:
            return self.search_tree
        if index == 1:
            return self.playlist_tree
        return None

    # ---------------- 右键菜单 / 详情 ----------------
    def _toggle_one(self, tree, iid):
        mark = UNPICK_MARK if tree.set(iid, 'pick') == PICK_MARK else PICK_MARK
        self._set_pick(tree, iid, mark)

    def _copy_text(self, text):
        self.clipboard_clear()
        self.clipboard_append(text)
        self._append_log(f'已复制：{text}')

    def _open_detail_at(self, tree, event):
        """双击行（非勾选列）打开详情。"""
        if tree.identify_column(event.x) == f'#{COL["pick"] + 1}':
            return
        iid = tree.identify_row(event.y)
        song = self._song_at(tree, iid) if iid else None
        if song is not None:
            self.open_detail(song)

    def _show_song_menu(self, tree, event):
        iid = tree.identify_row(event.y)
        if not iid:
            return
        tree.selection_set(iid)
        song = self._song_at(tree, iid)
        if song is None:
            return
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label='下载此曲', command=lambda: self._start_download([song]))
        menu.add_command(label='查看详情', command=lambda: self.open_detail(song))
        checked = tree.set(iid, 'pick') == PICK_MARK
        menu.add_command(label='取消勾选' if checked else '勾选此曲',
                         command=lambda: self._toggle_one(tree, iid))
        menu.add_separator()
        menu.add_command(label='复制「歌手 - 歌名」',
                         command=lambda: self._copy_text(f'{song.artists} - {song.name}'))
        menu.tk_popup(event.x_root, event.y_root)

    def _song_info_text(self, song) -> str:
        lines = [
            f'歌名：{song.name}',
            f'歌手：{song.artists}',
            f'专辑：{song.album or "-"}',
            f'时长：{song.duration_str}',
            f'音源：{SOURCE_LABELS.get(song.source, song.source)}',
            f'歌曲 ID：{song.id}',
        ]
        if song.mid:
            lines.append(f'songmid：{song.mid}')
        lines.append(f'收费：{"是" if song.fee else "否"}')
        sizes = song.sizes or {}
        size_text = '  '.join(f'{key}:{human_size(value)}'
                              for key, value in sizes.items() if value)
        lines.append(f'文件大小：{size_text or "-"}')
        return '\n'.join(lines)

    def open_detail(self, song):
        """详情窗：封面 + 标签信息 + 可用音质探测。"""
        win = tk.Toplevel(self)
        win.title(f'详情 - {song.name}')
        win.geometry('640x470')
        win.transient(self)

        left = ttk.Frame(win, padding=10)
        left.pack(side='left', fill='y')
        cover_label = ttk.Label(left, text='封面加载中…')
        cover_label.pack()

        right = ttk.Frame(win, padding=10)
        right.pack(side='left', fill='both', expand=True)
        ttk.Label(right, text=self._song_info_text(song), justify='left').pack(anchor='w')
        probe_label = ttk.Label(right, text='', justify='left')
        probe_label.pack(anchor='w', pady=(12, 0))
        ttk.Button(right, text='检测可用音质',
                   command=lambda: self.hub.start(self.hub.probe_qualities, song,
                                                  probe_label, win)).pack(
            anchor='w', pady=10)
        ttk.Button(right, text='下载此曲',
                   command=lambda: self._start_download([song])).pack(anchor='w')
        self.hub.start(self.hub.load_cover, song, cover_label, win)

    # ---------------- 下载 ----------------
    def download_selected(self):
        tree = self._active_tree()
        if tree is None:
            return
        self._start_download(self._picked_songs(tree))

    def download_all(self):
        tree = self._active_tree()
        if tree is None:
            return
        self._start_download(list(self.tree_songs.get(tree, [])))

    def _start_download(self, songs):
        if self._downloading:
            messagebox.showinfo('提示', '正在下载中，请稍候')
            return
        if not songs:
            messagebox.showinfo('提示', '没有可下载的歌曲')
            return
        # QQ 音乐下载强制要求登录，提前拦截，避免整批失败
        if songs[0].source == 'qq' and not self.logged_sources.get('qq'):
            messagebox.showwarning('需要登录', 'QQ 音乐下载必须登录，请先点「QQ 扫码登录」后再试。')
            return
        self._open_download_options(songs)

    def _open_download_options(self, songs):
        """下载选项窗：音质 + 歌词/标签/封面/跳过已存在。确认后记住为下次默认。"""
        win = tk.Toplevel(self)
        win.title('下载选项')
        win.geometry('400x390')
        win.transient(self)
        win.grab_set()

        levels = LEVELS_BY_SOURCE.get(songs[0].source, LEVELS_BY_SOURCE['netease'])
        label_levels = {label: level for level, label in levels}
        valid = dict(levels)
        current = self.cfg.get('level', 'exhigh')
        if current not in valid:
            current = 'exhigh'

        holder = ttk.Frame(win, padding=12)
        holder.pack(fill='x')
        ttk.Label(holder, text='音质').pack(side='left')
        level_var = tk.StringVar(value=valid.get(current, levels[0][1]))
        level_box = ttk.Combobox(holder, textvariable=level_var, state='readonly',
                                 values=[label for _, label in levels], width=18)
        level_box.pack(side='left', padx=8)

        lyric_var = tk.BooleanVar(value=self.cfg.get('save_lyric', True))
        tag_var = tk.BooleanVar(value=self.cfg.get('embed_tag', True))
        cover_var = tk.BooleanVar(value=self.cfg.get('embed_cover', True))
        embed_lyric_var = tk.BooleanVar(value=self.cfg.get('embed_lyric', True))
        skip_var = tk.BooleanVar(value=self.cfg.get('skip_existing', True))
        opts = ttk.Frame(win, padding=12)
        opts.pack(fill='x')
        ttk.Checkbutton(opts, text='下载歌词 (.lrc)', variable=lyric_var).pack(anchor='w')
        ttk.Checkbutton(opts, text='嵌入标签（歌名 / 歌手 / 专辑）', variable=tag_var).pack(anchor='w')
        ttk.Checkbutton(opts, text='嵌入封面', variable=cover_var).pack(anchor='w')
        ttk.Checkbutton(opts, text='嵌入歌词到标签', variable=embed_lyric_var).pack(anchor='w')
        ttk.Checkbutton(opts, text='跳过已存在的文件', variable=skip_var).pack(anchor='w')
        ttk.Label(win, text=f'共 {len(songs)} 首（切换音质会同步刷新列表「大小≈」）',
                  padding=(12, 0)).pack(anchor='w')

        # 切换音质时实时预览列表里的大小估算，无需先下载
        def preview(_event=None):
            self._refresh_sizes(self.search_tree, label_levels.get(level_var.get(), current))
            self._refresh_sizes(self.playlist_tree, label_levels.get(level_var.get(), current))

        level_box.bind('<<ComboboxSelected>>', preview)

        def restore_sizes():
            # 预览只是临时展示；关闭窗口时按当前配置音质回填，避免与 cfg 不一致
            for tree in (self.search_tree, self.playlist_tree):
                self._size_level.pop(tree, None)
                self._refresh_sizes(tree)

        def on_close():
            restore_sizes()
            win.destroy()

        win.protocol('WM_DELETE_WINDOW', on_close)   # 取消 / 直接关闭时回滚预览

        def confirm():
            level = label_levels.get(level_var.get(), current)
            options = {
                'save_lyric': bool(lyric_var.get()),
                'embed_tag': bool(tag_var.get()),
                'embed_cover': bool(cover_var.get()),
                'embed_lyric': bool(embed_lyric_var.get()),
            }
            # 记住本次选择，作为下次默认（避免设置页再维护一份重复选项）
            self.cfg['level'] = level
            self.cfg['save_lyric'] = options['save_lyric']
            self.cfg['embed_tag'] = options['embed_tag']
            self.cfg['embed_cover'] = options['embed_cover']
            self.cfg['embed_lyric'] = options['embed_lyric']
            self.cfg['skip_existing'] = bool(skip_var.get())
            save_config(self.cfg)
            self._refresh_sizes(self.search_tree)
            self._refresh_sizes(self.playlist_tree)
            win.destroy()
            self._dl_control = Control()
            self._downloading = True
            self._sync_download_buttons()
            self.hub.start(self.hub.download_worker, songs, level, options, self._dl_control)

        ttk.Button(win, text='开始下载', command=confirm).pack(pady=14)

    def _finish_download(self, payload):
        self._downloading = False
        self._dl_control = None
        self._sync_download_buttons()
        self.progress['value'] = 0
        self.progress_label.config(text='0%')
        self.status_label.config(text='就绪')

        success, total = payload['success'], payload['total']
        if payload['cancelled']:
            summary = f'下载已取消：成功 {success} / {total}'
        else:
            summary = f'下载结束：成功 {success} / {total}'
        if payload['failed']:
            summary += f'，失败 {len(payload["failed"])}'
        self._append_log(summary)
        for song, reason in payload['failed']:
            self._append_log(f'  失败清单：{song.name} - {song.artists}（{reason}）')
        if payload['cancelled']:
            self._cleanup_after_cancel(payload['downloader'])
        elif success and self.cfg.get('open_dir_after_download'):
            self._open_download_dir()   # 设置项开启时，下载成功后自动打开下载目录
        if not payload['cancelled'] and self.cfg.get('notify_on_finish'):
            self._notify('下载完成', summary)

    def _notify(self, title, message):
        """下载完成的轻量提醒：提示音 + 右下角浮窗 + 任务栏闪烁（全部尽力而为）。"""
        try:
            import winsound
            winsound.MessageBeep()
        except Exception:  # noqa: BLE001 - 无声环境忽略
            pass
        try:
            self._flash_taskbar()
        except Exception:  # noqa: BLE001
            pass
        try:
            win = tk.Toplevel(self)
            win.title(title)
            win.overrideredirect(True)
            win.attributes('-topmost', True)
            frame = ttk.Frame(win, padding=14, relief='solid', borderwidth=1)
            frame.pack()
            ttk.Label(frame, text=title, font=('', 10, 'bold')).pack(anchor='w')
            ttk.Label(frame, text=message, wraplength=280, justify='left').pack(anchor='w', pady=(4, 0))
            win.update_idletasks()
            width, height = win.winfo_width(), win.winfo_height()
            x = win.winfo_screenwidth() - width - 24
            y = win.winfo_screenheight() - height - 64
            win.geometry(f'+{x}+{y}')
            win.after(6000, win.destroy)
        except Exception:  # noqa: BLE001 - 浮窗失败不影响主流程
            pass

    def _flash_taskbar(self):
        """通过 ctypes 让窗口在任务栏闪烁（Windows 专用，失败静默）。"""
        import ctypes
        hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
        ctypes.windll.user32.FlashWindow(hwnd, True)

    def _cleanup_after_cancel(self, downloader):
        """取消后按设置处理本次产生的文件（未完成的 .part 与已下载的音频/歌词）。"""
        produced = downloader.produced
        pending = downloader.pending   # 多线程并发时可能有多个 .part
        if not produced and not pending:
            return
        setting = self.cfg.get('cancel_cleanup', 'ask')
        if setting == 'ask':
            count = len(produced) + len(pending)
            if not messagebox.askyesno(
                    '取消下载', f'本次共产生 {count} 个文件（含未完成）。\n是否删除？'):
                self._append_log('已保留取消下载产生的文件')
                return
        elif setting == 'keep':
            self._append_log('按设置保留取消下载产生的文件')
            return
        removed = downloader.cleanup()
        self._append_log(f'已删除取消下载产生的 {removed} 个文件')

    # ---------------- 登录 ----------------
    def _show_account(self, payload):
        profile, image = payload if isinstance(payload, tuple) else (payload, None)
        source = self.cfg.get('source', 'netease')
        self.logged_sources[source] = profile
        self.logged_in = bool(profile)
        self._sync_login_buttons()
        if profile:
            self._load_liked_keys()   # 登录后同步「喜欢」列
            vip = profile.get('vipType') or 0
            if source == 'qq':
                vip_text = ' · VIP' if vip else ''
            else:
                vip_text = {10: ' · 音乐包', 11: ' · 黑胶VIP'}.get(vip, '')
            self.login_label.config(
                text=f"已登录：{profile.get('nickname', '')}"
                     f"（ID {profile.get('userId', '')}）{vip_text}")
        else:
            hint = '未登录（QQ 下载需登录）' if source == 'qq' else '未登录（匿名模式）'
            self.login_label.config(text=hint)
        if image is not None:
            photo = ImageTk.PhotoImage(image)
            self.avatar_label.config(image=photo)
            self.avatar_label.image = photo   # 保持引用，避免被回收
        else:
            self.avatar_label.config(image='')
            self.avatar_label.image = None

    def _apply_login(self, cookie):
        self.cfg['cookie'] = cookie
        save_config(self.cfg)
        self.api.set_cookie(cookie)
        self._liked_loaded_source = None   # 重新登录后需刷新「喜欢」集合
        self._append_log('网易云登录成功，Cookie 已写入 config.json')
        self.hub.start(self.hub.check_account)

    def _on_qq_login(self, _payload=None):
        self.cfg['qq_credential'] = self.api.credential_dict()
        save_config(self.cfg)
        self._liked_loaded_source = None   # 重新登录后需刷新「喜欢」集合
        self._append_log('QQ 音乐登录成功，凭证已写入 config.json')
        self.hub.start(self.hub.check_account)

    def do_logout(self):
        source = self.cfg.get('source', 'netease')
        if source == 'qq':
            self.cfg['qq_credential'] = {}
            self.api.set_credential(None)
        else:
            self.cfg['cookie'] = ''
            self.api.set_cookie('')
        save_config(self.cfg)
        self.logged_sources[source] = None
        self.logged_in = False
        self.liked_keys = set()
        self._liked_loaded_source = None
        self._refresh_likes(self.search_tree)
        self._refresh_likes(self.playlist_tree)
        self._sync_login_buttons()
        self.login_label.config(text='未登录（匿名模式）')
        self.avatar_label.config(image='')
        self.avatar_label.image = None
        self._append_log('已退出登录')

    # ---------------- Cookie 登录 ----------------
    def open_cookie_login(self):
        if self.cfg.get('source') == 'qq':
            self._open_qq_cookie_login()
        else:
            self._open_netease_cookie_login()

    def _open_netease_cookie_login(self):
        win = tk.Toplevel(self)
        win.title('网易云 Cookie 登录')
        win.geometry('600x280')
        win.transient(self)
        ttk.Label(win, text='粘贴网易云 Cookie（至少包含 MUSIC_U）：').pack(anchor='w', padx=12, pady=(12, 4))
        text = tk.Text(win, height=7)
        text.pack(fill='both', expand=True, padx=12)
        text.insert('1.0', self.cfg.get('cookie', ''))

        def apply():
            cookie = text.get('1.0', 'end').strip()
            if not cookie:
                messagebox.showinfo('提示', 'Cookie 不能为空', parent=win)
                return
            self._apply_login(cookie)
            win.destroy()

        ttk.Button(win, text='登录', command=apply).pack(pady=10)

    def _open_qq_cookie_login(self):
        win = tk.Toplevel(self)
        win.title('QQ 音乐 Cookie 登录')
        win.geometry('620x300')
        win.transient(self)
        ttk.Label(win, text='粘贴 QQ 音乐 Cookie（至少包含 uin、qm_keyst 或 qqmusic_key）：'
                  ).pack(anchor='w', padx=12, pady=(12, 4))
        text = tk.Text(win, height=8)
        text.pack(fill='both', expand=True, padx=12)

        def apply():
            cookie = text.get('1.0', 'end').strip()
            if not cookie:
                messagebox.showinfo('提示', 'Cookie 不能为空', parent=win)
                return
            win.destroy()
            self.hub.start(self.hub.qq_cookie_apply, cookie)

        ttk.Button(win, text='登录', command=apply).pack(pady=10)

    # ---------------- 扫码登录 ----------------
    def open_qr_login(self):
        if self.cfg.get('source') == 'qq':
            self._open_qq_qr_login()
        else:
            self._open_netease_qr_login()

    def _open_netease_qr_login(self):
        win = tk.Toplevel(self)
        win.title('网易云扫码登录')
        win.geometry('320x400')
        win.transient(self)
        label = ttk.Label(win, text='正在获取二维码…')
        label.pack(expand=True)
        self.hub.qr_stop = False
        win.protocol('WM_DELETE_WINDOW', lambda: self._stop_qr(win))
        self.hub.start(self.hub.qr_worker, win, label)

    def _stop_qr(self, win):
        self.hub.qr_stop = True
        win.destroy()

    def _open_qq_qr_login(self):
        win = tk.Toplevel(self)
        win.title('QQ 音乐扫码登录')
        win.geometry('340x470')
        win.transient(self)
        type_labels = {'QQ': 'qq', '微信': 'wx', '手机': 'mobile'}
        top = ttk.Frame(win, padding=8)
        top.pack(fill='x')
        ttk.Label(top, text='登录方式').pack(side='left')
        type_var = tk.StringVar(value='QQ')
        box = ttk.Combobox(top, textvariable=type_var, state='readonly',
                           values=list(type_labels), width=8)
        box.pack(side='left', padx=6)
        hint = ttk.Label(win, text='', foreground='#666')
        hint.pack()
        label = ttk.Label(win, text='正在获取二维码…')
        label.pack(expand=True)

        def stop():
            session = getattr(win, '_session', None)
            if session:
                session['stop'] = True
            win.destroy()

        def refresh(_event=None):
            old = getattr(win, '_session', None)
            if old:
                old['stop'] = True
            session = {'stop': False}
            win._session = session
            label.config(text='正在获取二维码…', image='')
            hint.config(text='')
            self.hub.start(self.hub.qq_qr_worker, win, label, hint, session,
                           type_labels[type_var.get()])

        box.bind('<<ComboboxSelected>>', refresh)
        win.protocol('WM_DELETE_WINDOW', stop)
        refresh()

    def _show_qr(self, win, label, image):
        photo = ImageTk.PhotoImage(image)
        win._photo = photo  # 保持引用，避免被回收
        label.config(image=photo, text='')

    # ---------------- 设置 ----------------
    def _choose_dir(self):
        chosen = filedialog.askdirectory(initialdir=self.dir_var.get() or os.getcwd())
        if chosen:
            self.dir_var.set(chosen)

    def save_settings(self):
        self.cfg['download_dir'] = self.dir_var.get().strip() or self.cfg['download_dir']
        self.cfg['encode_type'] = ENCODE_LABELS.get(self.encode_var.get(), 'mp3')
        self.cfg['name_template'] = self.tpl_var.get().strip() or '{artist} - {name}'
        self.cfg['retry'] = int(self.retry_var.get())
        self.cfg['workers'] = max(1, min(8, int(self.workers_var.get())))
        self.cfg['folder_structure'] = LABEL_FOLDER_STRUCTURE.get(
            self.folder_var.get(), 'none')
        self.cfg['cancel_cleanup'] = LABEL_CANCEL_CLEANUP.get(
            self.cancel_cleanup_var.get(), 'ask')
        self.cfg['open_dir_after_download'] = bool(self.open_dir_var.get())
        self.cfg['notify_on_finish'] = bool(self.notify_var.get())
        save_config(self.cfg)
        self._append_log('设置已保存')

    # ---------------- 高级密钥 ----------------
    def _open_keys_dialog(self):
        """密钥对话框：weapi 加密密钥（即时生效）+ API 凭证（当前未启用）。"""
        win = tk.Toplevel(self)
        win.title('加密密钥与 API 凭证')
        win.geometry('660x520')
        win.transient(self)
        win.grab_set()
        nb = ttk.Notebook(win)
        nb.pack(fill='both', expand=True, padx=10, pady=10)
        self._build_weapi_keys_page(nb, win)
        self._build_credentials_page(nb, win)
        ttk.Button(win, text='关闭', command=win.destroy).pack(pady=(0, 12))

    def _keys_field(self, parent, row, label, value, height=None):
        """一行密钥输入；height 给定时改用多行 Text（长 hex / PEM 需换行显示）。"""
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky='nw', pady=6, padx=(0, 8))
        holder = ttk.Frame(parent)
        holder.grid(row=row, column=1, sticky='ew', pady=6)
        if height is None:
            var = tk.StringVar(value=value)
            ttk.Entry(holder, textvariable=var).pack(side='left', fill='x', expand=True)
            return var
        text = tk.Text(holder, height=height, wrap='char')
        bar = ttk.Scrollbar(holder, orient='vertical', command=text.yview)
        text.configure(yscrollcommand=bar.set)
        bar.pack(side='right', fill='y')
        text.pack(side='left', fill='both', expand=True)
        text.insert('1.0', value)
        return text

    def _build_weapi_keys_page(self, nb, win):
        page = ttk.Frame(nb, padding=12)
        nb.add(page, text='weapi 加密密钥（生效）')
        page.columnconfigure(1, weight=1)
        keys = WEApiKeys.from_config(self.cfg.get('weapi_keys'))
        self._weapi_vars = {
            'key1': self._keys_field(page, 0, 'key1', keys.key1),
            'iv': self._keys_field(page, 1, 'iv', keys.iv),
            'pub_key': self._keys_field(page, 2, 'pub_key', keys.pub_key),
            'modulus': self._keys_field(page, 3, 'modulus', keys.modulus, height=6),
        }
        ttk.Label(page, text='仅影响 /weapi/* 加密接口（歌曲详情、歌单、取址、歌词）；'
                             '填错会导致这些请求全部失败。',
                  foreground='#666', wraplength=520, justify='left').grid(
            row=4, column=0, columnspan=2, sticky='w', pady=(14, 0))
        bar = ttk.Frame(page)
        bar.grid(row=5, column=1, sticky='w', pady=12)
        ttk.Button(bar, text='保存并生效',
                   command=lambda: self._save_weapi_keys(win)).pack(side='left')
        ttk.Button(bar, text='校验',
                   command=lambda: self._check_weapi_keys(win)).pack(side='left', padx=6)
        ttk.Button(bar, text='恢复默认值', command=self._reset_weapi_keys).pack(side='left')

    def _collect_fields(self, widgets) -> dict:
        """读取一组输入控件（Entry 取 StringVar，Text 取内容）。"""
        data = {}
        for name, widget in widgets.items():
            data[name] = (widget.get('1.0', 'end').strip() if isinstance(widget, tk.Text)
                          else widget.get().strip())
        return data

    def _check_weapi_keys(self, win=None):
        """校验当前填写的密钥；不合法时弹窗提示并返回 None。"""
        keys = WEApiKeys.from_config(self._collect_fields(self._weapi_vars))
        error = keys.validate()
        if error:
            messagebox.showerror('密钥无效', error, parent=win or self)
            return None
        return keys

    def _save_weapi_keys(self, win=None):
        keys = self._check_weapi_keys(win)
        if keys is None:
            return
        self.cfg['weapi_keys'] = keys.to_dict()
        save_config(self.cfg)
        apply_weapi_keys(self.cfg)   # 只更新已缓存实例，保留登录态与匿名 token
        self._append_log('weapi 密钥已更新并生效')
        messagebox.showinfo('提示', 'weapi 密钥已保存并生效', parent=win or self)

    def _reset_weapi_keys(self):
        defaults = WEApiKeys()
        for name, widget in self._weapi_vars.items():
            value = getattr(defaults, name)
            if isinstance(widget, tk.Text):
                widget.delete('1.0', 'end')
                widget.insert('1.0', value)
            else:
                widget.set(value)

    def _build_credentials_page(self, nb, win):
        page = ttk.Frame(nb, padding=12)
        nb.add(page, text='API 凭证（未启用）')
        page.columnconfigure(1, weight=1)
        ttk.Label(page, text='本组凭证当前不参与任何请求，仅为留档与后续扩展保留；'
                             '填入不会改变下载行为。',
                  foreground='#c00', wraplength=540, justify='left').grid(
            row=0, column=0, columnspan=2, sticky='w', pady=(0, 10))
        cred = self.cfg.get('credentials')
        cred = cred if isinstance(cred, dict) else {}
        self._cred_vars = {
            'app_id': self._keys_field(page, 1, 'app_id', str(cred.get('app_id') or '')),
            'app_secret': self._keys_field(page, 2, 'app_secret', str(cred.get('app_secret') or '')),
            'pub_key': self._keys_field(page, 3, 'pub_key', str(cred.get('pub_key') or ''), height=7),
            'private_key': self._keys_field(page, 4, 'private_key',
                                            str(cred.get('private_key') or ''), height=7),
        }
        ttk.Button(page, text='保存', command=lambda: self._save_credentials(win)).grid(
            row=5, column=1, sticky='w', pady=12)

    def _save_credentials(self, win=None):
        cred = dict(self.cfg.get('credentials') or {})
        cred.update(self._collect_fields(self._cred_vars))
        self.cfg['credentials'] = cred
        save_config(self.cfg)
        self._append_log('API 凭证已保存（当前不参与任何请求）')
        messagebox.showinfo('提示', 'API 凭证已保存（当前不参与任何请求）', parent=win or self)

    def _refresh_sizes(self, tree, level=None):
        level = level or self.cfg.get('level', 'exhigh')
        # 音质未变化时跳过全表重算，避免每次切换音源 / 刷新都重写整张表
        if self._size_level.get(tree) == level:
            return
        self._size_level[tree] = level
        for iid in tree.get_children():
            song = self._song_at(tree, iid)
            if song is None:
                continue
            tree.set(iid, 'size', song.size_hint(level))   # 单格写入，免读改写
        self._update_stats(tree)   # 音质变了，统计里的总大小也要跟着更新