#!/usr/bin/env python3
"""
XTU GO 相机 Web UI（本地服务）

  python3 server.py            # 然后浏览器打开 http://127.0.0.1:8765

只监听 127.0.0.1。相机协议复用上一级的 xtu.py。

取文件两条路，自动降级：
  1) HiSilicon 私有协议 tcp://相机:8080（和 App 一样）
  2) HTTP GET http://相机<path>（含 /tmp/SD0、/mnt/sdcard 等变体）
两条都失败时返回结构化错误；已经发出响应后不会再补写内容（避免浏览器报
"此网站似乎出现了问题"）。
"""
from __future__ import annotations

import hashlib
import http.client
import json
import mimetypes
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import xtu  # noqa: E402

CAM_HTTP_PORT = int(os.environ.get("XTU_HTTP_PORT", "80"))
CAM_SOCK_PORT = int(os.environ.get("XTU_SOCKET_PORT", "8080"))
UI_PORT = int(os.environ.get("XTU_UI_PORT", "8765"))
MAX_FILES = int(os.environ.get("XTU_MAX_FILES", "600"))
FORCED_HOST = os.environ.get("XTU_HOST") or None

CACHE_DIR = os.path.expanduser(os.environ.get("XTU_CACHE", "~/.cache/xtu-webui"))
THUMB_DIR = os.path.join(CACHE_DIR, "thumbs")
TMP_DIR = os.path.join(CACHE_DIR, "tmp")
LOG_PATH = os.path.join(CACHE_DIR, "server.log")
for d in (CACHE_DIR, THUMB_DIR, TMP_DIR):
    os.makedirs(d, exist_ok=True)

CAMERA: dict = {"host": FORCED_HOST, "info": None, "ts": 0.0, "error": None}
CAM_LOCK = threading.Lock()
CAM_HTTP_LOCK = threading.Semaphore(1)   # 这类相机的 HTTP 服务基本只扛一路
CAM_SOCK_LOCK = threading.Semaphore(1)   # 8080 是单连接协议
FILES: dict = {"items": [], "ts": 0.0}
FILES_LOCK = threading.Lock()
AUX: dict = {"host": None, "ts": 0.0, "data": {}}

STATE_TTL = 5.0
NEG_TTL = 15.0
FILES_TTL = 20.0
LOG_LOCK = threading.Lock()

MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".dng": "image/x-adobe-dng", ".heic": "image/heic",
        ".mp4": "video/mp4", ".mov": "video/quicktime", ".lrv": "video/mp4"}


def log(msg: str) -> None:
    line = time.strftime("%H:%M:%S ") + msg
    with LOG_LOCK:
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass
    if os.environ.get("XTU_DEBUG"):
        sys.stderr.write(line + "\n")


def guess_type(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return MIME.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"


def kind_of(path: str) -> str:
    return "photo" if os.path.splitext(path)[1].lower() in (
        ".jpg", ".jpeg", ".png", ".dng", ".heic") else "video"


# ------------------------------------------------------------------ 相机发现
def probe_many(hosts: list[str], timeout: float = 1.2) -> dict | None:
    with ThreadPoolExecutor(max_workers=min(8, len(hosts))) as ex:
        futs = {ex.submit(xtu.probe, h, CAM_HTTP_PORT, timeout): h for h in hosts}
        for fut in as_completed(futs):
            try:
                info = fut.result()
            except Exception:
                info = None
            if info:
                return info
    return None


def camera_info(force: bool = False) -> dict:
    with CAM_LOCK:
        ttl = STATE_TTL if CAMERA["info"] else NEG_TTL
        if CAMERA["ts"] and not force and time.time() - CAMERA["ts"] < ttl:
            return {"info": CAMERA["info"], "error": CAMERA["error"]}
        host, info, err, t0 = CAMERA["host"], None, None, time.time()
        if host:
            info = xtu.probe(host, CAM_HTTP_PORT, timeout=2.0)
            if not info:
                err = f"{host} 不是可识别的 XTU 相机（getdeviceattr.cgi 没回应）"
        else:
            hosts = []
            gw = xtu.default_gateway()
            if gw:
                hosts.append(gw)
            hosts += [h for h in xtu.CANDIDATE_HOSTS if h not in hosts]
            info = probe_many(hosts)
            if info:
                CAMERA["host"] = info["host"]
            else:
                err = ("未发现相机。\n"
                       "  1) 确认 Mac 连的是相机 Wi-Fi（XTU / YUTU / 3516A / himc 开头）\n"
                       "  2) 或在「设置」里手动填地址，例如 192.168.0.1\n"
                       f"  已尝试: {', '.join(hosts)}\n"
                       f"  当前 Wi-Fi: {xtu.current_ssid() or '未知/未连接'}")
        CAMERA.update({"info": info, "ts": time.time(), "error": err})
        log(f"[probe] host={host} ok={bool(info)} {time.time()-t0:.2f}s "
            + ("" if info else str(err).split("\n")[0]))
        return {"info": info, "error": err}


def camera_files(force: bool = False) -> list[dict]:
    with FILES_LOCK:
        if not force and time.time() - FILES["ts"] < FILES_TTL:
            return FILES["items"]
    info = camera_info()["info"]
    if not info:
        return []
    try:
        with CAM_HTTP_LOCK:
            items = xtu.list_files(info, CAM_HTTP_PORT, limit=MAX_FILES)
    except Exception as exc:
        log(f"[list] fail {exc}")
        return []
    with FILES_LOCK:
        FILES.update({"items": items, "ts": time.time()})
    log(f"[list] {len(items)} items")
    return items


# ------------------------------------------------------------------ 相机 HTTP
def camera_open(path: str, headers: dict | None = None, timeout: float = 30.0,
                port: int | None = None):
    """GET，返回 (status, headers, resp, conn)。强制 Connection: close。"""
    hdrs = {"User-Agent": "XtuWeb/1.2", "Accept": "*/*", "Connection": "close"}
    if headers:
        hdrs.update(headers)
    conn = http.client.HTTPConnection(CAMERA["host"], port or CAM_HTTP_PORT, timeout=timeout)
    try:
        conn.request("GET", path, headers=hdrs)
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp, conn
    except Exception:
        conn.close()
        raise


def camera_get(path: str, headers: dict | None = None, timeout: float = 20.0,
               max_bytes: int | None = None, port: int | None = None):
    status, hdrs, resp, conn = camera_open(path, headers, timeout, port)
    try:
        body = resp.read(max_bytes) if max_bytes is not None else resp.read()
        return status, hdrs, body
    finally:
        resp.close()
        conn.close()


def path_variants(path: str) -> list[str]:
    """相机路径的写法变体。socket 用原样（本机实测是 sd/DCIM/...），HTTP 用带 / 的形式。"""
    slash = path if path.startswith("/") else "/" + path
    noslash = path[1:] if path.startswith("/") else path
    out = [noslash, slash]
    for prefix in ("/tmp/SD0", "/mnt/sdcard", "/sdcard"):
        if slash.startswith(prefix):
            rest = slash[len(prefix):]
            if rest:
                out += [rest.lstrip("/"), rest]
    if noslash.lower().endswith(".dng"):
        out += [noslash[:-4] + ".JPG", noslash[:-4] + ".jpg"]
    out += [quote(x, safe="/") for x in (noslash, slash)]
    seen, uniq = set(), []
    for p in out:
        if p and p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def http_paths(path: str) -> list[str]:
    """HTTP 请求用：带前导 / 的排前面（相机对 'GET sd/...' 会回 400）。"""
    return sorted(path_variants(path), key=lambda x: 0 if x.startswith("/") else 1)


def http_targets(path: str) -> list[tuple[int, str]]:
    """HTTP 候选：(端口, 路径)。有些固件把文件服务放在 8080 而不是 80。"""
    out = [(CAM_HTTP_PORT, c) for c in http_paths(path)]
    if CAM_SOCK_PORT != CAM_HTTP_PORT:
        out += [(CAM_SOCK_PORT, c) for c in http_paths(path)]
    return out


# ------------------------------------------------------------------ 8080 协议
def _sock_message(remote: str) -> bytes:
    data = remote.encode("utf-8")
    if len(data) > 64:
        raise ValueError("路径超过 64 字节，8080 协议放不下")
    return struct.pack("<ii", 0, len(data)) + data.ljust(64, b"\x00")


def sock_header(host: str, remote: str, timeout: float = 4.0) -> tuple[int, int]:
    with socket.create_connection((host, CAM_SOCK_PORT), timeout=timeout) as s:
        s.settimeout(timeout)
        s.sendall(_sock_message(remote))
        head = b""
        while len(head) < 72:
            chunk = s.recv(72 - len(head))
            if not chunk:
                raise RuntimeError("相机提前断开")
            head += chunk
    cmd, length = struct.unpack("<iI", head[:8])
    return cmd, length


def sock_begin(host: str, remote: str, timeout: float = 10.0):
    """建连 + 发请求 + 读 72 字节头，返回 (socket, length)。调用方负责关闭。"""
    s = socket.create_connection((host, CAM_SOCK_PORT), timeout=timeout)
    try:
        s.settimeout(timeout)
        s.sendall(_sock_message(remote))
        head = b""
        while len(head) < 72:
            chunk = s.recv(72 - len(head))
            if not chunk:
                raise RuntimeError("相机提前断开")
            head += chunk
        cmd, length = struct.unpack("<iI", head[:8])
        if cmd != 0 or length == 0:
            raise RuntimeError(f"8080 拒绝：cmd={cmd} len={length}")
        s.settimeout(60.0)
        return s, length
    except Exception:
        s.close()
        raise


def sock_copy(sock, length: int, out) -> int:
    sent = 0
    while sent < length:
        chunk = sock.recv(min(65536, length - sent))
        if not chunk:
            break
        out.write(chunk)
        sent += len(chunk)
    return sent


def sock_stream(host: str, remote: str, out, timeout: float = 10.0) -> int:
    sock, length = sock_begin(host, remote, timeout)
    try:
        sent = sock_copy(sock, length, out)
    finally:
        sock.close()
    if sent != length:
        raise RuntimeError(f"数据不完整 {sent}/{length}")
    return sent


def fetch_to_file(path: str, dest: str, max_bytes: int | None = None) -> dict:
    """抓整个文件到本地，返回 {ok, how, bytes, tries}。"""
    host = CAMERA["host"]
    tries: list = []
    CAM_SOCK_LOCK.acquire()
    try:
        for cand in path_variants(path):
            if len(cand.encode()) > 64:
                tries.append({"how": "socket", "path": cand, "error": "路径过长"})
                continue
            try:
                if max_bytes:
                    cmd, length = sock_header(host, cand)
                    if cmd != 0 or length == 0:
                        raise RuntimeError(f"8080 拒绝：cmd={cmd} len={length}")
                    if length > max_bytes:
                        raise RuntimeError(f"文件 {length}B 超过上限")
                with open(dest, "wb") as fh:
                    n = sock_stream(host, cand, fh)
                return {"ok": True, "how": "socket", "bytes": n, "path": cand, "tries": tries}
            except Exception as exc:
                tries.append({"how": "socket", "path": cand,
                              "error": f"{type(exc).__name__}: {exc}"})
                log(f"[fetch] socket {cand}: {exc}")
    finally:
        CAM_SOCK_LOCK.release()

    CAM_HTTP_LOCK.acquire()
    try:
        for port, cand in http_targets(path):
            try:
                status, hdrs, resp, conn = camera_open(cand, timeout=60, port=port)
            except Exception as exc:
                tries.append({"how": "http", "path": cand,
                              "error": f"{type(exc).__name__}: {exc}"})
                continue
            try:
                if status != 200:
                    tries.append({"how": "http", "path": cand, "error": f"HTTP {status}"})
                    continue
                n = 0
                with open(dest, "wb") as fh:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        n += len(chunk)
                        if max_bytes and n > max_bytes:
                            raise RuntimeError(f"文件超过 {max_bytes}B 上限")
                        fh.write(chunk)
                if n:
                    return {"ok": True, "how": "http", "bytes": n, "path": cand, "tries": tries}
                tries.append({"how": "http", "path": cand, "error": "空文件"})
            except Exception as exc:
                tries.append({"how": "http", "path": cand,
                              "error": f"{type(exc).__name__}: {exc}"})
                log(f"[fetch] http {cand}: {exc}")
            finally:
                resp.close()
                conn.close()
    finally:
        CAM_HTTP_LOCK.release()
    return {"ok": False, "how": None, "tries": tries}


# ------------------------------------------------------------------ 缩略图
def thumb_path(path: str, size: int) -> str:
    key = hashlib.sha1(f"{path}|{size}".encode()).hexdigest()[:20]
    return os.path.join(THUMB_DIR, key + ".jpg")


def make_thumb(path: str, size: int) -> str | None:
    out = thumb_path(path, size)
    if os.path.exists(out) and os.path.getsize(out) > 0:
        return out
    src = os.path.join(TMP_DIR, os.path.basename(out) + ".src")

    # 视频：只找相机生成的海报图（.THM/.jpg），绝不为缩略图拉整个视频
    if kind_of(path) == "video":
        stem = os.path.splitext(path)[0]
        res = {"ok": False}
        for cand in (stem + ".THM", stem + ".thm", stem + ".jpg", stem + ".JPG"):
            res = fetch_to_file(cand, src, max_bytes=8 * 1024 ** 2)
            if res["ok"]:
                break
        if not res["ok"]:
            return None
    else:
        res = fetch_to_file(path, src, max_bytes=24 * 1024 ** 2)
        if not res["ok"]:
            return None
    try:
        if shutil.which("sips"):
            subprocess.run(["sips", "-Z", "480", "-s", "format", "jpeg", src, "--out", out],
                           capture_output=True, timeout=25, check=False)
        if not (os.path.exists(out) and os.path.getsize(out) > 0):
            shutil.copyfile(src, out)
        log(f"[thumb] ok {path} {res['how']} {os.path.getsize(out)}B")
        return out
    except Exception as exc:
        log(f"[thumb] {path}: {exc}")
        return None
    finally:
        try:
            os.remove(src)
        except OSError:
            pass


# ------------------------------------------------------------------ HTTP 服务
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "XtuWeb/1.2"
    _sent = False

    # keep-alive 会把同一个实例复用给后续请求，必须每个请求重置
    def handle_one_request(self):
        self._sent = False
        super().handle_one_request()

    def log_message(self, fmt, *args):
        log(f"{self.address_string()} {fmt % args}")

    # ---------- 输出
    def _send(self, code: int, body: bytes, ctype: str, extra: list | None = None):
        if self._sent:
            self.close_connection = True
            return
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or []):
            self.send_header(k, v)
        self.end_headers()
        self._sent = True
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def send_json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(),
                   "application/json; charset=utf-8")

    def send_file(self, fp: str, ctype: str, cache: str = "no-store"):
        try:
            with open(fp, "rb") as fh:
                body = fh.read()
        except OSError as exc:
            return self.send_json({"ok": False, "error": f"本地文件读不到：{exc}"}, 500)
        if self._sent:
            self.close_connection = True
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self._sent = True
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    # ---------- 路由
    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/api/host":
            return self.send_json({"ok": False, "error": "not found"}, 404)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self.send_json({"ok": False, "error": "请求体不是合法 JSON"}, 400)
        host = (payload.get("host") or "").strip()
        with CAM_LOCK:
            CAMERA.update({"host": host or None, "info": None, "ts": 0.0})
        with FILES_LOCK:
            FILES.update({"items": [], "ts": 0.0})
        camera_info(force=True)
        self.send_json(self.status_payload())

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path
        try:
            if p in ("/", "/index.html"):
                return self.send_file(os.path.join(HERE, "static", "index.html"),
                                      "text/html; charset=utf-8")
            if p == "/favicon.ico":
                return self._send(200, b"", "image/x-icon")
            if p == "/api/status":
                return self.send_json(self.status_payload())
            if p == "/api/files":
                items = camera_files(q.get("refresh", ["0"])[0] == "1")
                return self.send_json({"ok": True, "count": len(items), "items": items})
            if p == "/api/thumb":
                return self.serve_thumb(q.get("path", [""])[0])
            if p == "/api/media":
                return self.serve_media(q.get("path", [""])[0])
            if p == "/api/download":
                return self.serve_download(q.get("path", [""])[0])
            if p == "/api/check":
                return self.send_json(self.check_file(q.get("path", [""])[0]))
            if p == "/api/diag":
                return self.send_json(self.diag())
            if p == "/api/config":
                return self.send_json(self.config_payload())
        except BrokenPipeError:
            self.close_connection = True
        except Exception as exc:                        # noqa: BLE001
            log("[error] " + traceback.format_exc().replace("\n", " | ")[:800])
            if self._sent:
                self.close_connection = True
            else:
                self.send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 502)
            return
        return self.send_json({"ok": False, "error": "not found"}, 404)

    # ---------- 状态
    def status_payload(self) -> dict:
        info = camera_info()
        cam = info["info"]
        payload = {"ok": bool(cam), "connected": bool(cam),
                   "host": (cam or {}).get("host") or CAMERA["host"],
                   "family": (cam or {}).get("family"),
                   "name": (cam or {}).get("name"),
                   "type": (cam or {}).get("type"),
                   "softversion": (cam or {}).get("softversion"),
                   "error": info["error"],
                   "wifi_ssid": xtu.current_ssid(),
                   "gateway": xtu.default_gateway(),
                   "log_path": LOG_PATH, "ui_version": "1.3"}
        if cam:
            aux = self._aux(cam["host"])
            if aux.get("sd"):
                payload["sd"] = aux["sd"]
            if aux.get("battery"):
                payload["battery"] = aux["battery"]
        return payload

    def _aux(self, host: str) -> dict:
        with CAM_LOCK:
            if AUX["host"] == host and time.time() - AUX["ts"] < 15:
                return AUX["data"]
        data = {}
        for key, cgi in (("sd", "getsdstate.cgi"), ("battery", "getbatterycapacity.cgi")):
            try:
                with CAM_HTTP_LOCK:
                    text = camera_get(f"{xtu.CGI}/{cgi}", timeout=3)[2].decode("utf-8", "replace")
                if xtu.var_map(text):
                    data[key] = xtu.var_map(text)
            except Exception:
                pass
        with CAM_LOCK:
            AUX.update({"host": host, "ts": time.time(), "data": data})
        return data

    def config_payload(self) -> dict:
        info = camera_info()["info"]
        h = info["host"] if info else (CAMERA["host"] or "")
        return {"ok": True,
                "rtsp": f"rtsp://{h}:554/livestream/12" if h else "",
                "rtsp_big": f"rtsp://{h}:554/livestream/11" if h else "",
                "flv": f"http://{h}:80/12?trans=tcp&action=play&media=video_data" if h else ""}

    @staticmethod
    def _valid(raw: str) -> str | None:
        if not raw or len(raw) > 600 or ".." in raw or "\x00" in raw:
            return None
        if "://" in raw:                       # 只接受相机路径，不接受完整 URL
            return None
        return raw

    # ---------- 缩略图
    def serve_thumb(self, raw: str):
        path = self._valid(raw)
        if not path:
            return self._send(404, b"", "image/jpeg")
        item = next((f for f in camera_files() if f["path"] == path), {})
        fp = make_thumb(path, int(item.get("size") or 0))
        if not fp:
            return self._send(404, b"", "image/jpeg")
        return self.send_file(fp, "image/jpeg", cache="max-age=86400")

    # ---------- 预览（灯箱）
    def serve_media(self, raw: str):
        path = self._valid(raw)
        if not path:
            return self.send_json({"ok": False, "error": "路径非法"}, 400)
        is_video = kind_of(path) == "video"
        tries: list = []

        # HTTP 流式
        CAM_HTTP_LOCK.acquire()
        try:
            for port, cand in http_targets(path):
                try:
                    status, hdrs, resp, conn = camera_open(cand, timeout=30, port=port)
                except Exception as exc:
                    tries.append({"how": "http", "path": cand, "error": str(exc)[:120]})
                    continue
                try:
                    if status != 200:
                        tries.append({"how": "http", "path": cand, "error": f"HTTP {status}"})
                        continue
                    ctype = hdrs.get("Content-Type") or ""
                    if ctype.startswith("text/"):
                        tries.append({"how": "http", "path": cand, "error": ctype})
                        continue
                    if not ctype.startswith(("image/", "video/")) or \
                            ctype.startswith("application/octet-stream"):
                        ctype = guess_type(cand)
                    name = os.path.basename(cand)
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    if "Content-Length" in hdrs:
                        self.send_header("Content-Length", hdrs["Content-Length"])
                    else:
                        self.close_connection = True
                        self.send_header("Connection", "close")
                    self.send_header("Content-Disposition", f'inline; filename="{name}"')
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self._sent = True
                    try:
                        while True:
                            chunk = resp.read(65536)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        self.close_connection = True
                    except Exception as exc:
                        log(f"[media] stream {cand}: {exc}")
                        self.close_connection = True
                    return
                finally:
                    resp.close()
                    conn.close()
        finally:
            CAM_HTTP_LOCK.release()

        # 8080 兜底：单连接直通（视频也走这里，边下边发，不落内存）
        CAM_SOCK_LOCK.acquire()
        try:
            for cand in path_variants(path):
                if len(cand.encode()) > 64:
                    tries.append({"how": "socket", "path": cand, "error": "路径过长"})
                    continue
                try:
                    sock, length = sock_begin(host, cand)
                except Exception as exc:
                    tries.append({"how": "socket", "path": cand, "error": str(exc)[:120]})
                    continue
                try:
                    name = os.path.basename(cand)
                    self.send_response(200)
                    self.send_header("Content-Type", "video/mp4" if is_video else "image/jpeg")
                    self.send_header("Content-Length", str(length))
                    self.send_header("Accept-Ranges", "none")
                    self.send_header("Content-Disposition", f'inline; filename="{name}"')
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self._sent = True
                    try:
                        sock_copy(sock, length, self.wfile)
                    except (BrokenPipeError, ConnectionResetError):
                        self.close_connection = True
                    except Exception as exc:
                        log(f"[media] 8080 stream {cand}: {exc}")
                        self.close_connection = True
                    return
                finally:
                    sock.close()
        finally:
            CAM_SOCK_LOCK.release()

        if not is_video:                       # 图片退到缩略图
            item = next((f for f in camera_files() if f["path"] == path), {})
            fp = make_thumb(path, int(item.get("size") or 0))
            if fp:
                return self.send_file(fp, "image/jpeg")
        log(f"[media] fail {path} {tries}")
        return self.send_json({"ok": False, "error": "相机没给出这个文件", "tries": tries}, 502)

    # ---------- 下载
    def serve_download(self, raw: str):
        path = self._valid(raw)
        if not path:
            return self.send_json({"ok": False, "error": "路径非法"}, 400)
        name = os.path.basename(path) or "file"
        ctype = guess_type(path)
        host = CAMERA["host"]
        tries: list = []

        # 8080：先握手拿长度，再边读边写（浏览器拿到准确的 Content-Length）
        CAM_SOCK_LOCK.acquire()
        try:
            for cand in path_variants(path):
                if len(cand.encode()) > 64:
                    tries.append({"how": "socket", "path": cand, "error": "路径过长"})
                    continue
                try:
                    sock, length = sock_begin(host, cand)
                except Exception as exc:
                    tries.append({"how": "socket", "path": cand, "error": str(exc)[:120]})
                    continue
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(length))
                    self.send_header("Content-Disposition",
                                     f'attachment; filename="{name}"; '
                                     f"filename*=UTF-8\'\'{quote(name)}")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self._sent = True
                    try:
                        sock_copy(sock, length, self.wfile)
                    except Exception as exc:
                        log(f"[download] 8080 中断 {cand}: {exc}")
                        self.close_connection = True
                    return
                finally:
                    sock.close()
        finally:
            CAM_SOCK_LOCK.release()

        # HTTP：流式透传
        CAM_HTTP_LOCK.acquire()
        try:
            for port, cand in http_targets(path):
                try:
                    status, hdrs, resp, conn = camera_open(cand, timeout=120, port=port)
                except Exception as exc:
                    tries.append({"how": "http", "path": cand, "error": str(exc)[:120]})
                    continue
                try:
                    if status != 200:
                        tries.append({"how": "http", "path": cand, "error": f"HTTP {status}"})
                        continue
                    cam_ctype = hdrs.get("Content-Type") or ""
                    if not cam_ctype.startswith(("image/", "video/")):
                        cam_ctype = ctype
                    self.send_response(200)
                    self.send_header("Content-Type", cam_ctype)
                    if "Content-Length" in hdrs:
                        self.send_header("Content-Length", hdrs["Content-Length"])
                    else:
                        self.close_connection = True
                        self.send_header("Connection", "close")
                    self.send_header("Content-Disposition",
                                     f'attachment; filename="{name}"; filename*=UTF-8\'\'{quote(name)}')
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self._sent = True
                    try:
                        while True:
                            chunk = resp.read(65536)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        self.close_connection = True
                    except Exception as exc:
                        log(f"[download] stream {cand}: {exc}")
                        self.close_connection = True
                    return
                finally:
                    resp.close()
                    conn.close()
        finally:
            CAM_HTTP_LOCK.release()
        log(f"[download] fail {path} {tries}")
        return self.send_json({"ok": False, "error": "这个文件两条通道都没取到",
                               "path": path, "tries": tries,
                               "hint": "点「诊断」看详细结果，或把日志发给我"}, 502)

    # ---------- 预检（避免浏览器直接跳到一个坏链接）
    def check_file(self, raw: str) -> dict:
        path = self._valid(raw)
        if not path:
            return {"ok": False, "error": "路径非法"}
        host = CAMERA["host"]
        out = {"ok": False, "path": path, "tries": []}
        CAM_SOCK_LOCK.acquire()
        try:
            for cand in path_variants(path):
                if len(cand.encode()) > 64:
                    out["tries"].append({"how": "socket", "path": cand, "error": "路径过长"})
                    continue
                try:
                    cmd, length = sock_header(host, cand)
                    if cmd == 0 and length > 0:
                        return {"ok": True, "how": "socket", "bytes": length, "path": cand}
                    out["tries"].append({"how": "socket", "path": cand,
                                         "error": f"cmd={cmd} len={length}"})
                except Exception as exc:
                    out["tries"].append({"how": "socket", "path": cand,
                                         "error": f"{type(exc).__name__}: {exc}"})
        finally:
            CAM_SOCK_LOCK.release()
        CAM_HTTP_LOCK.acquire()
        try:
            for port, cand in http_targets(path):
                try:
                    status, hdrs, body = camera_get(cand, timeout=15, max_bytes=1, port=port)
                except Exception as exc:
                    out["tries"].append({"how": "http", "path": cand,
                                         "error": f"{type(exc).__name__}: {exc}"})
                    continue
                if status == 200:
                    return {"ok": True, "how": "http", "path": cand,
                            "bytes": int(hdrs.get("Content-Length") or 0)}
                out["tries"].append({"how": "http", "path": cand, "error": f"HTTP {status}"})
        finally:
            CAM_HTTP_LOCK.release()
        return out

    # ---------- 诊断
    def diag(self) -> dict:
        info = camera_info(force=True)
        cam = info["info"]
        out = {"ok": bool(cam), "error": info["error"], "host": CAMERA["host"],
               "family": (cam or {}).get("family"), "type": (cam or {}).get("type"),
               "name": (cam or {}).get("name"), "log_path": LOG_PATH, "steps": []}
        if not cam:
            return out
        host = CAMERA["host"]

        def step(name, fn):
            t0 = time.time()
            try:
                detail = fn()
                ok = not (isinstance(detail, dict) and detail.get("ok") is False)
            except Exception as exc:
                detail, ok = f"{type(exc).__name__}: {exc}", False
            out["steps"].append({"step": name, "ok": ok,
                                 "ms": round((time.time() - t0) * 1000), "detail": detail})

        step("getdeviceattr", lambda: xtu.var_map(
            camera_get(f"{xtu.CGI}/getdeviceattr.cgi", timeout=5)[2].decode("utf-8", "replace")))
        step("getfilecount", lambda: camera_get(
            f"{xtu.CGI}/getfilecount.cgi", timeout=5)[2].decode("utf-8", "replace")[:160])
        step("getfilelistinfoios", lambda: camera_get(
            f"{xtu.CGI}/getfilelistinfoios.cgi?-start=0&-end=2", timeout=8)[2]
             .decode("utf-8", "replace")[:240])
        def probe_8080():
            cmd, length = sock_header(host, "/")
            return {"ok": cmd == 0, "cmd": cmd, "len": length}
        step("8080_connect", probe_8080)

        items = camera_files()
        if items:
            sample = items[0]["path"]
            out["sample"] = sample

            def probe_sock():
                cmd, length = sock_header(host, sample)
                return {"ok": cmd == 0, "cmd": cmd, "len": length, "path": sample}
            step("8080_file", probe_sock)

            for port, cand in http_targets(sample):
                def probe_http(cand=cand, port=port):
                    status, hdrs, _ = camera_get(cand, timeout=20, max_bytes=1, port=port)
                    return {"ok": status == 200, "status": status, "path": cand, "port": port,
                            "ctype": hdrs.get("Content-Type"), "len": hdrs.get("Content-Length")}
                step(f"http_file :{port} {cand[-36:]}", probe_http)

            tfile = os.path.join(TMP_DIR, "diag.bin")
            def probe_fetch():
                r = fetch_to_file(sample, tfile)
                size = os.path.getsize(tfile) if os.path.exists(tfile) else 0
                try:
                    os.remove(tfile)
                except OSError:
                    pass
                return {"ok": r["ok"], "how": r["how"], "bytes": size, "tries": r["tries"]}
            step("fetch_file", probe_fetch)

        step("rtsp_554", lambda: {"open": self._tcp_open(host, 554)})
        step("socket_8080_port", lambda: {"open": self._tcp_open(host, CAM_SOCK_PORT)})
        return out

    @staticmethod
    def _tcp_open(host: str, port: int, timeout: float = 3.0) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except Exception:
            return False


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    httpd = Server(("127.0.0.1", UI_PORT), Handler)
    print(f"XTU GO Web UI → http://127.0.0.1:{UI_PORT}")
    print(f"相机端口: HTTP {CAM_HTTP_PORT} / SOCKET {CAM_SOCK_PORT}"
          + (f" · 手动指定 {FORCED_HOST}" if FORCED_HOST else ""))
    print(f"日志: {LOG_PATH}")
    print("连上相机 Wi-Fi 后刷新页面。⌘C 退出。")
    if "--no-open" not in sys.argv:
        try:
            webbrowser.open(f"http://127.0.0.1:{UI_PORT}")
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
