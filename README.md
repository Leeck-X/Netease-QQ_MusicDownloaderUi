# Netease-QQ_MusicDownloaderUi

网易云音乐 / QQ 音乐 歌曲下载器，Python + Tkinter 图形界面。

> 仅供个人学习与备份使用，请遵守相关音乐平台的服务条款与版权规定，勿用于商业用途。

## 功能一览

**双音源**
- 顶部下拉切换「网易云音乐 / QQ 音乐」，登录态各自独立保存
- 统一的音源接口，音质档位随音源自动校正

**搜索与歌单**
- 关键词搜索，每页 50 首；滚动到列表底部自动预加载下一页，也可手动「加载更多」
- 歌单支持 ID / 链接加载
- 「我的歌单」窗口可浏览自己创建与收藏的歌单
- 一键获取「我喜欢的音乐」到列表

**列表交互**
- 多选复选框、全选 / 清空勾选、实时已选计数
- 点击表头排序、一键去重
- 右键快速操作菜单，双击打开歌曲详情（标签信息、封面预览、音质探测）
- 「喜欢」列标记已在「我喜欢的音乐」中的歌曲
- 「大小≈」列按当前音质预估文件大小

**下载**
- 音质档位与编码格式选择，命名模板可自定义
- 可写入歌词（含翻译）、歌曲标签与内嵌封面
- 跳过已存在的文件、失败自动重试
- 下载选项弹窗确认后会记忆为下次默认
- 下载中可**暂停 / 继续 / 取消**，断点续传（HTTP Range），暂停后继续不会重新下载
- 取消下载后可选「每次询问 / 总是删除 / 总是保留」本次产生的文件

**登录**
- Cookie 登录、扫码登录（网易云与 QQ 均支持）
- 顶部展示头像、昵称、ID、VIP 类型

## 环境要求

- Windows
- Python 3.10 及以上（开发环境为 3.14）

## 安装与运行

```bash
pip install -r requirements.txt
python main.py
```

## 配置文件

程序读取同目录下的 `config.json`（不存在时使用内置默认值，点击「保存设置」后写入）。
已内置网易云 API 凭证，无需自行配置。**`config.json` 会保存你的登录 Cookie，请勿提交到公开仓库。**

## 打包为单文件 exe

```bash
pip install pyinstaller
python -m PyInstaller --noconfirm --clean --onefile --windowed --name MusicDownloader ^
  --collect-all qqmusic_api ^
  --hidden-import mutagen.id3 --hidden-import mutagen.flac --hidden-import mutagen.mp4 ^
  --hidden-import qrcode.image.pil main.py
```

生成的 `MusicDownloader.exe` 为单文件、静态链接，目标机器无需安装 Python。exe 运行时会以**自身所在目录**作为配置与下载目录，因此整个文件夹可随意移动。

## 目录结构

```
.
├── main.py            # 程序入口
├── ui.py              # Tkinter 界面
├── providers.py       # 音源工厂（网易云 / QQ）
├── api.py             # 网易云 Provider
├── qq_api.py          # QQ 音乐 Provider
├── downloader.py      # 下载器（进度 / 重试 / 暂停取消 / 标签歌词）
├── encrypt.py         # 网易云 weapi 加密
├── models.py          # 数据模型（Song 等）
├── config.py          # 配置读写
└── requirements.txt   # 依赖清单
```
