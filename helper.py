# -*- coding: utf-8 -*-
"""Helper 更新程序 —— 图形界面版启动器

检查 GitHub Release 更新，应用后启动主程序。
版本判定：比较本地版本与 GitHub 最新 release 对应 tag 的 commit 时间。

更新策略：
  1. 全量备份 BASE_DIR 到 _backup（仅用于失败回滚）
  2. 删除 BASE_DIR 中除 KEEP_ITEMS、_backup、helper.log 之外的所有内容
  3. 解压新包到 _staging 暂存目录，按规则分类（替换/跳过/删除）
  4. 任一步失败 -> 用 _backup 回滚
  5. 更新成功 -> 退出后由 _replace.bat 分批替换文件并删除 _backup
"""
import os
import sys
import json
import time
import socket
import atexit
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
LOCAL_VERSION = 'v1.0'

# 更新时保留在根目录的内容（不会被删除、不会被覆盖）
KEEP_ITEMS = {'helper.exe', 'data'}

# 备份目录名（仅失败回滚用，更新成功后由批处理删除）
BACKUP_DIR_NAME = '_backup'
# 本 Helper 自身的可执行文件名
SELF_NAME = os.path.basename(
    sys.executable if getattr(sys, 'frozen', False) else __file__
).lower()

# 命名 Mutex 名称（系统范围内唯一，用于单实例互斥）
MUTEX_NAME = 'RandomNamePicker.Helper.SingleInstance'
# 主程序的 Mutex 名，必须与 app.py 里的 MUTEX_NAME 完全一致
MAIN_MUTEX_NAME = 'RandomNamePicker.Main.SingleInstance'

# ---------------------------------------------------------------------------
# 下载相关配置
# ---------------------------------------------------------------------------

# GitHub 加速代理前缀（直连失败时按顺序回退尝试）
# 用法：代理前缀 + 原始 GitHub URL
GITHUB_PROXIES = [
    'https://gh-proxy.com/',
    'https://ghfast.top/',
    'https://gh-proxy.org/',
]

# 超过该大小才启用多线程分块下载
MULTI_THREAD_THRESHOLD = 5 * 1024 * 1024      # 5 MB
# 每个线程负责的分片大小（线程数 = 文件大小 / 分片大小）
CHUNK_SIZE = 2 * 1024 * 1024                  # 2 MB
# 线程数上下限
MIN_THREADS = 2
MAX_THREADS = 16
# 动态调整：连续多少次分片失败后减少线程
THREAD_SHRINK_AFTER_FAILS = 2
# 单次请求超时（秒）
DOWNLOAD_TIMEOUT = 60
# 分片下载失败重试次数
CHUNK_RETRY = 3
# 速度看门狗：连续多少秒无新增字节则判定为"卡死"，换下一个下载源
STALL_TIMEOUT = 20
# 速度看门狗：低于该速度（字节/秒）持续 STALL_TIMEOUT 秒则判定为"过慢"
MIN_SPEED = 30 * 1024

# ---------------------------------------------------------------------------
# 延时批处理替换配置
# ---------------------------------------------------------------------------

# 延时替换清单文件名（记录哪些文件需要退出后替换）
REPLACE_MANIFEST = '_replace_manifest.txt'
# 延时替换报告文件名
REPLACE_REPORT = '_replace_report.txt'
# 延时替换日志文件名
REPLACE_LOG = '_replace.log'
# 延时替换批处理文件名
REPLACE_BAT = '_replace.bat'
# 新版本暂存目录（解压到这里，退出后由批处理搬到目标位置）
STAGING_DIR = '_staging'

# 每批处理的文件数（批次大小）
REPLACE_BATCH_SIZE = 8
# 批次之间的间隔秒数（给系统释放句柄留时间）
REPLACE_BATCH_INTERVAL = 2
# 单个文件替换的最大重试次数
REPLACE_MAX_RETRY = 5
# 单次重试的等待秒数
REPLACE_RETRY_WAIT = 1
# 启动前等待 Helper 进程退出的秒数
REPLACE_INITIAL_WAIT = 3

# 替换规则：按顺序匹配，第一条命中的规则决定该文件的处理方式
#   pattern : 正则表达式（匹配相对路径，大小写不敏感）
#   mode    : 'replace' 延时替换 / 'skip' 跳过不处理 / 'delete' 删除
#   desc    : 规则说明（写入日志）
REPLACE_RULES = [
    # 自身可执行文件：必须延时替换（运行中无法覆盖）
    {'pattern': r'^helper\.exe$', 'mode': 'replace',
     'desc': 'Helper 自身，退出后替换'},
    # 主程序：退出后替换
    {'pattern': r'^RandomNamePicker\.exe$', 'mode': 'replace',
     'desc': '主程序，退出后替换'},
    # 动态库：最容易被进程锁定，必须延时替换
    {'pattern': r'\.(dll|pyd|so|dylib)$', 'mode': 'replace',
     'desc': '动态库，易被占用'},
    # 用户数据：绝不覆盖
    {'pattern': r'^data([\\/].*)?$', 'mode': 'skip',
     'desc': '用户数据目录，保留'},
    # 日志与备份：不处理
    {'pattern': r'^(helper\.log|app\.log|_backup([\\/].*)?)$', 'mode': 'skip',
     'desc': '日志/备份，保留'},
    # 临时文件：清理掉
    # 注意：用 re.match 时模式从字符串开头匹配，故需 .* 前缀
    {'pattern': r'.*\.(tmp|temp|bak|old)$', 'mode': 'delete',
     'desc': '临时文件，删除'},
    # 其余文件：默认延时替换
    {'pattern': r'.*', 'mode': 'replace',
     'desc': '默认规则'},
]

# 配色
BG = '#2b2b3d'
FG = '#ffffff'
ACCENT = '#667eea'
MUTED = '#9a9ab0'


def log(msg, level='INFO'):
    """写入日志文件便于排查。

    格式：[时间] [级别] [线程] 消息
    level: INFO / WARN / ERROR / DEBUG
    """
    try:
        import time as _t
        ts = _t.strftime('%Y-%m-%d %H:%M:%S')
        ms = int((_t.time() % 1) * 1000)
        thread_name = threading.current_thread().name
        line = f'[{ts}.{ms:03d}] [{level:<5}] [{thread_name}] {msg}\n'
        with open(os.path.join(BASE_DIR, 'helper.log'), 'a', encoding='utf-8') as f:
            f.write(line)
    except OSError:
        pass


def log_exc(msg):
    """记录异常及其完整堆栈，便于定位问题。"""
    import traceback
    log(f'{msg}\n{traceback.format_exc()}', level='ERROR')


# ---------------------------------------------------------------------------
# 替换规则匹配引擎
# ---------------------------------------------------------------------------

def match_replace_rule(rel_path):
    """按 REPLACE_RULES 顺序匹配相对路径，返回命中的规则字典。

    支持正则表达式匹配（大小写不敏感）。未命中任何规则时返回默认 replace。
    """
    import re
    normalized = rel_path.replace('/', '\\')
    for rule in REPLACE_RULES:
        try:
            if re.match(rule['pattern'], normalized, re.IGNORECASE):
                return rule
        except re.error as e:
            log(f'规则正则无效，跳过：{rule.get("pattern")}（{e}）', level='WARN')
    return {'pattern': '.*', 'mode': 'replace', 'desc': '兜底默认'}


def classify_files(rel_paths):
    """把文件列表按规则分类。

    返回 (to_replace, to_skip, to_delete)，均为 [(相对路径, 规则说明)] 列表。
    """
    to_replace, to_skip, to_delete = [], [], []
    for rel in rel_paths:
        rule = match_replace_rule(rel)
        mode = rule.get('mode', 'replace')
        item = (rel, rule.get('desc', ''))
        if mode == 'skip':
            to_skip.append(item)
        elif mode == 'delete':
            to_delete.append(item)
        else:
            to_replace.append(item)
    return to_replace, to_skip, to_delete


def log_env():
    """记录运行环境信息，便于排查平台相关问题。"""
    log('-' * 60)
    log(f'Helper 启动 | 版本={LOCAL_VERSION} | PID={os.getpid()}')
    log(f'Python={sys.version.split()[0]} | 平台={sys.platform} | frozen={getattr(sys, "frozen", False)}')
    log(f'BASE_DIR={BASE_DIR}')
    log(f'可执行文件={sys.executable}')
    log(f'自身文件名={SELF_NAME}')
    log(f'主程序路径={MAIN_EXE}（存在={os.path.exists(MAIN_EXE)}）')
    log(f'工作目录={os.getcwd()}')
    log(f'Mutex 名：helper={MUTEX_NAME} | main={MAIN_MUTEX_NAME}')
    log('-' * 60)


# ---------------------------------------------------------------------------
# 单实例互斥：命名 Mutex
# ---------------------------------------------------------------------------

class SingleInstance:
    """基于命名 Mutex 的跨进程单实例锁。

    Windows 使用 CreateMutexW + GetLastError 判定；其他系统用文件锁模拟，
    保证不同平台行为一致。获取失败即表示已有实例在运行。
    """

    def __init__(self, name):
        self.name = name
        self._handle = None
        self._lock_file = None
        self.acquired = False

    def acquire(self):
        """尝试获取所有权。成功返回 True，已有实例返回 False。"""
        log(f'尝试获取 Mutex：{self.name}', level='DEBUG')
        try:
            if os.name == 'nt':
                return self._acquire_windows()
            return self._acquire_posix()
        except Exception as e:
            # 出错时保守放行，避免因锁机制本身故障导致程序无法启动
            log_exc(f'Mutex 获取异常，放行启动：{e}')
            self.acquired = False
            return True

    def _acquire_windows(self):
        import ctypes
        from ctypes import wintypes

        ERROR_ALREADY_EXISTS = 183
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL,
                                          wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE

        handle = kernel32.CreateMutexW(None, False, self.name)
        err = ctypes.get_last_error()
        if not handle:
            log(f'CreateMutexW 失败，错误码 {err}，放行启动', level='WARN')
            return True
        if err == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            log(f'检测到已有实例运行（Mutex={self.name}），退出', level='WARN')
            return False
        self._handle = handle
        self.acquired = True
        log(f'Mutex 获取成功：{self.name}（句柄={handle}）')
        return True

    def _acquire_posix(self):
        import fcntl
        import tempfile

        safe = self.name.replace('\\', '_').replace('/', '_')
        path = os.path.join(tempfile.gettempdir(), safe + '.lock')
        self._lock_file = open(path, 'a+')
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            self._lock_file.close()
            self._lock_file = None
            log(f'检测到已有实例运行（锁文件={path}）：{e}，退出', level='WARN')
            return False
        self.acquired = True
        log(f'Mutex 获取成功：{path}')
        return True

    def release(self):
        """释放 Mutex 资源，可重复调用。"""
        if self._handle is not None:
            try:
                import ctypes
                ctypes.WinDLL('kernel32', use_last_error=True).CloseHandle(
                    self._handle)
                log(f'Mutex 已释放：{self.name}（句柄={self._handle}）')
            except Exception as e:
                log_exc(f'Mutex 释放失败：{e}')
            finally:
                self._handle = None
        if self._lock_file is not None:
            try:
                import fcntl
                fcntl.flock(self._lock_file, fcntl.LOCK_UN)
                self._lock_file.close()
                log(f'Mutex 已释放：{self.name}')
            except Exception as e:
                log_exc(f'Mutex 释放失败：{e}')
            finally:
                self._lock_file = None
        self.acquired = False


_instance_lock = SingleInstance(MUTEX_NAME)


def github_get(path):
    url = f'https://api.github.com/repos/{GITHUB_REPO}{path}'
    log(f'GitHub API 请求：{url}', level='DEBUG')
    req = urllib.request.Request(url, headers={
        'Accept': 'application/vnd.github.v3+json',
        'User-Agent': 'RandomNamePicker-Helper',
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    log(f'GitHub API 响应：{url} -> HTTP {resp.status}', level='DEBUG')
    return data


def get_tag_commit_date(tag):
    try:
        ref = github_get(f'/git/refs/tags/{tag}')
        sha = ref['object']['sha']
        if ref['object']['type'] == 'tag':
            tag_obj = github_get(f'/git/tags/{sha}')
            sha = tag_obj['object']['sha']
        commit = github_get(f'/git/commits/{sha}')
        date = commit['committer']['date']
        log(f'tag {tag} 提交时间：{date}（sha={sha[:8]}）', level='DEBUG')
        return date
    except Exception as e:
        log_exc(f'获取 {tag} 时间失败：{e}')
        return None


def get_latest_release():
    try:
        release = github_get('/releases/latest')
        log(f'最新 release：tag={release.get("tag_name")} '
            f'资源数={len(release.get("assets", []))}', level='DEBUG')
        return release
    except Exception as e:
        log_exc(f'获取最新版本失败：{e}')
        return None


def _proxy_url(url, proxy):
    """把原始 GitHub URL 改写为代理 URL。"""
    return proxy + url


def _open_url(url, headers=None, timeout=DOWNLOAD_TIMEOUT):
    """发起 GET 请求，返回响应对象。

    同时设置 socket 级读超时，避免服务端"连上但不发数据"时永久阻塞。
    """
    hdrs = {'User-Agent': 'RandomNamePicker-Helper'}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    resp = urllib.request.urlopen(req, timeout=timeout)
    # 读超时设短一些，让卡死的连接尽快抛异常而不是干等
    try:
        resp.fp.raw._sock.settimeout(STALL_TIMEOUT)
    except Exception:
        pass
    return resp


def _probe(url, timeout=10):
    """探测 URL 是否可用，返回 (可用, 文件总大小, 是否支持 Range)。"""
    try:
        with _open_url(url, headers={'Range': 'bytes=0-0'}, timeout=timeout) as resp:
            # 206 表示支持 Range
            support_range = resp.status == 206
            if support_range:
                cr = resp.headers.get('Content-Range', '')
                total = int(cr.split('/')[-1]) if '/' in cr else 0
            else:
                total = int(resp.headers.get('Content-Length', 0))
            return True, total, support_range
    except Exception as e:
        log(f'探测失败 {url}：{e}', level='DEBUG')
        return False, 0, False


def _calc_threads(total_size):
    """按文件大小自适应计算线程数（参考 PCL2：分片驱动）。"""
    if total_size <= 0:
        return MIN_THREADS
    n = total_size // CHUNK_SIZE
    n = max(MIN_THREADS, min(MAX_THREADS, int(n)))
    return n


class _SpeedWatchdog:
    """速度看门狗：监控下载进度，长时间无进展或速度过低时判定为卡死。

    用于解决"连接成功但龟速"的场景——此时不会抛异常，只能靠速度判定。
    """

    def __init__(self, name):
        self.name = name
        self._last_bytes = 0
        self._last_time = time.time()
        self._lock = threading.Lock()
        self.stalled = False

    def update(self, total_bytes):
        """由下载线程汇报当前累计字节数。"""
        with self._lock:
            now = time.time()
            if total_bytes > self._last_bytes:
                self._last_bytes = total_bytes
                self._last_time = now

    def check(self):
        """检查是否卡死。返回 True 表示应中止当前下载源。"""
        with self._lock:
            idle = time.time() - self._last_time
            if idle >= STALL_TIMEOUT:
                self.stalled = True
                return True
            return False

    def start(self):
        """启动后台监控线程。"""
        def loop():
            while not self.stalled:
                time.sleep(1)
                if self.check():
                    log(f'下载源 {self.name} 连续 {STALL_TIMEOUT}s 无进展，'
                        f'判定为卡死，切换下一个源', level='WARN')
                    return
        t = threading.Thread(target=loop, daemon=True)
        t.start()
        return t


def _download_chunk(url, dest, start, end, progress_cb, lock, counter, watchdog):
    """下载 [start, end] 区间的分片并写入文件对应位置。

    返回实际写入字节数；失败抛异常。看门狗判定卡死时立即放弃，不重试。
    """
    headers = {'Range': f'bytes={start}-{end}'}
    last_err = None
    for attempt in range(CHUNK_RETRY):
        if watchdog.stalled:
            raise Exception('下载源卡死，中止')
        try:
            with _open_url(url, headers=headers) as resp:
                if resp.status not in (200, 206):
                    raise Exception(f'HTTP {resp.status}')
                dest.seek(start)
                written = 0
                while True:
                    if watchdog.stalled:
                        raise Exception('下载源卡死，中止')
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    dest.write(chunk)
                    written += len(chunk)
                    with lock:
                        counter[0] += len(chunk)
                        if progress_cb:
                            progress_cb(counter[0])
                    watchdog.update(counter[0])
                return written
        except Exception as e:
            last_err = e
            # 卡死或读超时：不再重试，直接放弃该源
            if watchdog.stalled or isinstance(e, (socket.timeout, TimeoutError)):
                watchdog.stalled = True
                raise Exception(f'下载源卡死：{last_err}')
            log(f'分片 {start}-{end} 第 {attempt + 1} 次失败：{e}', level='WARN')
    raise Exception(f'分片 {start}-{end} 下载失败：{last_err}')


def _download_multi(url, dest, total, progress_cb, watchdog):
    """多线程分块下载，线程数自适应并动态调整。"""
    threads_n = _calc_threads(total)
    log(f'启用多线程下载：{threads_n} 线程，分片 {CHUNK_SIZE // 1024 // 1024} MB，'
        f'总大小 {total / 1024 / 1024:.2f} MB')

    # 预分配文件空间，避免多线程写入时反复扩展
    with open(dest, 'wb') as f:
        f.truncate(total)

    lock = threading.Lock()
    counter = [0]
    fail_streak = [0]
    active = [threads_n]          # 当前允许的并发数（动态调整用）
    sem = threading.Semaphore(threads_n)
    errors = []

    # 切分任务
    tasks = []
    pos = 0
    while pos < total:
        end = min(pos + CHUNK_SIZE - 1, total - 1)
        tasks.append((pos, end))
        pos = end + 1

    def worker(start, end):
        if errors:
            return
        with sem:
            if errors:
                return
            try:
                with open(dest, 'r+b') as f:
                    _download_chunk(url, f, start, end, progress_cb,
                                    lock, counter, watchdog)
                with lock:
                    fail_streak[0] = 0
            except Exception as e:
                with lock:
                    fail_streak[0] += 1
                    # 动态调整：连续失败则降低并发，减轻服务器压力
                    if (fail_streak[0] >= THREAD_SHRINK_AFTER_FAILS
                            and active[0] > MIN_THREADS):
                        active[0] = max(MIN_THREADS, active[0] // 2)
                        log(f'连续失败 {fail_streak[0]} 次，'
                            f'并发降至 {active[0]}', level='WARN')
                    errors.append(e)

    ts = []
    for start, end in tasks:
        if errors:
            break
        t = threading.Thread(target=worker, args=(start, end), daemon=True)
        t.start()
        ts.append(t)
        # 动态调整：按当前允许的并发数节流
        while sum(1 for x in ts if x.is_alive()) >= active[0]:
            if errors:
                break
            time.sleep(0.05)

    for t in ts:
        t.join()

    if errors:
        raise errors[0]

    log(f'多线程下载完成：{counter[0]} 字节')


def _download_single(url, dest, total, progress_cb, watchdog):
    """单线程流式下载（小文件或服务端不支持 Range 时使用）。"""
    log('使用单线程下载')
    done = 0
    with _open_url(url) as resp:
        with open(dest, 'wb') as f:
            while True:
                if watchdog.stalled:
                    raise Exception('下载源卡死，中止')
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if progress_cb:
                    progress_cb(done)
                watchdog.update(done)
    log(f'单线程下载完成：{done} 字节')


def download_file(url, dest, progress_cb=None):
    """下载文件：直连优先，失败回退代理；大文件多线程分块。

    progress_cb 接收"已下载字节数"（不是比例），由调用方换算。
    每个源都有速度看门狗，龟速或卡死会自动切换下一个源。
    """
    log(f'开始下载：{url}')
    log(f'保存到：{dest}')

    # 候选源：直连 + 各代理
    candidates = [('直连', url)]
    for p in GITHUB_PROXIES:
        candidates.append((p, _proxy_url(url, p)))

    last_err = None
    for name, src in candidates:
        log(f'尝试下载源：{name} -> {src}')
        ok, total, support_range = _probe(src)
        if not ok:
            log(f'下载源不可用：{name}', level='WARN')
            continue

        log(f'下载源可用：{name} | 大小={total} 字节 '
            f'({total / 1024 / 1024:.2f} MB) | 支持 Range={support_range}')

        watchdog = _SpeedWatchdog(name)
        watchdog.start()
        t0 = time.time()
        try:
            if total >= MULTI_THREAD_THRESHOLD and support_range:
                _download_multi(src, dest, total, progress_cb, watchdog)
            else:
                if total >= MULTI_THREAD_THRESHOLD:
                    log('服务端不支持 Range，降级为单线程', level='WARN')
                _download_single(src, dest, total, progress_cb, watchdog)

            size = os.path.getsize(dest)
            if total and size != total:
                raise Exception(f'大小不符：期望 {total}，实际 {size}')

            elapsed = time.time() - t0
            speed = size / elapsed / 1024 if elapsed > 0 else 0
            log(f'下载成功（源={name}，{size} 字节，耗时 {elapsed:.2f}s，'
                f'平均 {speed:.1f} KB/s）')
            return
        except Exception as e:
            last_err = e
            log_exc(f'下载源 {name} 失败：{e}')
            if os.path.exists(dest):
                try:
                    os.remove(dest)
                except OSError:
                    pass

    raise Exception(f'所有下载源均失败：{last_err}')


# ---------------------------------------------------------------------------
# 更新核心：备份 -> 清理 -> 解压
# ---------------------------------------------------------------------------

def _abs(p):
    return os.path.abspath(p)


def _is_running_self(path):
    return _abs(path).lower() == _abs(os.path.join(BASE_DIR, SELF_NAME)).lower()


def backup_current(backup_root, exclude=None):
    """把 BASE_DIR 下所有内容备份到 backup_root（排除 _backup 自身）。

    exclude: 额外排除的文件/目录名集合（如正在下载的 app.zip）。
    被占用的文件跳过并记录，不中止整体流程。
    返回被跳过的条目列表。
    """
    os.makedirs(backup_root, exist_ok=True)
    exclude = exclude or set()
    items = [i for i in os.listdir(BASE_DIR)
             if i != BACKUP_DIR_NAME and i not in exclude]
    log(f'开始备份 {len(items)} 项到 {backup_root}', level='DEBUG')
    skipped = []
    for item in items:
        src = os.path.join(BASE_DIR, item)
        dst = os.path.join(backup_root, item)
        try:
            if os.path.isdir(src) and not os.path.islink(src):
                shutil.copytree(src, dst, symlinks=True)
                log(f'  备份目录：{item}', level='DEBUG')
            else:
                shutil.copy2(src, dst, follow_symlinks=False)
                log(f'  备份文件：{item}（{os.path.getsize(src)} 字节）', level='DEBUG')
        except OSError as e:
            # 被占用（WinError 32/33）等：跳过，不中止
            skipped.append(item)
            log(f'备份 {item} 失败（跳过）：{e}', level='WARN')

    if skipped:
        log(f'备份完成，{len(skipped)} 项被占用跳过：{skipped}', level='WARN')
    return skipped


def clean_dir(keep=None):
    """删除 BASE_DIR 下除 KEEP_ITEMS、_backup、helper.log 外的所有内容。

    keep: 额外保留的文件/目录名集合（如正在使用的更新包 app.zip）。
    遇到被占用的文件跳过并记录，不中止整体流程。
    """
    keep = keep or set()
    skipped = []
    kept = []
    removed = []
    for item in os.listdir(BASE_DIR):
        if item.lower() in KEEP_ITEMS:      # 大小写不敏感匹配
            kept.append(item)
            continue
        if item in (BACKUP_DIR_NAME, 'helper.log') or item in keep:
            kept.append(item)
            continue
        path = os.path.join(BASE_DIR, item)
        if _is_running_self(path):
            skipped.append(item)
            log(f'跳过正在运行的自己：{item}', level='WARN')
            continue
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            removed.append(item)
            log(f'  已删除：{item}', level='DEBUG')
        except OSError as e:
            skipped.append(item)
            log(f'删除 {item} 失败（跳过）：{e}', level='WARN')

    log(f'清理结果：删除 {len(removed)} 项，保留 {len(kept)} 项，跳过 {len(skipped)} 项')
    if kept:
        log(f'  保留：{kept}', level='DEBUG')
    if skipped:
        log(f'  跳过（被占用）：{skipped}', level='WARN')
    return skipped


def safe_extract(zip_path, target_dir):
    """安全解压，防 Zip Slip。

    所有文件先解压到暂存目录 _staging，再按替换规则分类：
      - replace：写入延时替换清单，退出后由批处理替换
      - skip   ：不处理（用户数据等）
      - delete ：直接删除暂存文件

    这样即使目标文件被占用，也不影响解压本身，替换交给退出后的批处理。

    返回 (替换清单, 跳过清单, 删除清单)，均为 [(相对路径, 规则说明)]。
    """
    abs_target = _abs(target_dir)
    staging = os.path.join(BASE_DIR, STAGING_DIR)
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging, exist_ok=True)

    with zipfile.ZipFile(zip_path, 'r') as zf:
        members = zf.namelist()
        log(f'开始解压 {len(members)} 个条目到暂存目录', level='DEBUG')

        # 防 Zip Slip
        for member in members:
            member_path = _abs(os.path.join(abs_target, member))
            if member_path != abs_target and not member_path.startswith(abs_target + os.sep):
                log(f'检测到非法压缩路径：{member}', level='ERROR')
                raise Exception(f'非法压缩路径：{member}')

        # 解压到暂存目录（不碰目标文件，避免占用问题）
        extracted = []
        for info in zf.infolist():
            if info.is_dir():
                continue
            try:
                zf.extract(info, staging)
                extracted.append(info.filename.replace('/', os.sep))
            except OSError as e:
                log(f'解压 {info.filename} 到暂存失败：{e}', level='WARN')

    log(f'暂存解压完成：{len(extracted)} 个文件', level='DEBUG')

    # 按规则分类
    to_replace, to_skip, to_delete = classify_files(extracted)
    log(f'规则分类：替换 {len(to_replace)} | 跳过 {len(to_skip)} | 删除 {len(to_delete)}')

    # 删除类：直接删掉暂存文件
    for rel, desc in to_delete:
        try:
            os.remove(os.path.join(staging, rel))
            log(f'  删除暂存文件：{rel}（{desc}）', level='DEBUG')
        except OSError as e:
            log(f'  删除暂存文件 {rel} 失败：{e}', level='WARN')

    # 写入替换清单（供退出后的批处理读取）
    manifest_path = os.path.join(BASE_DIR, REPLACE_MANIFEST)
    try:
        with open(manifest_path, 'w', encoding='utf-8') as f:
            for rel, desc in to_replace:
                f.write(f'{rel}\t{desc}\n')
        log(f'已写入替换清单：{manifest_path}（{len(to_replace)} 项）')
    except OSError as e:
        log_exc(f'写入替换清单失败：{e}')

    return to_replace, to_skip, to_delete


def rollback(backup_path):
    """从备份回滚：清空（跳过 _backup 和正在运行的自己）后还原。

    被占用的文件跳过并记录，不中止。
    """
    log(f'开始回滚，来源：{backup_path}', level='WARN')
    if not os.path.isdir(backup_path):
        log(f'备份目录不存在，无法回滚：{backup_path}', level='ERROR')
        return

    # 1. 清空（尽力而为）
    cleared = 0
    for item in os.listdir(BASE_DIR):
        if item == BACKUP_DIR_NAME:
            continue
        path = os.path.join(BASE_DIR, item)
        if _is_running_self(path):
            log(f'  跳过正在运行的自己：{item}', level='DEBUG')
            continue
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            cleared += 1
        except OSError as e:
            log(f'回滚清理 {item} 失败（跳过）：{e}', level='WARN')
    log(f'  回滚清空完成：{cleared} 项', level='DEBUG')

    # 2. 还原（尽力而为）
    restored = 0
    failed = 0
    for item in os.listdir(backup_path):
        src = os.path.join(backup_path, item)
        dst = os.path.join(BASE_DIR, item)
        if _is_running_self(dst):
            log(f'  跳过正在运行的自己：{item}', level='DEBUG')
            continue
        try:
            if os.path.isdir(src) and not os.path.islink(src):
                # dirs_exist_ok=True：目标可能已存在（清空时被占用的没删掉）
                shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst, follow_symlinks=False)
            restored += 1
        except OSError as e:
            failed += 1
            log(f'回滚还原 {item} 失败（跳过）：{e}', level='WARN')

    if failed:
        log(f'回滚完成：还原 {restored} 项，失败 {failed} 项', level='WARN')
    else:
        log(f'回滚完成：还原 {restored} 项')


def apply_update(zip_path):
    """备份 -> 清理 -> 解压。返回 (ok: bool, message: str)"""
    import time as _t
    backup_root = os.path.join(BASE_DIR, BACKUP_DIR_NAME)
    t0 = _t.time()
    log(f'===== 开始应用更新 =====')
    log(f'更新包：{zip_path}（{os.path.getsize(zip_path)} 字节）')
    log(f'备份目录：{backup_root}')

    # 清掉可能残留的旧备份（例如上次失败后遗留）
    if os.path.isdir(backup_root):
        log('发现残留旧备份，先清除', level='WARN')
    shutil.rmtree(backup_root, ignore_errors=True)

    # 1. 备份（排除正在使用的更新包，避免把 26MB 的 zip 也备份一份）
    t = _t.time()
    try:
        backup_current(backup_root, exclude={os.path.basename(zip_path)})
        log(f'[1/3] 备份完成，耗时 {_t.time() - t:.2f}s')
    except Exception as e:
        log_exc(f'[1/3] 备份失败：{e}')
        shutil.rmtree(backup_root, ignore_errors=True)
        return False, '备份失败'

    # 2. 清理（除 KEEP_ITEMS 外全删；必须保留更新包本身，否则第 3 步无包可解）
    t = _t.time()
    zip_name = os.path.basename(zip_path)
    try:
        skipped = clean_dir(keep={zip_name})
        log(f'[2/3] 清理完成，耗时 {_t.time() - t:.2f}s'
            + (f'，{len(skipped)} 项被占用跳过' if skipped else ''))
    except Exception as e:
        log_exc(f'[2/3] 清理失败：{e}，回滚中')
        rollback(backup_root)
        return False, '清理失败，已回滚'

    # 3. 解压到暂存目录并按规则分类（不直接覆盖目标，避免占用问题）
    t = _t.time()
    if not os.path.exists(zip_path):
        log(f'更新包丢失，无法解压：{zip_path}', level='ERROR')
        rollback(backup_root)
        return False, '更新包丢失，已回滚'
    try:
        to_replace, to_skip, to_delete = safe_extract(zip_path, BASE_DIR)
        log(f'[3/3] 暂存解压完成，耗时 {_t.time() - t:.2f}s | '
            f'待替换 {len(to_replace)} | 跳过 {len(to_skip)} | 删除 {len(to_delete)}')
    except Exception as e:
        log_exc(f'[3/3] 解压失败：{e}，回滚中')
        rollback(backup_root)
        return False, '解压失败，已回滚'

    log(f'===== 更新应用成功，总耗时 {_t.time() - t0:.2f}s =====')
    log(f'待替换文件将在 Helper 退出后由批处理分批完成')
    return True, f'成功（{len(to_replace)} 项待退出后替换）'


# ---------------------------------------------------------------------------
# 退出前调度批处理：分批延时替换 + 清理备份
# ---------------------------------------------------------------------------

def _read_manifest():
    """读取替换清单，返回 [(相对路径, 规则说明)]。"""
    manifest_path = os.path.join(BASE_DIR, REPLACE_MANIFEST)
    if not os.path.exists(manifest_path):
        return []
    items = []
    try:
        with open(manifest_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.rstrip('\n')
                if not line:
                    continue
                parts = line.split('\t', 1)
                rel = parts[0]
                desc = parts[1] if len(parts) > 1 else ''
                items.append((rel, desc))
    except OSError as e:
        log(f'读取替换清单失败：{e}', level='WARN')
    return items


def _bat_escape(s):
    """转义批处理中的特殊字符。"""
    return s.replace('^', '^^').replace('%', '%%').replace('&', '^&') \
            .replace('|', '^|').replace('<', '^<').replace('>', '^>')


def _gen_replace_batch(items, delete_backup, backup_root):
    """生成分批替换批处理脚本。

    设计要点：
      - 每批处理 REPLACE_BATCH_SIZE 个文件，批间等待 REPLACE_BATCH_INTERVAL 秒
      - 每个文件独立重试 REPLACE_MAX_RETRY 次，失败只记录不中断
      - 全程写入日志文件，最后生成汇总报告
    """
    staging = os.path.join(BASE_DIR, STAGING_DIR)
    log_path = os.path.join(BASE_DIR, REPLACE_LOG)
    report_path = os.path.join(BASE_DIR, REPLACE_REPORT)

    L = [
        '@echo off',
        # 批处理文件以 GBK 写入，故用 936 代码页，避免中文乱码
        'chcp 936 >nul',
        'setlocal enabledelayedexpansion',
        f'set "BASE={_bat_escape(BASE_DIR)}"',
        f'set "STAGING={_bat_escape(staging)}"',
        f'set "LOG={_bat_escape(log_path)}"',
        f'set "REPORT={_bat_escape(report_path)}"',
        'set /a OK=0',
        'set /a FAIL=0',
        'set /a BATCHNO=0',
        '',
        'echo [%date% %time%] ===== 延时替换开始 ===== > "%LOG%"',
        f'echo 批次大小={REPLACE_BATCH_SIZE} 批间隔={REPLACE_BATCH_INTERVAL}s '
        f'重试={REPLACE_MAX_RETRY} >> "%LOG%"',
        '',
        # 等 Helper 进程完全退出
        f'ping 127.0.0.1 -n {REPLACE_INITIAL_WAIT + 1} >nul',
        'echo [%date% %time%] Helper 已退出，开始替换 >> "%LOG%"',
        '',
    ]

    # 按批次切分
    batches = [items[i:i + REPLACE_BATCH_SIZE]
               for i in range(0, len(items), REPLACE_BATCH_SIZE)]

    for bi, batch in enumerate(batches, 1):
        L.append(f':: ---------- 批次 {bi}/{len(batches)} ----------')
        L.append(f'set /a BATCHNO={bi}')
        L.append(f'echo [%date% %time%] --- 批次 {bi}/{len(batches)} '
                 f'（{len(batch)} 个文件）--- >> "%LOG%"')

        for rel, desc in batch:
            src = os.path.join(staging, rel)
            dst = os.path.join(BASE_DIR, rel)
            safe_rel = _bat_escape(rel)
            safe_desc = _bat_escape(desc)
            L += [
                f':: 替换 {safe_rel}（{safe_desc}）',
                f'if not exist "{_bat_escape(src)}" (',
                f'  echo [%date% %time%] [SKIP] {safe_rel} 暂存文件不存在 >> "%LOG%"',
                f'  set /a FAIL+=1',
                f'  goto :next_{bi}_{len(L)}',
                ')',
                f'set /a TRY=0',
                f':retry_{bi}_{len(L)}',
                f'set /a TRY+=1',
                # 确保目标目录存在
                f'for %%D in ("{_bat_escape(dst)}") do if not exist "%%~dpD" '
                f'mkdir "%%~dpD" >nul 2>&1',
                # 先删旧文件，再搬新文件
                f'del /f /q "{_bat_escape(dst)}" >nul 2>&1',
                f'move /y "{_bat_escape(src)}" "{_bat_escape(dst)}" >nul 2>&1',
                f'if exist "{_bat_escape(dst)}" (',
                f'  echo [%date% %time%] [OK] {safe_rel} >> "%LOG%"',
                f'  set /a OK+=1',
                f'  goto :next_{bi}_{len(L)}',
                ')',
                f'if !TRY! GEQ {REPLACE_MAX_RETRY} (',
                f'  echo [%date% %time%] [FAIL] {safe_rel} 重试 !TRY! 次仍失败 >> "%LOG%"',
                f'  set /a FAIL+=1',
                f'  goto :next_{bi}_{len(L)}',
                ')',
                f'ping 127.0.0.1 -n {REPLACE_RETRY_WAIT + 1} >nul',
                f'goto :retry_{bi}_{len(L)}',
                f':next_{bi}_{len(L)}',
            ]

        # 批次间隔
        if bi < len(batches):
            L += [
                f'echo [%date% %time%] 批次 {bi} 完成，等待 '
                f'{REPLACE_BATCH_INTERVAL}s >> "%LOG%"',
                f'ping 127.0.0.1 -n {REPLACE_BATCH_INTERVAL + 1} >nul',
            ]
        L.append('')

    # 清理暂存目录
    L += [
        ':: ---------- 清理暂存 ----------',
        f'if exist "%STAGING%" rmdir /s /q "%STAGING%" >nul 2>&1',
        f'if exist "%STAGING%" (',
        f'  echo [%date% %time%] [WARN] 暂存目录未能删除 >> "%LOG%"',
        ') else (',
        f'  echo [%date% %time%] 暂存目录已清理 >> "%LOG%"',
        ')',
        '',
    ]

    # 删除备份
    if delete_backup:
        L += [
            ':: ---------- 删除备份 ----------',
            f'if exist "{_bat_escape(backup_root)}" (',
            f'  rmdir /s /q "{_bat_escape(backup_root)}" >nul 2>&1',
            ')',
            f'if exist "{_bat_escape(backup_root)}" (',
            f'  echo [%date% %time%] [WARN] 备份目录未能删除 >> "%LOG%"',
            ') else (',
            f'  echo [%date% %time%] 备份目录已清理 >> "%LOG%"',
            ')',
            '',
        ]

    # 生成报告
    L += [
        ':: ---------- 生成报告 ----------',
        f'echo [%date% %time%] ===== 延时替换结束 ===== >> "%LOG%"',
        f'echo 成功=!OK! 失败=!FAIL! 批次=!BATCHNO! >> "%LOG%"',
        '',
        f'echo ============================================ > "%REPORT%"',
        f'echo 延时替换报告 >> "%REPORT%"',
        f'echo 时间: %date% %time% >> "%REPORT%"',
        f'echo ============================================ >> "%REPORT%"',
        f'echo 总文件数: {len(items)} >> "%REPORT%"',
        f'echo 成功替换: !OK! >> "%REPORT%"',
        f'echo 失败: !FAIL! >> "%REPORT%"',
        f'echo 批次数: !BATCHNO! >> "%REPORT%"',
        f'echo. >> "%REPORT%"',
        f'echo 详细日志见: {_bat_escape(REPLACE_LOG)} >> "%REPORT%"',
        '',
        'del "%~f0"',
    ]

    return L


def schedule_cleanup(delete_backup, target_exe):
    """生成延时替换批处理并启动，在 Helper 退出后执行分批替换。

    delete_backup: 是否删除 _backup（只有更新成功才为 True）
    target_exe:    当前 Helper 可执行文件路径（保留参数以兼容调用方）
    """
    backup_root = os.path.join(BASE_DIR, BACKUP_DIR_NAME)
    items = _read_manifest()
    has_backup = delete_backup and os.path.isdir(backup_root)

    log(f'延时替换检查：待替换={len(items)} 项 | 删备份={has_backup}')

    if not items and not has_backup:
        log('无需调度延时替换', level='DEBUG')
        return

    bat_path = os.path.join(BASE_DIR, REPLACE_BAT)
    lines = _gen_replace_batch(items, has_backup, backup_root)

    try:
        with open(bat_path, 'w', encoding='gbk') as f:
            f.write('\r\n'.join(lines) + '\r\n')
        log(f'已写入延时替换批处理：{bat_path}（{len(lines)} 行）', level='DEBUG')
        proc = subprocess.Popen(
            ['cmd', '/c', bat_path],
            cwd=BASE_DIR,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        batches = (len(items) + REPLACE_BATCH_SIZE - 1) // REPLACE_BATCH_SIZE
        log(f'已调度延时替换（PID={proc.pid}，{len(items)} 个文件，'
            f'{batches} 个批次，删备份={has_backup}）')
    except Exception as e:
        log_exc(f'调度延时替换失败：{e}')


def is_main_running():
    """探测主程序是否在运行：尝试拿主程序的 Mutex。

    - 拿不到  -> 主程序持有锁 -> 在运行
    - 拿到了  -> 主程序没跑   -> 立即释放
    """
    probe = SingleInstance(MAIN_MUTEX_NAME)
    if probe.acquire():
        probe.release()
        log('探测结果：主程序未运行', level='DEBUG')
        return False
    log('探测结果：主程序正在运行', level='DEBUG')
    return True


def kill_main(wait=5):
    """taskkill 主程序并轮询确认退出。"""
    import time as _t
    exe_name = os.path.basename(MAIN_EXE)
    log(f'尝试终止主程序：{exe_name}（最长等待 {wait}s）')
    try:
        result = subprocess.run(
            ['taskkill', '/F', '/IM', exe_name],
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=wait,
        )
        output = result.stdout.decode('gbk', errors='replace').strip()
        log(f'taskkill 返回码={result.returncode} | 输出：{output}', level='DEBUG')
    except Exception as e:
        log_exc(f'终止主程序失败：{e}')
        return False

    for i in range(int(wait * 4)):
        if not is_main_running():
            log(f'主程序已确认退出（轮询 {i + 1} 次，'
                f'耗时约 {(i + 1) * 0.25:.2f}s）')
            return True
        _t.sleep(0.25)

    still = is_main_running()
    if still:
        log(f'等待 {wait}s 后主程序仍在运行', level='ERROR')
    else:
        log('主程序已退出')
    return not still


def launch_main():
    if os.path.exists(MAIN_EXE):
        log(f'启动主程序：{MAIN_EXE}')
        proc = subprocess.Popen([MAIN_EXE], cwd=BASE_DIR)
        log(f'主程序已启动（PID={proc.pid}）')
        return True
    log(f'主程序不存在：{MAIN_EXE}', level='ERROR')
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
        import time as _t
        t0 = _t.time()
        log('=' * 60)
        log(f'开始检查更新 | 本地版本={LOCAL_VERSION}')

        release = get_latest_release()
        if not release:
            log('无法连接 GitHub，跳过更新', level='WARN')
            self.root.after(0, lambda: self.set_status('无法连接 GitHub', '#ffb86c'))
            self.root.after(0, self.enable_start)
            return

        remote_tag = release.get('tag_name', '')
        log(f'远端最新版本：{remote_tag}')

        local_date = get_tag_commit_date(LOCAL_VERSION)
        remote_date = get_tag_commit_date(remote_tag) if remote_tag else None
        log(f'版本时间对比：本地={local_date} | 远端={remote_date}')

        if local_date and remote_date and remote_date <= local_date:
            log('已是最新版本，无需更新')
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
            names = [a['name'] for a in release.get('assets', [])]
            log(f'未找到更新包（app.zip），现有资源：{names}', level='WARN')
            self.root.after(0, lambda: self.set_status('未找到更新包', '#ffb86c'))
            self.root.after(0, self.enable_start)
            return

        log(f'选定更新包：{zip_asset["name"]}（{zip_asset.get("size", 0)} 字节）')

        # 更新前先确保主程序已退出，否则主程序占用的 DLL 会导致清理失败
        if is_main_running():
            self.root.after(0, lambda: self.set_status('正在关闭主程序...'))
            log('主程序在运行，尝试终止', level='WARN')
            if not kill_main():
                log('无法关闭主程序，放弃更新', level='ERROR')
                self.root.after(0, lambda: self.set_status(
                    '请先关闭主程序后重试', '#ffb86c'))
                self.root.after(0, self.enable_start)
                return
            log('主程序已退出')
        else:
            log('主程序未运行，直接更新')

        # 说明：不再阻塞等待 DLL 释放。新版本先解压到 _staging，
        # 被占用的文件由退出后的 _replace.bat 分批替换，无需在此等待。

        self.root.after(0, lambda: self.set_status(f'发现新版本 {remote_tag}，下载中...'))
        zip_path = os.path.join(BASE_DIR, 'app.zip')
        t = _t.time()
        # 进度回调收到的是"已下载字节数"，这里换算成比例
        total_size = [zip_asset.get('size', 0)]

        def on_progress(done):
            if total_size[0]:
                self.root.after(0, lambda: self.set_progress(done / total_size[0]))

        try:
            download_file(zip_asset['browser_download_url'], zip_path, on_progress)
            log(f'下载完成，耗时 {_t.time() - t:.2f}s')
        except Exception as e:
            log_exc(f'下载失败：{e}')
            self.root.after(0, lambda: self.set_status('下载失败', '#ff7b72'))
            self.root.after(0, self.enable_start)
            return

        self.root.after(0, lambda: self.set_status('正在应用更新...'))
        ok, msg = apply_update(zip_path)

        if os.path.exists(zip_path):
            try:
                os.remove(zip_path)
                log(f'已删除更新包：{zip_path}', level='DEBUG')
            except OSError as e:
                log(f'删除更新包失败：{e}', level='WARN')

        if ok:
            self.update_success = True
            log(f'更新成功！版本：{remote_tag} | 总耗时 {_t.time() - t0:.2f}s')
            self.root.after(0, lambda: self.set_status(
                f'更新完成（{remote_tag}）', '#7ee787'))
        else:
            log(f'更新失败：{msg} | 总耗时 {_t.time() - t0:.2f}s', level='ERROR')
            self.root.after(0, lambda: self.set_status(msg, '#ffb86c'))

        self.root.after(0, self.enable_start)

    def enable_start(self):
        self.btn.config(text='启动中...')
        self.root.after(800, self.auto_launch)

    def auto_launch(self):
        log('准备启动主程序')
        # 先调度延时替换批处理（分批替换暂存文件；成功时删 _backup）
        schedule_cleanup(
            delete_backup=self.update_success,
            target_exe=os.path.join(BASE_DIR, SELF_NAME),
        )
        launch_main()
        log('Helper 即将退出')
        self.root.destroy()


def main():
    log_env()
    # 单实例检查：已有实例在运行则立即退出
    if not _instance_lock.acquire():
        log('已有 Helper 实例在运行，本次启动退出', level='WARN')
        sys.exit(0)

    # 正常退出 / 异常终止时释放 Mutex，避免残留死锁
    atexit.register(_instance_lock.release)

    try:
        root = tk.Tk()
        HelperApp(root)
        root.mainloop()
    except Exception as e:
        log_exc(f'GUI 异常退出：{e}')
        raise
    finally:
        _instance_lock.release()
        log('Helper 已退出')


if __name__ == '__main__':
    main()