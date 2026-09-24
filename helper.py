# -*- coding: utf-8 -*-
"""Helper 更新程序 —— 图形界面版启动器

检查 GitHub Release 更新，应用后启动主程序。
版本判定：比较本地版本与 GitHub 最新 release 对应 tag 的 commit 时间。

更新策略：
  1. 全量备份 BASE_DIR 到 _backup（仅用于失败回滚）
  2. 删除 BASE_DIR 中除 KEEP_ITEMS、_backup、helper.log 之外的所有内容
  3. 解压新包到 BASE_DIR
  4. 任一步失败 -> 用 _backup 回滚
  5. 更新成功 -> 由退出时启动的 _cleanup.bat 删除 _backup（避免自身占用）
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

GITHUB_REPO = 'BaiZiDog/helper-test'
BASE_DIR = os.path.dirname(os.path.abspath(
    sys.executable if getattr(sys, 'frozen', False) else __file__
))
MAIN_EXE = os.path.join(BASE_DIR, 'RandomNamePicker.exe')

# 当前版本号（硬编码，每次发版时同步更新）
LOCAL_VERSION = 'v1.5'

# 更新时保留在根目录的内容（不会被删除、不会被覆盖）
KEEP_ITEMS = {'helper.exe', 'data'}

# 备份目录名（仅失败回滚用，更新成功后由批处理删除）
BACKUP_DIR_NAME = '_backup'
# 本 Helper 自身的可执行文件名
SELF_NAME = os.path.basename(
    sys.executable if getattr(sys, 'frozen', False) else __file__
).lower()

# 配色
BG = '#2b2b3d'
FG = '#ffffff'
ACCENT = '#667eea'
MUTED = '#9a9ab0'


def log(msg):
    """写入日志文件便于排查"""
    try:
        with open(os.path.join(BASE_DIR, 'helper.log'), 'a', encoding='utf-8') as f:
            import time as _t
            f.write(f'[{_t.strftime("%Y-%m-%d %H:%M:%S")}] {msg}\n')
    except OSError:
        pass


def github_get(path):
    url = f'https://api.github.com/repos/{GITHUB_REPO}{path}'
    req = urllib.request.Request(url, headers={
        'Accept': 'application/vnd.github.v3+json',
        'User-Agent': 'RandomNamePicker-Helper',
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode('utf-8'))


def get_tag_commit_date(tag):
    try:
        ref = github_get(f'/git/refs/tags/{tag}')
        sha = ref['object']['sha']
        if ref['object']['type'] == 'tag':
            tag_obj = github_get(f'/git/tags/{sha}')
            sha = tag_obj['object']['sha']
        commit = github_get(f'/git/commits/{sha}')
        return commit['committer']['date']
    except Exception as e:
        log(f'获取 {tag} 时间失败：{e}')
        return None


def get_latest_release():
    try:
        return github_get('/releases/latest')
    except Exception as e:
        log(f'获取最新版本失败：{e}')
        return None


def download_file(url, dest, progress_cb=None):
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


# ---------------------------------------------------------------------------
# 更新核心：备份 -> 清理 -> 解压
# ---------------------------------------------------------------------------

def _abs(p):
    return os.path.abspath(p)


def _is_running_self(path):
    return _abs(path).lower() == _abs(os.path.join(BASE_DIR, SELF_NAME)).lower()


def backup_current(backup_root):
    """把 BASE_DIR 下所有内容备份到 backup_root（排除 _backup 自身）"""
    os.makedirs(backup_root, exist_ok=True)
    for item in os.listdir(BASE_DIR):
        if item == BACKUP_DIR_NAME:
            continue
        src = os.path.join(BASE_DIR, item)
        dst = os.path.join(backup_root, item)
        if os.path.isdir(src) and not os.path.islink(src):
            shutil.copytree(src, dst, symlinks=True)
        else:
            shutil.copy2(src, dst, follow_symlinks=False)


def clean_dir():
    """删除 BASE_DIR 下除 KEEP_ITEMS、_backup、helper.log 外的所有内容"""
    for item in os.listdir(BASE_DIR):
        if item in KEEP_ITEMS:
            continue
        if item in (BACKUP_DIR_NAME, 'helper.log'):
            continue
        path = os.path.join(BASE_DIR, item)
        if _is_running_self(path):
            # 正在运行的自己删不掉，跳过
            continue
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.remove(path)


def safe_extract(zip_path, target_dir):
    """安全解压，防 Zip Slip。包内同名 helper.exe 落为 helper.exe.new。"""
    abs_target = _abs(target_dir)
    with zipfile.ZipFile(zip_path, 'r') as zf:
        for member in zf.namelist():
            member_path = _abs(os.path.join(abs_target, member))
            if member_path != abs_target and not member_path.startswith(abs_target + os.sep):
                raise Exception(f'非法压缩路径：{member}')

        for info in zf.infolist():
            base = os.path.basename(info.filename).lower()
            if base == SELF_NAME and not info.is_dir():
                dst = os.path.join(BASE_DIR, base + '.new')
                with zf.open(info) as src, open(dst, 'wb') as f:
                    shutil.copyfileobj(src, f)
            else:
                zf.extract(info, target_dir)


def rollback(backup_path):
    """从备份回滚：清空（跳过 _backup 和正在运行的自己）后还原"""
    log(f'开始回滚，来源：{backup_path}')
    for item in os.listdir(BASE_DIR):
        if item == BACKUP_DIR_NAME:
            continue
        path = os.path.join(BASE_DIR, item)
        if _is_running_self(path):
            continue
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
        except Exception as e:
            log(f'回滚清理 {item} 失败：{e}')

    for item in os.listdir(backup_path):
        src = os.path.join(backup_path, item)
        dst = os.path.join(BASE_DIR, item)
        if _is_running_self(dst):
            continue
        try:
            if os.path.isdir(src) and not os.path.islink(src):
                shutil.copytree(src, dst, symlinks=True)
            else:
                shutil.copy2(src, dst, follow_symlinks=False)
        except Exception as e:
            log(f'回滚还原 {item} 失败：{e}')


def apply_update(zip_path):
    """备份 -> 清理 -> 解压。返回 (ok: bool, message: str)"""
    backup_root = os.path.join(BASE_DIR, BACKUP_DIR_NAME)

    # 清掉可能残留的旧备份（例如上次失败后遗留）
    shutil.rmtree(backup_root, ignore_errors=True)

    # 1. 备份
    try:
        backup_current(backup_root)
        log(f'备份完成：{backup_root}')
    except Exception as e:
        log(f'备份失败：{e}')
        shutil.rmtree(backup_root, ignore_errors=True)
        return False, '备份失败'

    # 2. 清理（除 KEEP_ITEMS 外全删）
    try:
        clean_dir()
        log('清理完成')
    except Exception as e:
        log(f'清理失败：{e}，回滚中')
        rollback(backup_root)
        return False, '清理失败，已回滚'

    # 3. 解压新文件
    try:
        safe_extract(zip_path, BASE_DIR)
        log('解压完成')
    except Exception as e:
        log(f'解压失败：{e}，回滚中')
        rollback(backup_root)
        return False, '解压失败，已回滚'

    return True, '成功'


# ---------------------------------------------------------------------------
# 退出前调度批处理：删除 _backup + 替换 helper.exe.new
# ---------------------------------------------------------------------------

def schedule_cleanup(delete_backup, target_exe):
    """生成 _cleanup.bat 并启动，在 Helper 退出后执行清理/自替换。

    delete_backup: 是否删除 _backup（只有更新成功才为 True）
    target_exe:    当前 Helper 可执行文件路径
    """
    backup_root = os.path.join(BASE_DIR, BACKUP_DIR_NAME)
    new_exe = target_exe + '.new'

    has_backup = delete_backup and os.path.isdir(backup_root)
    has_self_replace = os.path.exists(new_exe)

    if not has_backup and not has_self_replace:
        return

    bat_path = os.path.join(BASE_DIR, '_cleanup.bat')

    lines = [
        '@echo off',
        'chcp 65001 >nul',
        # 等 Helper 进程完全退出
        'ping 127.0.0.1 -n 3 >nul',
    ]

    # ---- 1. 自替换 helper.exe.new -> helper.exe ----
    if has_self_replace:
        lines += [
            f'if not exist "{new_exe}" goto :skip_move',
            ':retry_move',
            f'del "{target_exe}" >nul 2>&1',
            f'if not exist "{target_exe}" goto :do_move',
            'ping 127.0.0.1 -n 2 >nul',
            'goto :retry_move',
            ':do_move',
            f'move /y "{new_exe}" "{target_exe}" >nul',
            ':skip_move',
        ]

    # ---- 2. 删除 _backup ----
    if has_backup:
        lines += [
            f'if not exist "{backup_root}" goto :skip_backup',
            ':retry_backup',
            f'rmdir /s /q "{backup_root}" >nul 2>&1',
            f'if not exist "{backup_root}" goto :skip_backup',
            'ping 127.0.0.1 -n 2 >nul',
            'goto :retry_backup',
            ':skip_backup',
        ]

    lines += ['del "%~f0"']

    try:
        with open(bat_path, 'w', encoding='gbk') as f:
            f.write('\r\n'.join(lines) + '\r\n')
        subprocess.Popen(
            ['cmd', '/c', bat_path],
            cwd=BASE_DIR,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        log(f'已调度清理批处理（删备份={has_backup}，自替换={has_self_replace}）')
    except Exception as e:
        log(f'调度清理批处理失败：{e}')


def launch_main():
    if os.path.exists(MAIN_EXE):
        subprocess.Popen([MAIN_EXE], cwd=BASE_DIR)
        return True
    log(f'主程序不存在：{MAIN_EXE}')
    return False


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class HelperApp:

    def __init__(self, root):
        self.root = root
        self.update_success = False

        self.root.title('随机点名工具 - 启动器')
        self.root.resizable(False, False)
        self.root.configure(bg=BG)

        w, h = 420, 260
        self.root.update_idletasks()
        x = (self.root.winfo_screenwidth() - w) // 2
        y = (self.root.winfo_screenheight() - h) // 2
        self.root.geometry(f'{w}x{h}+{x}+{y}')

        tk.Label(root, text='随 机 点 名', font=('Microsoft YaHei', 18, 'bold'),
                 bg=BG, fg=FG).pack(pady=(28, 4))

        self.ver_label = tk.Label(root, text=f'当前版本：{LOCAL_VERSION}',
                                  font=('Microsoft YaHei', 10), bg=BG, fg=MUTED)
        self.ver_label.pack()

        self.status = tk.Label(root, text='正在检查更新...',
                               font=('Microsoft YaHei', 11), bg=BG, fg=FG)
        self.status.pack(pady=(22, 8))

        style = ttk.Style()
        style.theme_use('default')
        style.configure('Helper.Horizontal.TProgressbar',
                        troughcolor='#3d3d52', background=ACCENT,
                        bordercolor=BG, lightcolor=ACCENT, darkcolor=ACCENT)
        self.progress = ttk.Progressbar(root, style='Helper.Horizontal.TProgressbar',
                                        length=320, mode='determinate')
        self.progress.pack()

        self.btn = tk.Button(root, text='检查中...', font=('Microsoft YaHei', 11),
                             bg=ACCENT, fg=FG, activebackground='#5568d3',
                             activeforeground=FG, relief='flat',
                             width=14, state='disabled')
        self.btn.pack(pady=(22, 0))

        self.root.after(200, lambda: threading.Thread(
            target=self.check, daemon=True).start())

    def set_status(self, text, color=FG):
        self.status.config(text=text, fg=color)

    def set_progress(self, value):
        self.progress['value'] = value * 100

    def check(self):
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

        self.root.after(0, lambda: self.set_status('正在应用更新...'))
        ok, msg = apply_update(zip_path)

        if os.path.exists(zip_path):
            try:
                os.remove(zip_path)
            except OSError:
                pass

        if ok:
            self.update_success = True
            log(f'更新成功！版本：{remote_tag}')
            self.root.after(0, lambda: self.set_status(
                f'更新完成（{remote_tag}）', '#7ee787'))
        else:
            log(f'更新失败：{msg}')
            self.root.after(0, lambda: self.set_status(msg, '#ffb86c'))

        self.root.after(0, self.enable_start)

    def enable_start(self):
        self.btn.config(text='启动中...')
        self.root.after(800, self.auto_launch)

    def auto_launch(self):
        # 先调度清理批处理（只在成功时删 _backup；有 helper.exe.new 时自替换）
        schedule_cleanup(
            delete_backup=self.update_success,
            target_exe=os.path.join(BASE_DIR, SELF_NAME),
        )
        launch_main()
        self.root.destroy()


def main():
    root = tk.Tk()
    HelperApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()