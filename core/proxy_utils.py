"""代理 URL 解析、脱敏与本地 HTTP -> SOCKS5(RFC 1928/1929) 桥接转发器。

纯 Python 标准库实现，零外部依赖：
1. 兼容解析常见代理格式：
   - 标准 URL：http://user:pass@host:port、socks5://user:pass@host:port、socks5h://...
   - 面板导出格式：socks5://host:port:user:pass、http://host:port:user:pass、host:port:user:pass
2. 提供脱敏显示 mask_proxy_url()，确保日志与前端永不泄露明文密码。
3. 提供 LocalSocks5HttpBridge：Chromium/Playwright 原生不支持带账号密码的 SOCKS5 代理，
   该桥接器在 127.0.0.1 随机空闲端口监听标准 HTTP 代理（支持 HTTPS CONNECT 隧道与
   普通 HTTP 代理请求），内部通过 SOCKS5 + 用户名/密码认证转发至上游静态住宅代理。
"""
from __future__ import annotations

import base64
import dataclasses
import select
import socket
import socketserver
import struct
import threading
from urllib.parse import quote, unquote, urlparse


@dataclasses.dataclass(frozen=True)
class ProxySpec:
    scheme: str          # http / https / socks5 / socks5h
    host: str
    port: int
    username: str = ""
    password: str = ""

    @property
    def is_socks5(self) -> bool:
        return self.scheme in ("socks5", "socks5h")

    @property
    def needs_bridge_for_chromium(self) -> bool:
        """所有带账密的代理(SOCKS5 与 HTTP)统一走本地无鉴权桥接，彻底避免 Chromium 有头模式 HTTP 407 认证失败。"""
        return bool(self.username or self.password)

    def standard_url(self, *, socks5h: bool = False) -> str:
        sch = "socks5h" if (socks5h and self.is_socks5) else self.scheme
        if self.username or self.password:
            u = quote(self.username, safe="")
            p = quote(self.password, safe="")
            return f"{sch}://{u}:{p}@{self.host}:{self.port}"
        return f"{sch}://{self.host}:{self.port}"

    def masked(self) -> str:
        if self.username:
            head = self.username[:3] + "***" if len(self.username) > 3 else "***"
            return f"{self.scheme}://{head}@{self.host}:{self.port}"
        return f"{self.scheme}://{self.host}:{self.port}"


def parse_proxy_url(raw: str | None) -> ProxySpec | None:
    """解析常见代理字符串；空串返回 None，格式非法抛 ValueError。"""
    text = str(raw or "").strip()
    if not text:
        return None
    if "://" not in text:
        # 兼容裸 host:port 或 host:port:user:pass（默认按 http）
        text = "http://" + text

    scheme_part, rest = text.split("://", 1)
    scheme = scheme_part.strip().lower()
    if scheme not in ("http", "https", "socks5", "socks5h"):
        raise ValueError(f"不支持的代理协议: {scheme!r}（仅支持 http/https/socks5）")
    rest = rest.strip().rstrip("/")

    # 兼容面板常见的 scheme://host:port:user:pass（无 @ 且冒号段数 >= 3）
    if "@" not in rest and rest.count(":") >= 3 and not rest.startswith("["):
        parts = rest.split(":", 3)
        host, port_s, user, pwd = parts[0].strip(), parts[1].strip(), parts[2], parts[3]
        if not host or not port_s.isdigit():
            raise ValueError(f"无效的代理地址格式: {mask_proxy_url(raw)}")
        port = int(port_s)
        if not (1 <= port <= 65535):
            raise ValueError(f"无效的代理端口: {port}")
        return ProxySpec(scheme=scheme, host=host, port=port, username=user, password=pwd)

    parsed = urlparse(f"{scheme}://{rest}")
    host = (parsed.hostname or "").strip()
    port = parsed.port
    if not host or port is None or not (1 <= port <= 65535):
        raise ValueError("代理地址缺少有效主机或端口")
    user = unquote(parsed.username) if parsed.username is not None else ""
    pwd = unquote(parsed.password) if parsed.password is not None else ""
    return ProxySpec(scheme=scheme, host=host, port=port, username=user, password=pwd)


def mask_proxy_url(raw: str | None) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        spec = parse_proxy_url(text)
        return spec.masked() if spec else ""
    except ValueError:
        return "***"


def to_curl_proxy(raw: str | None) -> str:
    """转为 curl_cffi 可直接使用的代理 URL（socks5 自动转 socks5h 走远端 DNS）。"""
    spec = parse_proxy_url(raw)
    if spec is None:
        return ""
    return spec.standard_url(socks5h=True)


# ---------------- 本地 HTTP -> SOCKS5 桥接转发器 ----------------

def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("SOCKS5 代理提前关闭连接")
        buf.extend(chunk)
    return bytes(buf)


def _open_socks5_tunnel(spec: ProxySpec, target_host: str, target_port: int, timeout: float = 20.0) -> socket.socket:
    """建立到上游 SOCKS5 代理的隧道连接（支持无认证 & RFC 1929 用户名密码认证）。"""
    upstream = socket.create_connection((spec.host, spec.port), timeout=timeout)
    try:
        has_auth = bool(spec.username or spec.password)
        methods = b"\x00\x02" if has_auth else b"\x00"
        upstream.sendall(b"\x05" + bytes([len(methods)]) + methods)
        ver, chosen = _recv_exact(upstream, 2)
        if ver != 5 or chosen == 0xFF:
            raise ConnectionError(f"SOCKS5 握手失败: ver={ver}, method={chosen}")

        if chosen == 0x02:
            u_b = spec.username.encode("utf-8")
            p_b = spec.password.encode("utf-8")
            upstream.sendall(b"\x01" + bytes([len(u_b)]) + u_b + bytes([len(p_b)]) + p_b)
            aver, status = _recv_exact(upstream, 2)
            if aver != 1 or status != 0:
                raise ConnectionError("SOCKS5 用户名或密码认证失败")

        # CONNECT 命令，优先用 DOMAINNAME (ATYP=0x03) 让代理端解析 DNS
        try:
            ip4 = socket.inet_aton(target_host)
            addr_part = b"\x01" + ip4
        except OSError:
            host_b = target_host.encode("idna")
            addr_part = b"\x03" + bytes([len(host_b)]) + host_b
        upstream.sendall(b"\x05\x01\x00" + addr_part + struct.pack("!H", target_port))

        rep_hdr = _recv_exact(upstream, 4)
        if rep_hdr[0] != 5 or rep_hdr[1] != 0:
            raise ConnectionError(f"SOCKS5 CONNECT 目标失败: reply={rep_hdr[1]}")
        atyp = rep_hdr[3]
        if atyp == 1:
            _recv_exact(upstream, 4 + 2)
        elif atyp == 3:
            alen = _recv_exact(upstream, 1)[0]
            _recv_exact(upstream, alen + 2)
        elif atyp == 4:
            _recv_exact(upstream, 16 + 2)
        else:
            raise ConnectionError(f"SOCKS5 未知地址类型: {atyp}")
        return upstream
    except Exception:
        try:
            upstream.close()
        except Exception:
            pass
        raise


def _relay_bidirectional(c_sock: socket.socket, u_sock: socket.socket) -> None:
    sockets = [c_sock, u_sock]
    try:
        while True:
            r, _, _ = select.select(sockets, [], [], 60.0)
            if not r:
                break
            for s in r:
                data = s.recv(65536)
                if not data:
                    return
                dst = u_sock if s is c_sock else c_sock
                dst.sendall(data)
    except OSError:
        pass


class _ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class LocalSocks5HttpBridge:
    """为 Chromium 提供本地无鉴权 HTTP 代理端口，透明桥接到上游带账密 HTTP 或 SOCKS5 代理。"""

    def __init__(self, spec: ProxySpec):
        self.spec = spec
        self._server: _ThreadingTCPServer | None = None
        self._thread: threading.Thread | None = None
        self.port: int = 0

    @property
    def http_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> str:
        spec = self.spec
        auth_header = ""
        if spec.username or spec.password:
            token = base64.b64encode(f"{spec.username}:{spec.password}".encode("utf-8")).decode("ascii")
            auth_header = f"Proxy-Authorization: Basic {token}"

        class _BridgeHandler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                c_sock: socket.socket = self.request
                c_sock.settimeout(30.0)
                upstream: socket.socket | None = None
                try:
                    raw = b""
                    while b"\r\n\r\n" not in raw and len(raw) < 65536:
                        chunk = c_sock.recv(4096)
                        if not chunk:
                            return
                        raw += chunk
                    if b"\r\n\r\n" not in raw:
                        return
                    head_bytes, rest_bytes = raw.split(b"\r\n\r\n", 1)
                    lines = head_bytes.decode("iso-8859-1", errors="replace").split("\r\n")
                    if not lines:
                        return
                    parts = lines[0].split()
                    if len(parts) < 3:
                        return
                    method, target, version = parts[0].upper(), parts[1], parts[2]

                    if not spec.is_socks5:
                        # 上游为带账密 HTTP/HTTPS 代理：直接连上游并主动注入 Proxy-Authorization Basic 头
                        upstream = socket.create_connection((spec.host, spec.port), timeout=20.0)
                        fwd_lines = [lines[0]]
                        for ln in lines[1:]:
                            if ln.lower().startswith("proxy-authorization:"):
                                continue
                            fwd_lines.append(ln)
                        if auth_header:
                            fwd_lines.append(auth_header)
                        upstream.sendall(("\r\n".join(fwd_lines) + "\r\n\r\n").encode("iso-8859-1") + rest_bytes)
                        _relay_bidirectional(c_sock, upstream)
                        return

                    if method == "CONNECT":
                        if ":" in target:
                            h, p_s = target.rsplit(":", 1)
                            t_host, t_port = h.strip("[]"), int(p_s)
                        else:
                            t_host, t_port = target, 443
                        upstream = _open_socks5_tunnel(spec, t_host, t_port)
                        c_sock.sendall(f"{version} 200 Connection Established\r\n\r\n".encode("ascii"))
                        if rest_bytes:
                            upstream.sendall(rest_bytes)
                        _relay_bidirectional(c_sock, upstream)
                    else:
                        u = urlparse(target)
                        t_host = (u.hostname or "").strip()
                        t_port = u.port or (443 if u.scheme == "https" else 80)
                        if not t_host:
                            return
                        path = u.path or "/"
                        if u.query:
                            path += "?" + u.query
                        upstream = _open_socks5_tunnel(spec, t_host, t_port)
                        # 重写首行路径并移除 Proxy-Connection 头
                        new_lines = [f"{method} {path} {version}"]
                        for ln in lines[1:]:
                            if ln.lower().startswith("proxy-"):
                                continue
                            new_lines.append(ln)
                        upstream.sendall(("\r\n".join(new_lines) + "\r\n\r\n").encode("iso-8859-1") + rest_bytes)
                        _relay_bidirectional(c_sock, upstream)
                except Exception:
                    try:
                        c_sock.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
                    except Exception:
                        pass
                finally:
                    if upstream is not None:
                        try:
                            upstream.close()
                        except Exception:
                            pass

        self._server = _ThreadingTCPServer(("127.0.0.1", 0), _BridgeHandler)
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.2},
            daemon=True,
            name=f"socks5-bridge-{self.port}",
        )
        self._thread.start()
        return self.http_url

    def stop(self) -> None:
        srv = self._server
        self._server = None
        if srv is not None:
            try:
                srv.shutdown()
            except Exception:
                pass
            try:
                srv.server_close()
            except Exception:
                pass
        self._thread = None

    def __enter__(self) -> "LocalSocks5HttpBridge":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()
