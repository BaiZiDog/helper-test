# -*- coding: utf-8 -*-
"""Helper 更新程序 —— 图形界面版启动器

检查 GitHub Release 更新，应用后启动主程序。
版本判定：比较本地版本与 GitHub 最新 release 对应 tag 的 commit 时间。
"""
import os
import sys
import json
import zipfile
import subprocess
import shutil
import threading
import urllib.request
import tkinter as tk
from tkinter import ttk

GITHUB_REPO = 'BaiZiDog/RandomNamePicker'
BASE_DIR = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, 'frozen', False) else __file__))
MAIN_EXE = os.path.join(BASE_DIR, 'RandomNamePicker.exe')

# 当前版本号（硬编码，每次发版时同步更新）
LOCAL_VERSION = 'v1.5-fix'

# 更新时保留的文件/目录（helper 自身 + 用户数据）
KEEP_ITEMS = {'helper.exe', 'data'}

# 配色
BG = '#2b2b3d'
FG = '#ffffff'
ACCENT = '#667eea'
MUTED = '#9a9ab0'


def log(msg):
    """写入日志文件便于排查"""
    try:
        with open(os.path.join(BASE_DIR, 'helper.log'), 'a', encoding='utf-8') as f:
            f.write(msg + '\n')
    except OSError:
        pass


def github_get(path):
    """请求 GitHub API，返回解析后的 JSON"""
    url = f'https://api.github.com/repos/{GITHUB_REPO}{path}'
    req = urllib.request.Request(url, headers={
        'Accept': 'application/vnd.github.v3+json',
        'User-Agent': 'RandomNamePicker-Helper',
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode('utf-8'))


def get_tag_commit_date(tag):
    """获取指定 tag 对应 commit 的时间戳（ISO 8601 字符串）"""
    try:
        ref = github_get(f'/git/refs/tags/{tag}')
        sha = ref['object']['sha']
        # annotated tag 需再解析一层
        if ref['object']['type'] == 'tag':
            tag_obj = github_get(f'/git/tags/{sha}')
            sha = tag_obj['object']['sha']
        commit = github_get(f'/git/commits/{sha}')
        return commit['committer']['date']
    except Exception as e:
        log(f'获取 {tag} 时间失败：{e}')
        return None


def get_latest_release():
    """获取 GitHub 最新 release 信息"""
    try:
        return github_get('/releases/latest')
    except Exception as e:
        log(f'获取最新版本失败：{e}')
        return None


def download_file(url, dest, progress_cb=None):
    """下载文件到指定路径，可选进度回调"""
    req = urllib.request.Request(url, headers={'User-Agent': 'RandomNamePicker-Helper'})
    with urllib.request.urlopen(req, timeout=120) as resp:
        total = int(resp.headers.get('Content-Length', 0))
        done = 0
        with open(dest, 'wb') as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if progress_cb and total:
                    progress_cb(done / total)


def apply_update(zip_path):
    """应用更新：删除除 helper.exe、data/ 和 app.zip 外的文件，解压 app.zip 到根目录"""
    for item in os.listdir(BASE_DIR):
        if item.lower() in KEEP_ITEMS or item in ('helper.log', 'app.zip'):
            continue
        item_path = os.path.join(BASE_DIR, item)
        try:
            if os.path.isfile(item_path) or os.path.islink(item_path):
                os.remove(item_path)
            elif os.path.isdir(item_path):
                shutil.rmtree(item_path)
        except Exception as e:
            log(f'删除 {item} 失败：{e}')

    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(BASE_DIR)
        return True
    except Exception as e:
        log(f'解压失败：{e}')
        return False


def launch_main():
    """启动主程序"""
    if os.path.exists(MAIN_EXE):
        subprocess.Popen([MAIN_EXE], cwd=BASE_DIR)
        return True
    log(f'主程序不存在：{MAIN_EXE}')
    return False


class HelperApp:
    """更新器图形界面"""

    def __init__(self, root):
        self.root = root
        self.root.title('随机点名工具 - 启动器')
        self.root.geometry('420x260')
        self.root.resizable(False, False)
        self.root.configure(bg=BG)

        # 居中显示
        self.root.update_idletasks()
        w, h = 420, 260
        x = (self.root.winfo_screenwidth() - w) // 2
        y = (self.root.winfo_screenheight() - h) // 2
        self.root.geometry(f'{w}x{h}+{x}+{y}')

        # 标题
        tk.Label(root, text='随 机 点 名', font=('Microsoft YaHei', 18, 'bold'),
                 bg=BG, fg=FG).pack(pady=(28, 4))

        # 版本信息
        self.ver_label = tk.Label(root, text=f'当前版本：{LOCAL_VERSION}',
                                  font=('Microsoft YaHei', 10), bg=BG, fg=MUTED)
        self.ver_label.pack()

        # 状态文字
        self.status = tk.Label(root, text='正在检查更新...',
                               font=('Microsoft YaHei', 11), bg=BG, fg=FG)
        self.status.pack(pady=(22, 8))

        # 进度条
        style = ttk.Style()
        style.theme_use('default')
        style.configure('Helper.Horizontal.TProgressbar',
                        troughcolor='#3d3d52', background=ACCENT,
                        bordercolor=BG, lightcolor=ACCENT, darkcolor=ACCENT)
        self.progress = ttk.Progressbar(root, style='Helper.Horizontal.TProgressbar',
                                        length=320, mode='determinate')
        self.progress.pack()

        # 按钮（仅作状态显示，检查完成后自动启动）
        self.btn = tk.Button(root, text='检查中...', font=('Microsoft YaHei', 11),
                             bg=ACCENT, fg=FG, activebackground='#5568d3',
                             activeforeground=FG, relief='flat',
                             width=14, state='disabled')
        self.btn.pack(pady=(22, 0))

        self.root.after(200, lambda: threading.Thread(target=self.check, daemon=True).start())

    def set_status(self, text, color=FG):
        self.status.config(text=text, fg=color)

    def set_progress(self, value):
        self.progress['value'] = value * 100

    def check(self):
        """后台线程：检查更新并应用"""
        log('=' * 40)
        log(f'本地版本：{LOCAL_VERSION}')

        release = get_latest_release()
        if not release:
            self.root.after(0, lambda: self.set_status('无法连接 GitHub', '#ffb86c'))
            self.root.after(0, self.enable_start)
            return

        remote_tag = release.get('tag_name', '')
        log(f'最新版本：{remote_tag}')

        local_date = get_tag_commit_date(LOCAL_VERSION)
        remote_date = get_tag_commit_date(remote_tag) if remote_tag else None

        if local_date and remote_date and remote_date <= local_date:
            log('已是最新版本')
            self.root.after(0, lambda: self.set_status('已是最新版本', '#7ee787'))
            self.root.after(0, self.enable_start)
            return

        # 查找 app.zip
        zip_asset = None
        for asset in release.get('assets', []):
            if asset['name'] == 'app.zip':
                zip_asset = asset
                break
        if not zip_asset:
            for asset in release.get('assets', []):
                if asset['name'].endswith('.zip'):
                    zip_asset = asset
                    break

        if not zip_asset:
            log('未找到更新包（app.zip）')
            self.root.after(0, lambda: self.set_status('未找到更新包', '#ffb86c'))
            self.root.after(0, self.enable_start)
            return

        # 下载
        self.root.after(0, lambda: self.set_status(f'发现新版本 {remote_tag}，下载中...'))
        zip_path = os.path.join(BASE_DIR, 'app.zip')
        try:
            download_file(zip_asset['browser_download_url'], zip_path,
                          lambda p: self.root.after(0, lambda: self.set_progress(p)))
        except Exception as e:
            log(f'下载失败：{e}')
            self.root.after(0, lambda: self.set_status('下载失败', '#ff7b72'))
            self.root.after(0, self.enable_start)
            return

        # 应用更新
        self.root.after(0, lambda: self.set_status('正在应用更新...'))
        ok = apply_update(zip_path)
        if os.path.exists(zip_path):
            try:
                os.remove(zip_path)
            except OSError:
                pass

        if ok:
            log(f'更新成功！版本：{remote_tag}')
            self.root.after(0, lambda: self.set_status(f'更新完成（{remote_tag}）', '#7ee787'))
        else:
            log('更新失败，保留旧版本')
            self.root.after(0, lambda: self.set_status('更新失败，保留旧版本', '#ffb86c'))

        self.root.after(0, self.enable_start)

    def enable_start(self):
        """检查完成，自动启动主程序"""
        self.btn.config(text='启动中...')
        self.root.after(800, self.auto_launch)

    def auto_launch(self):
        launch_main()
        self.root.destroy()


def main():
    root = tk.Tk()
    HelperApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
