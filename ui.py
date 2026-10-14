"""Tkinter 图形界面：登录栏 + 搜索页 + 歌单页 + 设置页 + 进度与日志。"""
import io
import os
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

import qrcode
import requests
from PIL import Image, ImageTk

import config as cfgmod
from downloader import Control, DownloadCancelled, Downloader
from models import (ENCODE_LABELS, ENCODE_TYPES, LABEL_ENCODES,
                    LEVELS_BY_SOURCE, human_size)
from providers import SOURCE_LABELS, SOURCES, get_provider

# 列标题与列宽（表头可点击排序，勾选列除外）
HEAD_LABELS = {'pick': '选', 'idx': '序号', 'name': '歌名', 'artist': '歌手',
               'album': '专辑', 'dur': '时长', 'size': '大小≈', 'mark': '标记',
               'like': '喜欢'}
COL_WIDTHS = {'pick': 40, 'idx': 50, 'name': 250, 'artist': 160,
              'album': 190, 'dur': 60, 'size': 80, 'mark': 50, 'like': 50}

PAGE_SIZE = 50       # 每次搜索 / 加载更多请求的条数
PRELOAD_ROWS = 50    # 视口下方剩余不足该行数时，自动预加载下一页

# 取消下载后的文件处理方式（设置页可选）
CANCEL_CLEANUP_LABELS = {'ask': '每次询问', 'delete': '总是删除文件', 'keep': '总是保留文件'}
LABEL_CANCEL_CLEANUP = {label: key for key, label in CANCEL_CLEANUP_LABELS.items()}


def song_key(song):
    """去重用的歌曲标识：QQ 用 media/songmid，网易云用 id。"""
    return (song.source, song.mid or song.id)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('音乐下载器（网易云 / QQ 音乐）')
        self.geometry('1040x740')
        self.minsize(900, 640)

        self.cfg = cfgmod.load_config()
        self.cfg.setdefault('source', 'netease')
        self.api = get_provider(self.cfg['source'], self.cfg)
        self.msg_queue = queue.Queue()
        self.tree_songs = {}          # 树控件 -> 歌曲列表
        self._downloading = False
        self._dl_control = None       # 当前下载的暂停/取消控制
        self._qr_stop = False
        self.logged_in = False        # 当前音源是否已登录
        self.logged_sources = {}      # 音源 -> 登录 profile（用于下载前校验）
        self._search_keyword = ''     # 搜索分页：当前关键词
        self._search_offset = 0       # 搜索分页：下一页偏移
        self._search_busy = False     # 搜索分页：正在请求中（防止重复触发）
        self.pick_labels = {}         # 树控件 -> 已选计数标签
        self._sort_state = {}         # 树控件 -> (排序列, 是否倒序)
        self.liked_keys = set()       # 「我喜欢的音乐」标识集合（当前音源）

        self._build_top()
        self._build_tabs()
        self._build_bottom()

        self._poll_queue()
        self.after(400, lambda: self._bg(self._check_account))

    # ---------------- 界面构建 ----------------
    def _build_top(self):
        bar = ttk.Frame(self, padding=(10, 8))
        bar.pack(fill='x')
        ttk.Label(bar, text='音源').pack(side='left')
        self.source_var = tk.StringVar(value=SOURCE_LABELS[self.cfg['source']])
        source_box = ttk.Combobox(bar, textvariable=self.source_var, state='readonly',
                                  values=[label for _, label in SOURCES], width=12)
        source_box.pack(side='left', padx=(4, 12))
        source_box.bind('<<ComboboxSelected>>', lambda _event: self._switch_source())
        self.avatar_label = ttk.Label(bar)
        self.avatar_label.pack(side='left', padx=(0, 8))
        self.login_label = ttk.Label(bar, text='未登录（匿名模式）')
        self.login_label.pack(side='left')

        # 登录相关按钮按登录状态显隐，避免未登录时出现无效的「退出登录」
        self.logout_btn = ttk.Button(bar, text='退出登录', command=self.do_logout)
        self.cookie_btn = ttk.Button(bar, text='Cookie 登录', command=self.open_cookie_login)
        self.qr_btn = ttk.Button(bar, text='扫码登录', command=self.open_qr_login)
        self._sync_login_buttons()

    def _sync_login_buttons(self):
        """按钮文案随音源变化，并按登录状态决定显示哪些。"""
        is_qq = self.cfg.get('source') == 'qq'
        self.qr_btn.config(text='QQ 扫码登录' if is_qq else '网易云扫码登录')
        self.cookie_btn.config(text='QQ Cookie 登录' if is_qq else '网易云 Cookie 登录')
        for btn in (self.logout_btn, self.cookie_btn, self.qr_btn):
            btn.pack_forget()
        if self.logged_in:
            self.logout_btn.pack(side='right', padx=4)
        else:
            self.qr_btn.pack(side='right', padx=4)
            self.cookie_btn.pack(side='right', padx=4)

    def _switch_source(self):
        source = next(s for s, label in SOURCES if label == self.source_var.get())
        if source == self.cfg['source']:
            return
        self.cfg['source'] = source
        cfgmod.save_config(self.cfg)
        self.api = get_provider(source, self.cfg)
        self.logged_in = bool(self.logged_sources.get(source))
        self.liked_keys = set()   # 音源变了，之前的「喜欢」集合作废
        self._refresh_likes(self.search_tree)
        self._refresh_likes(self.playlist_tree)
        self._sync_login_buttons()
        self._sync_levels()
        self._append_log(f'已切换到 {SOURCE_LABELS[source]}')
        self._bg(self._check_account)

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
        columns = ('pick', 'idx', 'name', 'artist', 'album', 'dur', 'size', 'mark', 'like')
        tree = ttk.Treeview(parent, columns=columns, show='headings', selectmode='extended')
        for col in columns:
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
        ttk.Label(top, text='关键词').pack(side='left')
        self.search_entry = ttk.Entry(top, width=42)
        self.search_entry.pack(side='left', padx=6)
        self.search_entry.bind('<Return>', lambda _event: self.do_search())
        ttk.Button(top, text='搜索', command=self.do_search).pack(side='left')
        self.more_btn = ttk.Button(top, text='加载更多', command=self.load_more, state='disabled')
        self.more_btn.pack(side='left', padx=6)
        wrap = ttk.Frame(tab)
        wrap.pack(fill='both', expand=True, padx=8, pady=(0, 8))
        self.search_tree = self._make_song_tree(wrap)
        self.sel_label = self._build_list_actions(top, self.search_tree)

    def _build_playlist_tab(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text='歌单')
        top = ttk.Frame(tab, padding=8)
        top.pack(fill='x')
        ttk.Label(top, text='歌单 ID 或链接').pack(side='left')
        self.playlist_entry = ttk.Entry(top, width=42)
        self.playlist_entry.pack(side='left', padx=6)
        self.playlist_entry.bind('<Return>', lambda _event: self.do_load_playlist())
        ttk.Button(top, text='加载', command=self.do_load_playlist).pack(side='left')
        ttk.Button(top, text='我的歌单',
                   command=self.open_my_playlists).pack(side='left', padx=(8, 0))
        ttk.Button(top, text='我喜欢',
                   command=self.load_liked).pack(side='left', padx=4)
        wrap = ttk.Frame(tab)
        wrap.pack(fill='both', expand=True, padx=8, pady=(0, 8))
        self.playlist_tree = self._make_song_tree(wrap)
        self.sel_label_pl = self._build_list_actions(top, self.playlist_tree)

    def _build_list_actions(self, parent, tree):
        """搜索页 / 歌单页共用的列表操作区，保证两处交互完全一致。
        自右向左依次打包，最终视觉顺序为：已选计数 · 去重 · 全选 · 清空 · 下载。"""
        ttk.Button(parent, text='下载全部', command=self.download_all).pack(side='right', padx=4)
        ttk.Button(parent, text='下载选中', command=self.download_selected).pack(side='right', padx=4)
        ttk.Button(parent, text='清空勾选',
                   command=lambda: self._set_all_pick(tree, False)).pack(side='right', padx=4)
        ttk.Button(parent, text='全选',
                   command=lambda: self._set_all_pick(tree, True)).pack(side='right', padx=4)
        ttk.Button(parent, text='去重',
                   command=lambda: self._dedupe_tree(tree)).pack(side='right', padx=4)
        sel_label = ttk.Label(parent, text='已选 0 首')
        sel_label.pack(side='right', padx=(0, 10))
        self.pick_labels[str(tree)] = sel_label
        return sel_label

    def _build_settings_tab(self):
        tab = ttk.Frame(self.nb, padding=16)
        self.nb.add(tab, text='设置')
        row = 0

        def line(label):
            nonlocal row
            ttk.Label(tab, text=label).grid(row=row, column=0, sticky='w', pady=7)
            holder = ttk.Frame(tab)
            holder.grid(row=row, column=1, sticky='w')
            row += 1
            return holder

        holder = line('下载目录')
        self.dir_var = tk.StringVar(value=self.cfg['download_dir'])
        ttk.Entry(holder, textvariable=self.dir_var, width=54).pack(side='left')
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
        ttk.Entry(holder, textvariable=self.tpl_var, width=54).pack(side='left')
        ttk.Label(holder, text='可用 {artist} {name} {album}').pack(side='left', padx=8)

        holder = line('失败重试次数')
        self.retry_var = tk.IntVar(value=int(self.cfg['retry']))
        ttk.Spinbox(holder, from_=0, to=10, textvariable=self.retry_var, width=6).pack(side='left')

        holder = line('取消下载后')
        self.cancel_cleanup_var = tk.StringVar(
            value=CANCEL_CLEANUP_LABELS.get(self.cfg.get('cancel_cleanup', 'ask'),
                                            CANCEL_CLEANUP_LABELS['ask']))
        ttk.Combobox(holder, textvariable=self.cancel_cleanup_var, state='readonly',
                     values=list(CANCEL_CLEANUP_LABELS.values()), width=22).pack(side='left')
        ttk.Label(holder, text='处理本次已下载 / 未完成的文件').pack(side='left', padx=8)

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
        self.progress = ttk.Progressbar(line, mode='determinate', maximum=100)
        self.progress.pack(side='left', fill='x', expand=True)
        self.progress_label = ttk.Label(line, text='0%', width=5, anchor='e')
        self.progress_label.pack(side='left', padx=(8, 0))
        self.pause_btn = ttk.Button(line, text='暂停', command=self._toggle_pause, state='disabled')
        self.pause_btn.pack(side='left', padx=(10, 0))
        self.cancel_btn = ttk.Button(line, text='取消', command=self._cancel_download, state='disabled')
        self.cancel_btn.pack(side='left', padx=6)
        ttk.Button(line, text='打开下载目录', command=self._open_download_dir).pack(
            side='left', padx=(10, 0))
        ttk.Button(line, text='清空日志', command=self._clear_log).pack(side='left', padx=6)
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
    def _bg(self, func, *args):
        threading.Thread(target=func, args=args, daemon=True).start()

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.msg_queue.get_nowait()
                self._handle(kind, payload)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _handle(self, kind, payload):
        if kind == 'log':
            self._append_log(payload)
        elif kind == 'status':
            self.status_label.config(text=payload)
        elif kind == 'progress':
            self.progress['value'] = payload * 100
            self.progress_label.config(text=f'{int(payload * 100)}%')
        elif kind == 'songs':
            tree, songs = payload
            self._fill_tree(tree, songs)
        elif kind == 'append_songs':
            tree, songs = payload
            self._append_tree(tree, songs)
        elif kind == 'search_done':
            self._search_busy = False
            self.more_btn.config(state='normal' if payload else 'disabled')
        elif kind == 'my_playlists':
            self._show_my_playlists(payload)
        elif kind == 'liked_keys':
            self._apply_liked_keys(payload)
        elif kind == 'download_end':
            self._finish_download(payload)
        elif kind == 'login_ok':
            self._apply_login(payload)
        elif kind == 'qq_login_ok':
            self._on_qq_login()
        elif kind == 'qr_image':
            self._show_qr(*payload)
        elif kind == 'detail_cover':
            win, label, image = payload
            if win.winfo_exists():
                photo = ImageTk.PhotoImage(image)
                win._photo = photo   # 保持引用，避免被回收
                label.config(image=photo, text='')
        elif kind == 'probe_done':
            win, label, text = payload
            if win.winfo_exists():
                label.config(text=text)
        elif kind == 'close_qr':
            payload.destroy()
        elif kind == 'account':
            self._show_account(payload)
        elif kind == 'error':
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
        self._search_keyword = keyword
        self._search_offset = 0
        self._search_busy = True
        self.more_btn.config(state='disabled')
        self._bg(self._search_worker, keyword, 0, False)

    def load_more(self):
        if not self._search_keyword or self._search_busy:
            return
        self._search_busy = True
        self.more_btn.config(state='disabled')
        self._append_log(f'加载更多（偏移 {self._search_offset}）')
        self._bg(self._search_worker, self._search_keyword, self._search_offset, True)

    def _on_tree_scroll(self, tree, scrollbar, first, last):
        """同步滚动条；搜索列表滑到底部（提前 PRELOAD_ROWS 行）时自动加载下一页。"""
        scrollbar.set(first, last)
        if tree is not getattr(self, 'search_tree', None):
            return
        if self._search_busy or not self._search_keyword:
            return
        if str(self.more_btn['state']) != 'normal':   # 没有更多结果了
            return
        total = len(tree.get_children())
        if total == 0:
            return
        remaining = (1.0 - float(last)) * total          # 视口下方还有多少行
        if remaining <= PRELOAD_ROWS:
            self.load_more()

    def _search_worker(self, keyword, offset, append):
        try:
            songs = self.api.search(keyword, limit=PAGE_SIZE, offset=offset)
            kind = 'append_songs' if append else 'songs'
            self.msg_queue.put((kind, (self.search_tree, songs)))
            self._search_offset = offset + len(songs)
            self.msg_queue.put(('search_done', len(songs) >= PAGE_SIZE))
            if append:
                self.msg_queue.put(('log', f'追加 {len(songs)} 首（累计 {self._search_offset}）'))
            else:
                self.msg_queue.put(('log', f'共搜索到 {len(songs)} 首'))
        except Exception as exc:  # noqa: BLE001
            self.msg_queue.put(('search_done', False))   # 失败也要复位忙碌状态
            self.msg_queue.put(('log', f'搜索失败：{exc}'))

    def _parse_playlist_id(self, text: str) -> str:
        """从歌单 ID 或链接里提取 ID；优先 id= 参数，QQ 链接兼容 /playlist/xxx。"""
        match = re.search(r'[?&#]id=(\d+)', text)
        if not match and self.cfg.get('source') == 'qq':
            match = re.search(r'/playlist/(\d+)', text)
        if match:
            return match.group(1)
        nums = re.findall(r'\d+', text)
        return max(nums, key=len) if nums else ''

    def do_load_playlist(self):
        text = self.playlist_entry.get().strip()
        # 优先取链接里的 id= 参数，避免误抓到域名中的数字（如 music.163.com 的 163）
        playlist_id = self._parse_playlist_id(text)
        if not playlist_id:
            messagebox.showinfo('提示', '请输入歌单 ID 或歌单链接')
            return
        self._append_log(f'加载歌单：{playlist_id}')
        self._bg(self._playlist_worker, playlist_id)

    def _playlist_worker(self, playlist_id):
        try:
            title, songs = self.api.playlist_songs(playlist_id)
            if not songs:
                self.msg_queue.put(('log', '歌单为空或加载失败'))
                return
            self.msg_queue.put(('songs', (self.playlist_tree, songs)))
            self.msg_queue.put(('log', f'歌单《{title}》共 {len(songs)} 首'))
        except Exception as exc:  # noqa: BLE001
            self.msg_queue.put(('log', f'歌单加载失败：{exc}'))

    # ---------------- 我的歌单 / 我喜欢的音乐 ----------------
    def _require_login(self) -> bool:
        if self.logged_in:
            return True
        source = self.cfg.get('source', 'netease')
        messagebox.showinfo('提示', f'请先登录{SOURCE_LABELS.get(source, source)}账号')
        return False

    def _load_liked_keys(self):
        """后台拉取「我喜欢的音乐」标识集合，用于列表「喜欢」列。"""
        if self.logged_in:
            self._bg(self._liked_keys_worker)

    def _liked_keys_worker(self):
        try:
            self.msg_queue.put(('liked_keys', (True, self.api.liked_keys())))
        except Exception as exc:  # noqa: BLE001
            self.msg_queue.put(('liked_keys', (False, str(exc))))

    def _apply_liked_keys(self, payload):
        ok, data = payload
        if not ok:
            self._append_log(f'获取「喜欢」状态失败：{data}')
            return
        self.liked_keys = data
        self._refresh_likes(self.search_tree)
        self._refresh_likes(self.playlist_tree)
        self._append_log(f'已同步「我喜欢的音乐」：{len(data)} 首')

    def _refresh_likes(self, tree):
        for iid in tree.get_children():
            song = self._song_at(tree, iid)
            if song is None:
                continue
            values = list(tree.item(iid, 'values'))
            values[8] = '♥' if song_key(song) in self.liked_keys else ''
            tree.item(iid, values=values)

    def load_liked(self):
        """一键把我喜欢的音乐加载到列表。"""
        if not self._require_login():
            return
        self._append_log('正在获取我喜欢的音乐…')
        self._bg(self._liked_worker)

    def _liked_worker(self):
        try:
            title, songs = self.api.liked_songs()
            if not songs:
                self.msg_queue.put(('log', '我喜欢的音乐为空'))
                return
            self.msg_queue.put(('songs', (self.playlist_tree, songs)))
            self.msg_queue.put(('log', f'《{title}》共 {len(songs)} 首'))
        except Exception as exc:  # noqa: BLE001
            self.msg_queue.put(('log', f'获取我喜欢的音乐失败：{exc}'))

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
            self._bg(self._playlist_worker, playlist_id)

        ttk.Button(bar, text='加载此歌单', command=load_selected).pack(side='right')
        ttk.Button(bar, text='关闭', command=win.destroy).pack(side='right', padx=6)
        tree.bind('<Double-1>', load_selected)
        self._my_pl_win, self._my_pl_tree, self._my_pl_status = win, tree, status
        self._my_pl_ids = {}
        self._bg(self._my_playlists_worker)

    def _my_playlists_worker(self):
        try:
            self.msg_queue.put(('my_playlists', (True, self.api.user_playlists())))
        except Exception as exc:  # noqa: BLE001
            self.msg_queue.put(('my_playlists', (False, str(exc))))

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
        songs = self.tree_songs.get(str(tree), [])
        return songs[index] if 0 <= index < len(songs) else None

    def _fill_tree(self, tree, songs):
        tree.delete(*tree.get_children())
        self.tree_songs[str(tree)] = []
        self._sort_state.pop(str(tree), None)   # 重新载入后清除排序标记
        self._append_tree(tree, songs)

    def _append_tree(self, tree, songs):
        """追加歌曲；自动跳过已存在的重复项（去重）。"""
        level = self.cfg.get('level', 'exhigh')
        existing = self.tree_songs.get(str(tree), [])
        seen = {song_key(song) for song in existing}
        added = []
        for song in songs:
            key = song_key(song)
            if key in seen:
                continue
            seen.add(key)
            added.append(song)
        for index, song in enumerate(added, len(existing) + 1):
            mark = 'VIP' if song.fee in (1, 4) else ''
            tree.insert('', 'end', iid=str(index),
                        values=('☐', index, song.name, song.artists,
                                song.album, song.duration_str,
                                song.size_hint(level), mark,
                                '♥' if song_key(song) in self.liked_keys else ''))
        self.tree_songs[str(tree)] = existing + added
        self._update_pick_label(tree)
        skipped = len(songs) - len(added)
        if skipped:
            self._append_log(f'  已跳过 {skipped} 首重复歌曲')

    def _dedupe_tree(self, tree):
        """按歌曲标识去重并重建列表。"""
        songs = self.tree_songs.get(str(tree), [])
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
        self._fill_tree(tree, unique)
        self._append_log(f'去重完成：{len(songs)} → {len(unique)} 首')

    # ---------------- 排序 ----------------
    def _sort_tree(self, tree, column):
        """点击表头排序：首次升序，再点切换倒序。"""
        if not self.tree_songs.get(str(tree)):
            return
        key = str(tree)
        prev_column, prev_reverse = self._sort_state.get(key, (None, False))
        reverse = not prev_reverse if prev_column == column else False
        self._sort_state[key] = (column, reverse)
        level = self.cfg.get('level', 'exhigh')

        def sort_key(iid):
            song = self._song_at(tree, iid)
            if song is None:
                return 0
            if column == 'idx':
                return int(iid)   # 按原始载入顺序
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

        ordered = sorted(tree.get_children(), key=sort_key, reverse=reverse)
        for position, iid in enumerate(ordered):
            tree.move(iid, '', position)
        self._renumber(tree)
        self._update_headers(tree)

    def _renumber(self, tree):
        """让「序号」列始终等于当前视觉顺序。"""
        for position, iid in enumerate(tree.get_children(), 1):
            values = list(tree.item(iid, 'values'))
            values[1] = position
            tree.item(iid, values=values)

    def _update_headers(self, tree):
        column, reverse = self._sort_state.get(str(tree), (None, False))
        for col, label in HEAD_LABELS.items():
            if col == 'pick':
                continue
            text = label + (' ▼' if reverse else ' ▲') if col == column else label
            tree.heading(col, text=text)

    def _toggle_pick(self, tree, event=None):
        """点击「选」列切换勾选状态。"""
        if event is None or tree.identify_region(event.x, event.y) != 'cell':
            return
        if tree.identify_column(event.x) != '#1':   # 第 1 列即勾选列
            return
        iid = tree.identify_row(event.y)
        if not iid:
            return
        values = list(tree.item(iid, 'values'))
        values[0] = '☑' if values[0] != '☑' else '☐'
        tree.item(iid, values=values)
        self._update_pick_label(tree)

    def _set_all_pick(self, tree, picked):
        mark = '☑' if picked else '☐'
        for iid in tree.get_children():
            values = list(tree.item(iid, 'values'))
            values[0] = mark
            tree.item(iid, values=values)
        self._update_pick_label(tree)

    def _picked_songs(self, tree):
        """返回勾选的歌曲；没勾选时回退到高亮选中的行。"""
        result = [song for iid in tree.get_children()
                  if (song := self._song_at(tree, iid)) is not None
                  and (tree.item(iid, 'values') or [''])[0] == '☑']
        if result:
            return result
        return [song for iid in tree.selection()
                if (song := self._song_at(tree, iid)) is not None]

    def _update_pick_label(self, tree):
        label = self.pick_labels.get(str(tree))
        if label is None:
            return
        count = sum(1 for iid in tree.get_children()
                    if tree.item(iid, 'values') and tree.item(iid, 'values')[0] == '☑')
        label.config(text=f'已选 {count} 首')

    def _active_tree(self):
        index = self.nb.index(self.nb.select())
        if index == 0:
            return self.search_tree
        if index == 1:
            return self.playlist_tree
        return None

    # ---------------- 右键菜单 / 详情 ----------------
    def _toggle_one(self, tree, iid):
        values = list(tree.item(iid, 'values'))
        values[0] = '☐' if values[0] == '☑' else '☑'
        tree.item(iid, values=values)
        self._update_pick_label(tree)

    def _copy_text(self, text):
        self.clipboard_clear()
        self.clipboard_append(text)
        self._append_log(f'已复制：{text}')

    def _open_detail_at(self, tree, event):
        """双击行（非勾选列）打开详情。"""
        if tree.identify_column(event.x) == '#1':
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
        checked = (tree.item(iid, 'values') or ['☐'])[0] == '☑'
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
                   command=lambda: self._bg(self._probe_qualities, song, probe_label, win)).pack(
            anchor='w', pady=10)
        ttk.Button(right, text='下载此曲',
                   command=lambda: self._start_download([song])).pack(anchor='w')
        self._bg(self._load_cover, song, cover_label, win)

    def _load_cover(self, song, label, win):
        image = None
        try:
            if song.cover_url:
                resp = requests.get(song.cover_url, timeout=15)
                image = Image.open(io.BytesIO(resp.content)).convert('RGB').resize((300, 300))
        except Exception as exc:  # noqa: BLE001
            self.msg_queue.put(('log', f'封面加载失败：{exc}'))
        if image is None:
            self.msg_queue.put(('probe_done', (win, label, '（无封面）')))
            return
        self.msg_queue.put(('detail_cover', (win, label, image)))

    def _probe_qualities(self, song, label, win):
        rows = []
        for level, level_label in LEVELS_BY_SOURCE.get(song.source, LEVELS_BY_SOURCE['netease']):
            try:
                info = self.api.song_url(song, level, self.cfg.get('encode_type', 'mp3'))
                detail = f"{info.get('type', '').upper()} {human_size(info.get('size'))}"
                rows.append(f'{level_label}：可用（{detail}）')
            except Exception:  # noqa: BLE001 - 探测失败即视为不可用
                rows.append(f'{level_label}：不可用')
        self.msg_queue.put(('probe_done', (win, label, '\n'.join(rows))))

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
        self._start_download(list(self.tree_songs.get(str(tree), [])))

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
        win.geometry('400x360')
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
        skip_var = tk.BooleanVar(value=self.cfg.get('skip_existing', True))
        opts = ttk.Frame(win, padding=12)
        opts.pack(fill='x')
        ttk.Checkbutton(opts, text='下载歌词 (.lrc)', variable=lyric_var).pack(anchor='w')
        ttk.Checkbutton(opts, text='嵌入标签（歌名 / 歌手 / 专辑）', variable=tag_var).pack(anchor='w')
        ttk.Checkbutton(opts, text='嵌入封面', variable=cover_var).pack(anchor='w')
        ttk.Checkbutton(opts, text='跳过已存在的文件', variable=skip_var).pack(anchor='w')
        ttk.Label(win, text=f'共 {len(songs)} 首（切换音质会同步刷新列表「大小≈」）',
                  padding=(12, 0)).pack(anchor='w')

        # 切换音质时实时预览列表里的大小估算，无需先下载
        def preview(_event=None):
            self._refresh_sizes(self.search_tree, label_levels.get(level_var.get(), current))
            self._refresh_sizes(self.playlist_tree, label_levels.get(level_var.get(), current))

        level_box.bind('<<ComboboxSelected>>', preview)

        def confirm():
            level = label_levels.get(level_var.get(), current)
            options = {
                'save_lyric': bool(lyric_var.get()),
                'embed_tag': bool(tag_var.get()),
                'embed_cover': bool(cover_var.get()),
            }
            # 记住本次选择，作为下次默认（避免设置页再维护一份重复选项）
            self.cfg['level'] = level
            self.cfg['save_lyric'] = options['save_lyric']
            self.cfg['embed_tag'] = options['embed_tag']
            self.cfg['embed_cover'] = options['embed_cover']
            self.cfg['skip_existing'] = bool(skip_var.get())
            cfgmod.save_config(self.cfg)
            self._refresh_sizes(self.search_tree)
            self._refresh_sizes(self.playlist_tree)
            win.destroy()
            self._dl_control = Control()
            self._downloading = True
            self._sync_download_buttons()
            self._bg(self._download_worker, songs, level, options, self._dl_control)

        ttk.Button(win, text='开始下载', command=confirm).pack(pady=14)

    def _download_worker(self, songs, level=None, options=None, control=None):
        control = control or Control()
        downloader = Downloader(
            self.api, self.cfg,
            on_log=lambda text: self.msg_queue.put(('log', text)),
            on_progress=lambda ratio: self.msg_queue.put(('progress', ratio)),
            control=control,
        )
        total = len(songs)
        success = 0
        failed = []
        cancelled = False
        try:
            for index, song in enumerate(songs, 1):
                if control.cancelled:
                    cancelled = True
                    break
                control.wait_if_paused()
                self.msg_queue.put(('status', f'[{index}/{total}] {song.name} - {song.artists}'))
                self.msg_queue.put(('log', f'[{index}/{total}] {song.name} - {song.artists}'))
                try:
                    downloader.download(song, level=level, options=options)
                    success += 1
                except DownloadCancelled:
                    cancelled = True
                    self.msg_queue.put(('log', f'  已取消：{song.name} - {song.artists}'))
                    break
                except Exception as exc:  # noqa: BLE001
                    failed.append((song, str(exc)))
                    self.msg_queue.put(('log', f'  失败：{exc}'))
                time.sleep(0.3)
        finally:
            # 收尾一律回到界面线程处理（含取消后的清理询问）
            self.msg_queue.put(('download_end', {
                'total': total, 'success': success, 'failed': failed,
                'cancelled': cancelled, 'downloader': downloader,
            }))

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

    def _cleanup_after_cancel(self, downloader):
        """取消后按设置处理本次产生的文件（未完成的 .part 与已下载的音频/歌词）。"""
        produced = list(downloader.produced)
        pending = downloader.pending
        if not produced and not pending:
            return
        setting = self.cfg.get('cancel_cleanup', 'ask')
        if setting == 'ask':
            count = len(produced) + (1 if pending else 0)
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
    def _check_account(self):
        source = self.cfg.get('source', 'netease')
        if source == 'netease' and self.cfg.get('use_anonimous') and not self.cfg.get('cookie'):
            try:
                self.api.register_anonimous()
                self.msg_queue.put(('log', '匿名注册成功（已获取匿名 token）'))
            except Exception as exc:  # noqa: BLE001
                self.msg_queue.put(('log', f'匿名注册失败（不影响使用）：{exc}'))
        try:
            profile = self.api.account()
        except Exception as exc:  # noqa: BLE001
            self.msg_queue.put(('log', f'账号校验失败：{exc}'))
            profile = None
        image = self._fetch_avatar(profile.get('avatarUrl')) if (profile and source == 'netease') else None
        self.msg_queue.put(('account', (profile, image)))

    def _fetch_avatar(self, url):
        """拉取头像缩略图；失败不影响登录信息展示。"""
        if not url:
            return None
        try:
            resp = self.api.session.get(url + '?param=64y64', timeout=10)
            return Image.open(io.BytesIO(resp.content)).convert('RGB').resize((36, 36))
        except Exception:  # noqa: BLE001
            return None

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
        cfgmod.save_config(self.cfg)
        self.api.set_cookie(cookie)
        self._append_log('网易云登录成功，Cookie 已写入 config.json')
        self._bg(self._check_account)

    def _on_qq_login(self):
        self.cfg['qq_credential'] = self.api.credential_dict()
        cfgmod.save_config(self.cfg)
        self._append_log('QQ 音乐登录成功，凭证已写入 config.json')
        self._bg(self._check_account)

    def do_logout(self):
        source = self.cfg.get('source', 'netease')
        if source == 'qq':
            self.cfg['qq_credential'] = {}
            self.api.set_credential(None)
        else:
            self.cfg['cookie'] = ''
            self.api.set_cookie('')
        cfgmod.save_config(self.cfg)
        self.logged_sources[source] = None
        self.logged_in = False
        self.liked_keys = set()
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
            self._bg(self._qq_cookie_apply, cookie)

        ttk.Button(win, text='登录', command=apply).pack(pady=10)

    def _qq_cookie_apply(self, cookie):
        try:
            self.api.set_cookie(cookie)
        except Exception as exc:  # noqa: BLE001
            self.msg_queue.put(('error', f'QQ 音乐登录失败：{exc}'))
            return
        self._on_qq_login()

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
        self._qr_stop = False
        win.protocol('WM_DELETE_WINDOW', lambda: self._stop_qr(win))
        self._bg(self._qr_worker, win, label)

    def _stop_qr(self, win):
        self._qr_stop = True
        win.destroy()

    def _qr_worker(self, win, label):
        try:
            unikey = self.api.qrcode_unikey()
        except Exception as exc:  # noqa: BLE001
            self.msg_queue.put(('log', f'获取二维码失败：{exc}'))
            return
        if not unikey:
            self.msg_queue.put(('log', '获取二维码失败：unikey 为空'))
            return
        image = qrcode.make(f'https://music.163.com/login?codekey={unikey}').convert('RGB')
        self.msg_queue.put(('qr_image', (win, label, image)))

        deadline = time.time() + 180
        while not self._qr_stop and time.time() < deadline:
            time.sleep(2)
            try:
                result = self.api.qrcode_check(unikey)
            except Exception as exc:  # noqa: BLE001
                self.msg_queue.put(('log', f'轮询失败：{exc}'))
                continue
            code = result.get('code')
            if code == 800:
                self.msg_queue.put(('log', '二维码已过期，请重新获取'))
                return
            if code == 802:
                self.msg_queue.put(('status', '已扫码，请在手机上确认'))
            elif code == 803:
                cookie = result.get('cookie') or self.api.cookie_str()
                self.msg_queue.put(('login_ok', cookie))
                self.msg_queue.put(('close_qr', win))
                return

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
            self._bg(self._qq_qr_worker, win, label, hint, session, type_labels[type_var.get()])

        box.bind('<<ComboboxSelected>>', refresh)
        win.protocol('WM_DELETE_WINDOW', stop)
        refresh()

    def _qq_qr_worker(self, win, label, hint, session, login_type):
        if session['stop'] or not win.winfo_exists():
            return
        try:
            data, _identifier = self.api.qr_login_start(login_type)
        except Exception as exc:  # noqa: BLE001
            self.msg_queue.put(('log', f'获取二维码失败：{exc}'))
            return
        try:
            image = Image.open(io.BytesIO(data)).convert('RGB')
        except Exception as exc:  # noqa: BLE001
            self.msg_queue.put(('log', f'二维码图片解析失败：{exc}'))
            return
        self.msg_queue.put(('qr_image', (win, label, image)))

        deadline = time.time() + 180
        while not session['stop'] and time.time() < deadline:
            time.sleep(2)
            if session['stop'] or not win.winfo_exists():
                return
            try:
                event, note = self.api.qr_login_poll()
            except Exception as exc:  # noqa: BLE001
                self.msg_queue.put(('log', f'轮询失败：{exc}'))
                continue
            if event in ('scan', 'conf'):
                self.msg_queue.put(('probe_done', (win, hint, note)))
            elif event == 'done':
                self.msg_queue.put(('qq_login_ok', None))
                self.msg_queue.put(('close_qr', win))
                return
            elif event in ('timeout', 'refuse'):
                self.msg_queue.put(('probe_done', (win, hint, note)))
                return

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
        self.cfg['cancel_cleanup'] = LABEL_CANCEL_CLEANUP.get(
            self.cancel_cleanup_var.get(), 'ask')
        cfgmod.save_config(self.cfg)
        self._append_log('设置已保存')

    def _refresh_sizes(self, tree, level=None):
        level = level or self.cfg.get('level', 'exhigh')
        for iid in tree.get_children():
            song = self._song_at(tree, iid)
            if song is None:
                continue
            values = list(tree.item(iid, 'values'))
            values[6] = song.size_hint(level)
            tree.item(iid, values=values)