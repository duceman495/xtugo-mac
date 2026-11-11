#!/usr/bin/env python3
"""mock_camera.py — 按 XTU GO 反编译出的协议实现的假相机，用于验证 xtu.py。

用法: python3 mock_camera.py --profile hisi|ss --http-port 18080 --socket-port 18081
"""
from __future__ import annotations
import argparse, json, socket, struct, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

REL = {"on": False}
PAGE = {"n": 0}


def _rel(key: str) -> str:
    """绝对键 /DCIM/... → 相对键 sd/DCIM/...（模拟真机返回的 path）"""
    return "sd" + key if REL["on"] else key


def _photo(text, color):
    try:                                   # 有 Pillow 就生成真图，方便验证缩略图
        from PIL import Image, ImageDraw
        import io
        im = Image.new("RGB", (640, 480), color)
        d = ImageDraw.Draw(im)
        d.text((24, 220), text, fill="white")
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        return buf.getvalue()
    except Exception:
        return b"\xff\xd8\xff\xe0" + b"JPEGDATA" * 500 + b"\xff\xd9"


_ABS = {
    "/DCIM/100MEDIA/IMG_0001.JPG": _photo("XTU S7PRO MAX  sample 1", (40, 90, 200)),
    "/DCIM/100XTUDV/VID_0002.MP4": b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 400,
    "/DCIM/101MEDIA/IMG_0003.DNG": _photo("XTU sample DNG", (150, 70, 40)),
}
FILES: dict = {}


def _build_files():
    FILES.clear()
    for k, v in _ABS.items():
        FILES[_rel(k)] = v

PROFILES = {
    "hisi": {"name": "XTU S7PRO MAX", "type": "Hi3518EV300", "soft": "V1.2.3"},
    "ss": {"name": "XTU MAX2", "type": "SSC8838", "soft": "V2.0.1"},
}


BROKEN = {"on": False}


def make_handler(profile: str):
    p = PROFILES[profile]

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, body: bytes, ctype="text/plain"):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            path = u.path
            if path.endswith("/getdeviceattr.cgi"):
                return self._send(
                    f'var name="{p["name"]}";\r\nvar type="{p["type"]}";\r\n'
                    f'var softversion="{p["soft"]}";\r\nvar hardversion="NewAPP";\r\n'.encode())
            if path.endswith("/getfilecount.cgi"):
                return self._send(f'var count="{len(FILES)}";\r\n'.encode())
            if path.endswith("/getfiletypecount.cgi"):
                photos = sum(1 for k in FILES if k.upper().endswith((".JPG", ".DNG")))
                videos = len(FILES) - photos
                return self._send(
                    f'var imagecount="{photos}";\r\nvar videocount="{videos}";\r\n'.encode())
            if REL["on"] and not path.startswith("/"):
                self.send_response(400); self.send_header("Content-Length", "0"); self.end_headers(); return
            if path.endswith("/getfilelistinfoios.cgi"):
                start = int(q.get("-start", ["0"])[0])
                end = int(q.get("-end", ["49"])[0])
                items = [{"path": k, "create": "2026/11/11 11:06:44",
                          "time": "38", "size": str(len(v))}
                         for k, v in FILES.items()]
                page = items[start:end + 1]
                if PAGE["n"]:
                    page = page[:PAGE["n"]]      # 模拟相机每页只给 N 条
                return self._send(json.dumps(page).encode(), "application/json")
            if path.endswith("/getfilelist.cgi"):
                kind = q.get("-type", ["Photo"])[0]
                keys = [k for k in FILES if (k.upper().endswith(".MP4") == (kind == "Video"))]
                return self._send(";".join(keys).encode())
            key = path[1:] if (REL["on"] and path.startswith("/")) else path
            if key in FILES:
                if BROKEN["on"]:
                    self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers(); return
                return self._send(FILES[key], "application/octet-stream")
            if path.endswith("/getfileinfo.cgi"):
                name = q.get("-name", [""])[0]
                data = FILES.get(name)
                if data is None:
                    self.send_response(404); self.send_header("Content-Length","0"); self.end_headers(); return
                return self._send(
                    f'var size="{len(data)}";\r\nvar create="2026-11-01 10:00:00";\r\nvar time="12";\r\n'.encode())
            if path.startswith("/thumb") and path[6:] in FILES:
                return self._send(_photo("thumb " + path[6:].split("/")[-1], (60, 120, 80)),
                                  "image/jpeg")
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    return H


def socket_server(port: int, stop: threading.Event):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(8)
    srv.settimeout(0.5)

    def handle(conn):
        with conn:
            head = b""
            while len(head) < 72:
                c = conn.recv(72 - len(head))
                if not c:
                    return
                head += c
            cmd, n = struct.unpack("<ii", head[:8])
            path = head[8:8 + n].decode("utf-8", "replace")
            data = None if BROKEN["on"] else FILES.get(path)
            if cmd != 0 or data is None:
                conn.sendall(struct.pack("<iI", 1, 0) + b"\x00" * 64)
                return
            conn.sendall(struct.pack("<iI", 0, len(data)) + b"\x00" * 64)
            conn.sendall(data)

    while not stop.is_set():
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            continue
        threading.Thread(target=handle, args=(conn,), daemon=True).start()
    srv.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="hisi", choices=list(PROFILES))
    ap.add_argument("--http-port", type=int, default=18080)
    ap.add_argument("--socket-port", type=int, default=18081)
    ap.add_argument("--relative", action="store_true", help="路径用 sd/DCIM/...（模拟 XTU S7PRO MAX）")
    ap.add_argument("--page-size", type=int, default=0, help="列表每页最多返回 N 条")
    ap.add_argument("--broken-files", action="store_true",
                    help="CGI 正常但文件取不到（模拟真机故障）")
    a = ap.parse_args()
    BROKEN["on"] = a.broken_files
    REL["on"] = a.relative
    PAGE["n"] = a.page_size
    _build_files()
    stop = threading.Event()
    threading.Thread(target=socket_server, args=(a.socket_port, stop), daemon=True).start()
    httpd = ThreadingHTTPServer(("127.0.0.1", a.http_port), make_handler(a.profile))
    print(f"mock camera [{a.profile}] http={a.http_port} socket={a.socket_port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()


if __name__ == "__main__":
    main()
