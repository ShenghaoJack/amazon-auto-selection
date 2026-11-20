"""System-launched CDP Chrome lifetime; fail closed when identity is uncertain."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import uuid
from pathlib import Path

from core import ss_lock
from core.run_lock import _try_lock, _unlock


def _key(resource):
    return hashlib.sha256(resource.encode('utf-8')).hexdigest()[:24]


def _directory(resource):
    return ss_lock.lock_dir() / ('auto-close-' + _key(resource))


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.auto-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def record_auto_launch(config, port, profile, pid):
    """Record only browser launches initiated by ensure_ss_chrome, not user launch scripts."""
    try:
        import psutil
        created = psutil.Process(pid).create_time()
        resource = ss_lock.resource_key('http://127.0.0.1:%d' % port, '')
        _write(_directory(resource) / 'launch.json', {
            'pid': pid, 'created': created, 'port': port,
            'profile': os.path.normcase(os.path.abspath(profile)),
        })
    except Exception:
        # Unverifiable provenance must never cause an unrelated Chrome to be closed.
        return False
    return True


def _guard(resource):
    # Dedicated lock path: PIPELINE_SS_LOCK may deliberately map every browser
    # resource to one file, so reusing acquire() here could recursively lock it.
    directory = _directory(resource)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'guard.lock'
    deadline = time.monotonic() + 30
    while True:
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
        fh = os.fdopen(fd, 'r+b')
        state = _try_lock(fh)
        if state is True:
            return ss_lock.SsBrowserLock(fh, path, 'auto-close-guard')
        fh.close()
        if state is None or time.monotonic() >= deadline:
            raise RuntimeError('浏览器使用登记锁不可用')
        time.sleep(.05)


def register(resource):
    """Register active *and waiting* browser leases with a kernel-held marker lock."""
    if not resource.startswith('cdp:'):
        return None
    with _guard(resource):
        directory = _directory(resource)
        directory.mkdir(parents=True, exist_ok=True)
        p = directory / ('lease-' + uuid.uuid4().hex + '.lock')
        f = p.open('w+b')
        if _try_lock(f) is not True:
            f.close()
            raise RuntimeError('浏览器使用登记失败')
        return (resource, p, f)


def _has_live_leases(directory):
    for p in directory.glob('lease-*.lock'):
        try:
            with p.open('r+b') as f:
                state = _try_lock(f)
                if state is not True:
                    return True
                _unlock(f)
            p.unlink(missing_ok=True)
        except OSError:
            return True
    return False


def _close_verified(resource, record):
    import psutil
    from playwright.sync_api import sync_playwright
    from urllib.parse import urlparse

    url = 'http://' + resource[4:]
    parsed = urlparse(url)
    if parsed.hostname not in {'127.0.0.1', 'localhost'} or parsed.port != int(record['port']):
        return False
    proc = psutil.Process(int(record['pid']))
    if abs(proc.create_time() - float(record['created'])) > .01:
        return False
    if proc.name().lower() not in {'chrome', 'chrome.exe', 'chromium', 'chromium.exe'}:
        return False
    args = proc.cmdline()
    port_arg = '--remote-debugging-port=%d' % parsed.port
    profiles = [a.split('=', 1)[1].strip('"') for a in args if a.startswith('--user-data-dir=')]
    if port_arg not in args or len(profiles) != 1:
        return False
    if os.path.normcase(os.path.abspath(profiles[0])) != record['profile']:
        return False
    # Ensure the listening socket belongs to exactly the recorded Chrome process.
    listeners = [c for c in proc.net_connections(kind='inet') if c.status == psutil.CONN_LISTEN
                 and c.laddr and c.laddr.port == parsed.port]
    if not listeners:
        return False
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(url, timeout=5000)
        try:
            browser.new_browser_cdp_session().send('Browser.close')
        except Exception:
            # Chrome may close the CDP connection before sending its response.
            try:
                proc.wait(timeout=5)
            except psutil.TimeoutExpired:
                return False
        else:
            try:
                proc.wait(timeout=5)
            except psutil.TimeoutExpired:
                return False
    return True


def _try_pending(resource, log=None):
    directory = _directory(resource)
    pending, launch = directory / 'pending.json', directory / 'launch.json'
    if not pending.exists() or _has_live_leases(directory):
        return
    if not launch.exists():
        pending.unlink(missing_ok=True)
        if log: log('步骤13后保留卖家精灵窗口：没有系统自动启动记录')
        return
    # The resource lock protects against legacy clients that do not register leases.
    try:
        lock = ss_lock.acquire(resource=resource, wait_seconds=0)
    except ss_lock.SsBrowserLockTimeout:
        return
    try:
        if lock.degraded:
            return
        record = json.loads(launch.read_text(encoding='utf-8'))
        if _close_verified(resource, record):
            pending.unlink(missing_ok=True)
            launch.unlink(missing_ok=True)
            if log: log('步骤13后已关闭系统自动启动的卖家精灵 Chrome')
        else:
            pending.unlink(missing_ok=True)
            if log: log('步骤13后保留窗口：无法确认该 Chrome 是系统启动的目标进程')
    except Exception as exc:
        if log: log('步骤13后保留窗口：浏览器身份或关闭状态无法核验 (%s)' % type(exc).__name__)
    finally:
        lock.release()


def unregister(handle, log=None):
    if not handle:
        return
    resource, p, f = handle
    try:
        with _guard(resource):
            _unlock(f)
            f.close()
            p.unlink(missing_ok=True)
            _try_pending(resource, log)
    except Exception:
        try: f.close()
        except Exception: pass


def request_close(cdp_url, log=None):
    resource = ss_lock.resource_key(cdp_url, '')
    if not cdp_url or not resource.startswith('cdp:'):
        return
    try:
        with _guard(resource):
            _write(_directory(resource) / 'pending.json', {'requested': True})
            _try_pending(resource, log)
    except Exception as exc:
        if log: log('步骤13后未关闭窗口：无法安全登记或核验 (%s)' % type(exc).__name__)
