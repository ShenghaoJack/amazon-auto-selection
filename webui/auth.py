"""Local account and session store for the WebUI; no bootstrap over HTTP."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import getpass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / '.webui' / 'auth.sqlite3'
ROLES = ('admin', 'operator', 'viewer')
SESSION_SECONDS = 8 * 60 * 60
WINDOW = 15 * 60
MAX_FAILURES = 5
USERNAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{2,63}$')


def _cats(raw):
    """类目白名单：JSON 数组。`["*"]` = 全部；`[]` = 无。admin 恒为全部。"""
    try:
        data = json.loads(raw) if isinstance(raw, str) and raw.strip() else []
    except (TypeError, ValueError):
        data = []
    return [str(x) for x in data if isinstance(x, str) and str(x).strip()] if isinstance(data, list) else []


def _dump_cats(items):
    return json.dumps([str(x) for x in (items or []) if str(x).strip()], ensure_ascii=False)


@contextmanager
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(DB_PATH), timeout=20)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript('''
    CREATE TABLE IF NOT EXISTS users (name TEXT PRIMARY KEY COLLATE NOCASE, salt BLOB NOT NULL, password BLOB NOT NULL, role TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, categories TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, username TEXT NOT NULL COLLATE NOCASE REFERENCES users(name) ON DELETE CASCADE, csrf TEXT NOT NULL, expires REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS failures (username TEXT NOT NULL COLLATE NOCASE, at REAL NOT NULL);
    CREATE INDEX IF NOT EXISTS failures_user_at ON failures(username, at);
    CREATE TABLE IF NOT EXISTS audit (at REAL NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL, cat TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '');
    -- 2026-11-12: 类目归属与共享（类目名里已含市场+年月，不需要额外字段）
    CREATE TABLE IF NOT EXISTS category_owner (cat TEXT PRIMARY KEY, owner TEXT NOT NULL COLLATE NOCASE, created_at REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS category_share (cat TEXT NOT NULL, username TEXT NOT NULL COLLATE NOCASE, level TEXT NOT NULL DEFAULT 'view', shared_by TEXT NOT NULL, shared_at REAL NOT NULL, PRIMARY KEY (cat, username));
    ''')
    # ★ 2026-11-12: 老库迁移。
    #   ① users.categories —— 类目白名单（`["*"]`=全部, `[]`=无）；
    #   ② audit.cat / audit.detail —— 操作日志的「类目」与「细节」两列。
    try:
        cols = {r[1] for r in db.execute('PRAGMA table_info(users)')}
        if 'categories' not in cols:
            db.execute("ALTER TABLE users ADD COLUMN categories TEXT NOT NULL DEFAULT '[]'")
            db.execute("UPDATE users SET categories=? WHERE role!='admin'", ('["*"]',))
    except sqlite3.Error:
        pass
    # 2026-11-12: users.created_at —— 账号创建时间；老库从 audit 的 create_user 记录回填
    try:
        ucols = {r[1] for r in db.execute('PRAGMA table_info(users)')}
        if 'created_at' not in ucols:
            db.execute("ALTER TABLE users ADD COLUMN created_at REAL NOT NULL DEFAULT 0")
        db.execute("UPDATE users SET created_at = COALESCE((SELECT MIN(a.at) FROM audit a "
                   "WHERE a.action='create_user' AND a.target=users.name), 0) WHERE created_at = 0")
    except sqlite3.Error:
        pass
    try:
        acols = {r[1] for r in db.execute('PRAGMA table_info(audit)')}
        if 'cat' not in acols:
            db.execute("ALTER TABLE audit ADD COLUMN cat TEXT NOT NULL DEFAULT ''")
        if 'detail' not in acols:
            db.execute("ALTER TABLE audit ADD COLUMN detail TEXT NOT NULL DEFAULT ''")
        db.execute('CREATE INDEX IF NOT EXISTS audit_at ON audit(at)')
    except sqlite3.Error:
        pass
    # Schema/backfill statements may open an implicit transaction. Finish the
    # migration before callers start their own BEGIN IMMEDIATE transaction.
    db.commit()
    try: os.chmod(DB_PATH, 0o600)
    except OSError: pass
    try:
        with db:
            yield db
    finally:
        db.close()

def _hash(password, salt):
    return hashlib.scrypt(password.encode('utf-8'), salt=salt, n=32768, r=8, p=1, maxmem=128*1024*1024, dklen=32)

def valid_password(password):
    if not isinstance(password, str) or not 6 <= len(password) <= 256:
        raise ValueError('密码长度须为 6 至 256 个字符')

def _audit(db, actor, action, target='', cat='', detail=''):
    db.execute('INSERT INTO audit (at,actor,action,target,cat,detail) VALUES (?,?,?,?,?,?)',
               (time.time(), str(actor)[:64], str(action)[:64], str(target)[:200],
                str(cat)[:120], str(detail)[:500]))
    # 只保留最近 180 天（走 audit_at 索引，代价很小），避免日志库无限增长
    db.execute('DELETE FROM audit WHERE at < ?', (time.time() - 180 * 86400,))


def log_action(actor, action, target='', cat='', detail=''):
    """记一条操作日志（登录/改账号/各类业务操作共用）。"""
    with connect() as db:
        _audit(db, actor, action, target, cat, detail)

def create_user(name, password, role, *, actor, first=False, categories=None):
    if not isinstance(name, str) or not USERNAME.fullmatch(name):
        raise ValueError('账号须为 3 至 64 位字母、数字、下划线、点或连字符')
    if role not in ROLES: raise ValueError('无效角色')
    valid_password(password)
    # 管理员恒为全部；其它角色默认无（`[]`），由管理员在账号页勾选类目
    cats = _dump_cats(categories if categories is not None else (['*'] if role == 'admin' else []))
    salt = secrets.token_bytes(16)
    encoded = _hash(password, salt)
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if first and db.execute('SELECT 1 FROM users LIMIT 1').fetchone():
            raise ValueError('已有账号，不能重复初始化')
        try: db.execute('INSERT INTO users (name,salt,password,role,enabled,categories,created_at) VALUES (?,?,?,?,1,?,?)',
                        (name, salt, encoded, role, cats, time.time()))
        except sqlite3.IntegrityError: raise ValueError('账号已存在') from None
        _audit(db, actor, 'create_user', name)

def list_users():
    with connect() as db:
        rows = [dict(r) for r in db.execute('SELECT name,role,enabled,categories,created_at FROM users ORDER BY name')]
    for r in rows:
        r['categories'] = _cats(r.get('categories'))
    return rows

def login(name, password):
    name = str(name or '')[:64]
    password = str(password or '')[:256]
    now = time.time()
    with connect() as db:
        db.execute('DELETE FROM failures WHERE at < ?', (now-WINDOW,))
        fails = db.execute('SELECT count(*) FROM failures WHERE username=?', (name,)).fetchone()[0]
        burst = db.execute('SELECT count(*) FROM failures WHERE at > ?', (now-60,)).fetchone()[0]
        if fails >= MAX_FAILURES or burst >= 60:
            _audit(db, name, 'login_throttled')
            return None
        row = db.execute('SELECT * FROM users WHERE name=?', (name,)).fetchone()
        salt = row['salt'] if row else bytes(16)
        expected = row['password'] if row else bytes(32)
        actual = _hash(password, salt)
        if not row or not row['enabled'] or not hmac.compare_digest(actual, expected):
            db.execute('INSERT INTO failures VALUES (?,?)', (name, now))
            _audit(db, name, 'login_failed')
            return None
        db.execute('DELETE FROM failures WHERE username=?', (name,))
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        db.execute('DELETE FROM sessions WHERE expires < ?', (now,))
        db.execute('INSERT INTO sessions VALUES (?,?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), row['name'], csrf, now+SESSION_SECONDS))
        _audit(db, row['name'], 'login_success')
        return token

def session(token):
    if not token or len(token) > 128: return None
    key = hashlib.sha256(token.encode()).hexdigest()
    with connect() as db:
        row = db.execute('SELECT u.name,u.role,u.enabled,u.categories,s.csrf,s.expires FROM sessions s JOIN users u ON u.name=s.username WHERE s.token_hash=?', (key,)).fetchone()
        if not row or not row['enabled'] or row['expires'] <= time.time(): return None
        return {'name':row['name'], 'role':row['role'], 'csrf':row['csrf'],
                'categories': _cats(row['categories'])}

def logout(token):
    if not token: return
    with connect() as db:
        key = hashlib.sha256(token.encode()).hexdigest()
        row = db.execute('SELECT username FROM sessions WHERE token_hash=?', (key,)).fetchone()
        db.execute('DELETE FROM sessions WHERE token_hash=?', (key,))
        if row: _audit(db, row['username'], 'logout')

def record_action(actor, path):
    with connect() as db:
        _audit(db, actor, 'request', path)


def list_audit(limit: int = 200, actor: str | None = None, cat: str | None = None,
               days: float | None = None, q: str | None = None):
    """操作日志查询：最近 N 条，可按账号 / 类目 / 最近天数 / 关键词过滤。"""
    try:
        n = max(1, min(int(limit), 2000))
    except (TypeError, ValueError):
        n = 200
    where, args = [], []
    if actor:
        where.append('actor = ? COLLATE NOCASE'); args.append(str(actor))
    if cat:
        where.append('cat = ?'); args.append(str(cat))
    if days:
        try:
            where.append('at >= ?'); args.append(time.time() - float(days) * 86400)
        except (TypeError, ValueError):
            pass
    if q:
        where.append('(action LIKE ? OR target LIKE ? OR detail LIKE ? OR cat LIKE ?)')
        like = f'%{q}%'; args += [like, like, like, like]
    sql = 'SELECT at,actor,action,target,cat,detail FROM audit'
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY at DESC LIMIT ?'
    args.append(n)
    with connect() as db:
        rows = db.execute(sql, args).fetchall()
    return [dict(r) for r in rows]

def update_user(name, *, actor, role=None, enabled=None, password=None, categories=None):
    if role is not None and role not in ROLES: raise ValueError('无效角色')
    if enabled is not None and not isinstance(enabled, bool): raise ValueError('账号状态无效')
    if categories is not None and (not isinstance(categories, list)
                                   or any(not isinstance(c, str) or not c.strip() for c in categories)):
        raise ValueError('类目白名单格式无效')
    if password is not None:
        valid_password(password)
        salt = secrets.token_bytes(16); encoded = _hash(password, salt)
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        user = db.execute('SELECT name,role,enabled,categories FROM users WHERE name=?', (name,)).fetchone()
        if not user: raise ValueError('账号不存在')
        new_role = role if role is not None else user['role']
        new_enabled = int(enabled) if enabled is not None else user['enabled']
        new_cats = _dump_cats(categories) if categories is not None else user['categories']
        if user['role']=='admin' and user['enabled'] and (new_role!='admin' or not new_enabled):
            count = db.execute("SELECT count(*) FROM users WHERE role='admin' AND enabled=1").fetchone()[0]
            if count <= 1: raise ValueError('必须保留至少一个启用的管理员')
        db.execute('UPDATE users SET role=?,enabled=?,categories=? WHERE name=?', (new_role,new_enabled,new_cats,name))
        if password is not None: db.execute('UPDATE users SET salt=?,password=? WHERE name=?', (salt,encoded,name))
        if (password is not None or new_role != user['role'] or new_enabled != user['enabled']
                or new_cats != user['categories']):
            db.execute('DELETE FROM sessions WHERE username=?', (name,))
            _audit(db, actor, 'update_user', name)

# ---------------- 类目归属与共享（2026-11-12） ----------------
SHARE_LEVELS = ('view', 'run')


def owner_of(cat):
    with connect() as db:
        row = db.execute('SELECT owner, created_at FROM category_owner WHERE cat=?', (str(cat),)).fetchone()
    return dict(row) if row else None


def set_owner(cat, owner, created_at=None):
    with connect() as db:
        db.execute('INSERT OR IGNORE INTO category_owner (cat, owner, created_at) VALUES (?,?,?)',
                   (str(cat), str(owner)[:64], float(created_at if created_at is not None else time.time())))


def ensure_owner(cat, default='admin'):
    row = owner_of(cat)
    if row:
        return str(row['owner'])
    set_owner(cat, default)
    return str(default)


def shares(cat):
    with connect() as db:
        rows = db.execute('SELECT username, level, shared_by, shared_at FROM category_share WHERE cat=? ORDER BY username',
                          (str(cat),)).fetchall()
    return [dict(r) for r in rows]


def share(cat, username, level, shared_by):
    if level not in SHARE_LEVELS:
        raise ValueError('共享级别必须是 view 或 run')
    with connect() as db:
        db.execute('INSERT INTO category_share (cat, username, level, shared_by, shared_at) VALUES (?,?,?,?,?) '
                   'ON CONFLICT(cat, username) DO UPDATE SET level=excluded.level, shared_by=excluded.shared_by, shared_at=excluded.shared_at',
                   (str(cat), str(username)[:64], str(level), str(shared_by)[:64], time.time()))
        _audit(db, shared_by, 'category_share', '%s -> %s(%s)' % (cat, username, level))


def unshare(cat, username, actor=''):
    with connect() as db:
        cur = db.execute('DELETE FROM category_share WHERE cat=? AND username=?', (str(cat), str(username)[:64]))
        n = cur.rowcount or 0
        if n:
            _audit(db, actor, 'category_unshare', '%s -/-> %s' % (cat, username))
    return bool(n)


def owned_cats(username):
    """该用户创建的类目列表。"""
    with connect() as db:
        rows = db.execute('SELECT cat FROM category_owner WHERE owner=? COLLATE NOCASE', (str(username),)).fetchall()
    return [str(r['cat']) for r in rows]


def shared_cats(username):
    with connect() as db:
        rows = db.execute('SELECT cat, level FROM category_share WHERE username=?', (str(username),)).fetchall()
    return {str(r['cat']): str(r['level']) for r in rows}


def main():
    parser=argparse.ArgumentParser(description='WebUI 管理员本机初始化/恢复')
    parser.add_argument('action', choices=['init-admin','reset-admin-password'])
    parser.add_argument('--user', required=True)
    args=parser.parse_args()
    password=getpass.getpass('管理员密码（至少6位）: ')
    if password != getpass.getpass('再次输入密码: '): raise SystemExit('两次密码不一致')
    try:
        if args.action=='init-admin': create_user(args.user,password,'admin',actor='local-cli',first=True)
        else:
            if not any(u['name'].lower()==args.user.lower() and u['role']=='admin' and u['enabled'] for u in list_users()):
                raise ValueError('指定账号不是启用的管理员')
            update_user(args.user,actor='local-cli',password=password)
        print('管理员账号已设置。')
    except ValueError as exc: raise SystemExit(str(exc)) from exc

if __name__=='__main__': main()
