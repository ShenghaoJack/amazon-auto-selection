import http.client
import json
import threading
import time
from http.server import ThreadingHTTPServer

import pytest
from webui import auth, server


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, 'DB_PATH', tmp_path / 'auth.sqlite3')
    monkeypatch.delenv('WEBUI_PUBLIC_ORIGIN', raising=False)
    auth.create_user('admin', 'administrator-pass-123', 'admin', actor='test', first=True)
    auth.create_user('worker', 'operator-pass-12345', 'operator', actor='admin')
    auth.create_user('reader', 'readonly-pass-12345', 'viewer', actor='admin')
    monkeypatch.setattr(server.Handler, '_api_history', lambda self: {'ok': True, 'items': []})
    monkeypatch.setattr(server.Handler, '_api_run', lambda self, body: {'ok': True, 'ran': True})
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True); thread.start()
    yield httpd.server_port
    httpd.shutdown(); httpd.server_close(); thread.join()


def call(port, path, *, method='GET', cookie='', csrf='', body=None, origin=None, host=None):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
    headers = {'Host':host or f'127.0.0.1:{port}'}
    if cookie: headers['Cookie'] = cookie
    if method == 'POST':
        headers.update({'Content-Type':'application/json', 'Origin':origin or f'http://127.0.0.1:{port}', 'X-CSRF-Token':csrf})
    conn.request(method, path, body=json.dumps(body or {}) if method=='POST' else None, headers=headers)
    response = conn.getresponse(); data = response.read(); result = (response.status, dict(response.getheaders()), data)
    conn.close(); return result


def login(port, username, password):
    status, headers, _ = call(port, '/api/auth/login', method='POST', body={'username':username, 'password':password})
    assert status == 200
    cookie = headers['Set-Cookie'].split(';',1)[0]
    status, _, body = call(port, '/api/auth/me', cookie=cookie)
    assert status == 200
    return cookie, json.loads(body)['csrf']


def test_login_gate_roles_csrf_and_logout(app):
    assert call(app, '/')[0] == 303
    assert call(app, '/api/config')[0] == 401
    assert call(app, '/step18-panel.js')[0] == 401
    reader, reader_csrf = login(app, 'reader', 'readonly-pass-12345')
    assert call(app, '/', cookie=reader)[0] == 303
    assert call(app, '/api/history', cookie=reader)[0] == 200
    assert call(app, '/api/config', cookie=reader)[0] == 403
    assert call(app, '/api/run', method='POST', cookie=reader, csrf=reader_csrf)[0] == 403
    assert call(app, '/api/auth/users', cookie=reader)[0] == 403
    worker, csrf = login(app, 'worker', 'operator-pass-12345')
    assert call(app, '/api/run', method='POST', cookie=worker)[0] == 403
    assert call(app, '/api/run', method='POST', cookie=worker, csrf=csrf, origin='https://attacker.example')[0] == 403
    assert call(app, '/api/run', method='POST', cookie=worker, csrf=csrf)[0] == 200
    assert call(app, '/api/auth/users', cookie=worker)[0] == 403
    assert call(app, '/api/auth/logout', method='POST', cookie=worker, csrf=csrf)[0] == 200
    assert call(app, '/api/auth/me', cookie=worker)[0] == 401


def test_admin_reauthentication_change_revokes_sessions_and_last_admin(app):
    admin, csrf = login(app, 'admin', 'administrator-pass-123')
    reader, _ = login(app, 'reader', 'readonly-pass-12345')
    args = {'username':'reader','role':'operator','enabled':True,'admin_password':'wrong'}
    assert call(app, '/api/auth/users/update', method='POST', cookie=admin, csrf=csrf, body=args)[0] == 403
    args['admin_password']='administrator-pass-123'
    assert call(app, '/api/auth/users/update', method='POST', cookie=admin, csrf=csrf, body=args)[0] == 200
    assert call(app, '/api/auth/me', cookie=reader)[0] == 401
    args.update(username='admin',role='viewer')
    assert call(app, '/api/auth/users/update', method='POST', cookie=admin, csrf=csrf, body=args)[0] == 400
    assert call(app, '/api/auth/me', cookie=admin)[0] == 200


def test_proxy_origin_cookie_and_host_are_enforced(app, monkeypatch):
    monkeypatch.setenv('WEBUI_PUBLIC_ORIGIN', 'https://work.example.com')
    assert call(app, '/login')[0] == 403
    assert call(app, '/login', host='attacker.example')[0] == 403
    assert call(app, '/login', host='work.example.com')[0] == 200
    s, h, _ = call(app, '/api/auth/login', method='POST', host='work.example.com', origin='https://work.example.com', body={'username':'admin','password':'administrator-pass-123'})
    assert s == 200 and 'Secure' in h['Set-Cookie'] and 'HttpOnly' in h['Set-Cookie'] and 'SameSite=Strict' in h['Set-Cookie']


def test_no_browser_bootstrap_and_no_default_password(app):
    assert call(app, '/api/auth/init', method='POST')[0] == 401
    with pytest.raises(ValueError, match='已有账号'):
        auth.create_user('another', 'another-password-123', 'admin', actor='test', first=True)


def test_failed_login_limit_and_session_expiry(app):
    for _ in range(auth.MAX_FAILURES):
        assert auth.login('reader', 'wrong-password') is None
    assert auth.login('reader', 'readonly-pass-12345') is None
    with auth.connect() as db:
        db.execute('UPDATE failures SET at=?', (time.time() - auth.WINDOW - 1,))
    token = auth.login('reader', 'readonly-pass-12345')
    assert auth.session(token)['role'] == 'viewer'
    with auth.connect() as db:
        db.execute('UPDATE sessions SET expires=?', (time.time()-1,))
    assert auth.session(token) is None


def test_password_change_invalidates_other_sessions(app):
    token1 = auth.login('worker', 'operator-pass-12345')
    token2 = auth.login('worker', 'operator-pass-12345')
    auth.update_user('worker', actor='admin', password='ExamplePasswordForTests_987')
    assert auth.session(token1) is None and auth.session(token2) is None
    assert auth.login('worker', 'operator-pass-12345') is None
    assert auth.login('worker', 'ExamplePasswordForTests_987') is not None
    assert b'ExamplePasswordForTests_987' not in auth.DB_PATH.read_bytes()


def test_password_minimum_six_characters(app):
    with pytest.raises(ValueError, match='6 至 256'):
        auth.create_user('shortuser', 'abcde', 'viewer', actor='admin')
    auth.create_user('shortuser', 'abcdef', 'viewer', actor='admin')
    assert auth.login('shortuser', 'abcdef') is not None
    auth.update_user('shortuser', actor='admin', password='123456')
    assert auth.login('shortuser', '123456') is not None


def test_self_password_requires_new_password_and_current_password(app):
    cookie, csrf = login(app, 'worker', 'operator-pass-12345')
    request = lambda body: call(app, '/api/auth/password', method='POST', cookie=cookie, csrf=csrf, body=body)[0]
    assert request({'current_password':'operator-pass-12345'}) == 400
    assert request({'current_password':'wrong', 'password':'ExamplePasswordForTests_987'}) == 403
    assert request({'current_password':'operator-pass-12345', 'password':'ExamplePasswordForTests_987'}) == 200
    assert call(app, '/api/auth/me', cookie=cookie)[0] == 401
