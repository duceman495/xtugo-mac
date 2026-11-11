#!/usr/bin/env python3
"""
xtu.py — XTU GO (骁途 / GKU) 相机 macOS 端内容传输工具

协议来源：XTU GO 8.5.3 (com.gku.xtugo) 反编译结果
  - HiSilicon/SigmaStar 控制面: http://<camera>/cgi-bin/hi3510/*.cgi
  - HiSilicon 文件面: TCP 8080 私有帧协议 (SocketHisiFile)
  - SigmaStar/Amba 文件面: HTTP GET http://<camera><path>
  - 预览: rtsp://<camera>:554/livestream/12

仅使用 Python 标准库，无需安装依赖。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request

CANDIDATE_HOSTS = ["192.168.0.1", "192.168.1.1", "192.168.42.1", "10.0.0.1"]
CGI = "/cgi-bin/hi3510"
SOCKET_PORT = 8080
HTTP_PORT = 80
UA = "XtuMac/1.0"


# ----------------------------------------------------------------- HTTP utils
def http_get(host: str, path: str, port: int = HTTP_PORT, timeout: float = 5.0,
             raw: bool = False):
    url = f"http://{host}:{port}{path}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
        return (data, resp) if raw else data.decode("utf-8", "replace")


def var_map(text: str) -> dict:
    """解析 `var key="value";` / `key="value"` 形式（getdeviceattr / getfilecount）。"""
    return {k: v for k, v in re.findall(r'([A-Za-z_][\w]*)\s*=\s*"([^"]*)"', text)}


def is_ss_type(dev_type: str) -> bool:
    return dev_type in ("SSC8838", "SSC8826", "SSC8838C", "CV75")


def is_amba_type(dev_type: str) -> bool:
    return dev_type in ("H75N", "CV75")


# ----------------------------------------------------------------- discovery
def wifi_interface() -> str | None:
    """macOS: 从 networksetup 找出 Wi-Fi 的 BSD 设备名（en0/en1…）。"""
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.run(["networksetup", "-listallhardwareports"],
                             capture_output=True, text=True, timeout=5).stdout
        blocks = out.split("Hardware Port:")
        for b in blocks:
            if re.search(r"Wi-?Fi|AirPort", b, re.I):
                m = re.search(r"Device:\s*(\w+)", b)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return None


def default_gateway() -> str | None:
    """相机地址：相机 AP 的 DHCP 路由器（= 相机自己）。"""
    dev = wifi_interface()
    if dev:
        try:  # Wi-Fi 接口的 DHCP 路由器
            out = subprocess.run(["ipconfig", "getoption", dev, "router"],
                                 capture_output=True, text=True, timeout=5).stdout.strip()
            if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", out):
                return out
        except Exception:
            pass
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["route", "-n", "get", "default"],
                                 capture_output=True, text=True, timeout=5).stdout
            m = re.search(r"gateway:\s*([0-9.]+)", out)
            if m:
                return m.group(1)
        except Exception:
            pass
    try:  # Linux / 通用
        out = subprocess.run(["sh", "-c", "ip route show default"],
                             capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"default via ([0-9.]+)", out)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def current_ssid() -> str | None:
    if sys.platform != "darwin":
        return None
    dev = wifi_interface()
    if not dev:
        return None
    try:
        out = subprocess.run(["networksetup", "-getairportnetwork", dev],
                             capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"Current Wi-Fi Network:\s*(.+)", out)
        if m:
            return m.group(1).strip()
    except Exception:
        pass
    return None


def probe(host: str, http_port: int = HTTP_PORT, timeout: float = 3.0) -> dict | None:
    """用 getdeviceattr.cgi 判定是否 GKU 相机，并返回设备属性。"""
    try:
        text = http_get(host, f"{CGI}/getdeviceattr.cgi", port=http_port, timeout=timeout)
    except Exception:
        return None
    attrs = var_map(text)
    if not attrs:
        return None
    name = attrs.get("name", "")
    dev_type = attrs.get("type", "")
    soft = attrs.get("softversion", "")
    if not (name or dev_type):
        return None
    if is_ss_type(dev_type):
        family = "sigmastar"
    elif is_amba_type(dev_type):
        family = "amba"
    else:
        family = "hisilicon"
    return {
        "host": host, "family": family, "name": name, "type": dev_type,
        "softversion": soft, "hardversion": attrs.get("hardversion", ""),
        "region": attrs.get("region", ""), "raw": attrs,
    }


def discover(explicit: str | None, http_port: int = HTTP_PORT) -> dict:
    hosts = [explicit] if explicit else []
    gw = default_gateway()
    if gw and gw not in hosts:
        hosts.append(gw)
    hosts += [h for h in CANDIDATE_HOSTS if h not in hosts]
    for h in hosts:
        info = probe(h, http_port=http_port)
        if info:
            return info
    raise SystemExit(
        "未发现相机。\n"
        "  1) 让 Mac 连接到相机的 Wi-Fi 热点（XTU / YUTU / 3516A / himc 开头）\n"
        "  2) 或者用 --host 指定相机地址，例如 --host 192.168.0.1\n"
        f"  已尝试: {', '.join(hosts)}\n"
        f"  当前 Wi-Fi: {current_ssid() or '未知/未连接'}"
    )


# ----------------------------------------------------------------- file list
def list_hisilicon(host: str, http_port: int = HTTP_PORT, limit: int | None = None) -> list[dict]:
    """HiSilicon: getfilecount.cgi + getfilelistinfoios.cgi (JSON 数组)。

    相机每次返回的条数不一定等于 -end - -start（实测会少），所以按"实际返回条数"
    推进 start，并用 count 作为终止条件；重复页直接停，避免死循环。
    """
    files: list[dict] = []
    try:
        count_text = http_get(host, f"{CGI}/getfilecount.cgi", port=http_port)
        count = int(var_map(count_text).get("count", "0") or 0)
    except Exception:
        count = 0
    step = 50
    start = 0
    seen: set[str] = set()
    for _ in range(300):
        try:
            body = http_get(host, f"{CGI}/getfilelistinfoios.cgi?-start={start}&-end={start + step - 1}",
                            port=http_port, timeout=15)
        except Exception:
            break
        try:
            items = json.loads(body)
        except json.JSONDecodeError:
            break
        if not isinstance(items, list) or not items:
            break
        added = 0
        for it in items:
            path = str(it.get("path", "") or "")
            if not path or path in seen:
                continue
            seen.add(path)
            files.append({"path": path,
                          "size": int(str(it.get("size", 0)).strip() or 0),
                          "create": str(it.get("create", "")),
                          "time": str(it.get("time", ""))})
            added += 1
        if limit and len(files) >= limit:
            break
        if added == 0:
            break
        start += len(items)
        if count and len(files) >= count:
            break
    return files


def list_sigmastar(host: str, kind: str = "Photo", http_port: int = HTTP_PORT) -> list[dict]:
    """SigmaStar: getfiletypecount.cgi + getfilelist.cgi (分号分隔路径)。"""
    try:
        count_text = http_get(host, f"{CGI}/getfiletypecount.cgi", port=http_port)
    except Exception:
        return []
    attrs = var_map(count_text)
    key = "imagecount" if kind == "Photo" else "videocount"
    try:
        count = int(attrs.get(key, attrs.get("count", "0")) or 0)
    except ValueError:
        count = 0
    if count <= 0:
        return []
    body = http_get(host, f"{CGI}/getfilelist.cgi?-start=0&-end={max(count - 1, 0)}&-type={kind}",
                    port=http_port, timeout=10)
    out = []
    for chunk in body.split(";"):
        p = chunk.strip()
        if not p:
            continue
        if not p.startswith("/"):
            p = "/" + p
        if p.lower().endswith((".mp4", ".lrv", ".jpg", ".jpeg", ".dng", ".png")):
            item = {"path": p, "size": 0, "create": "", "time": ""}
            # getfilelist.cgi 只给路径，补一次 getfileinfo.cgi 拿大小/时间
            if len(out) < 300:
                try:
                    info_text = http_get(host, f"{CGI}/getfileinfo.cgi?-name={p}",
                                         port=http_port, timeout=4)
                    attrs = var_map(info_text)
                    item["size"] = int(str(attrs.get("size", "0")).strip() or 0)
                    item["create"] = attrs.get("create", "")
                    item["time"] = attrs.get("time", "")
                except Exception:
                    pass
            out.append(item)
    return out


def list_files(info: dict, http_port: int = HTTP_PORT, limit: int | None = None) -> list[dict]:
    if info["family"] == "sigmastar":
        files = list_sigmastar(info["host"], "Photo", http_port) + \
                list_sigmastar(info["host"], "Video", http_port)
        if not files:  # 部分固件两种接口都在
            files = list_hisilicon(info["host"], http_port, limit)
    else:
        files = list_hisilicon(info["host"], http_port, limit)
    if limit:
        files = files[:limit]
    for f in files:
        f["type"] = "photo" if f["path"].lower().endswith(
            (".jpg", ".jpeg", ".png", ".dng", ".heic")) else "video"
    return files


# ----------------------------------------------------------------- download
def download_socket(host: str, remote_path: str, local_path: str,
                    port: int = SOCKET_PORT, timeout: float = 5.0) -> tuple[bool, str]:
    """HiSilicon 私有文件协议 (SocketHisiFile)。

    请求 (72 字节, little-endian):
        int32 0                 ; 固定 0
        int32 len               ; 路径字节数
        byte[64] path           ; UTF-8, 0 填充
    应答 (72 字节, little-endian):
        int32 status            ; 0 = OK
        uint32 contentLength    ; 文件字节数
    随后为 contentLength 字节裸数据。
    """
    path_bytes = remote_path.encode("utf-8")
    if len(path_bytes) > 64:
        return False, "路径超过 64 字节，无法用 socket 协议传输"
    req = struct.pack("<ii", 0, len(path_bytes)) + path_bytes.ljust(64, b"\x00")
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(req)
            head = b""
            while len(head) < 72:
                chunk = s.recv(72 - len(head))
                if not chunk:
                    return False, "相机提前关闭连接"
                head += chunk
            status, length = struct.unpack("<iI", head[:8])
            if status != 0:
                return False, f"相机返回 status={status}"
            if length == 0:
                return False, "文件长度为 0"
            s.settimeout(max(timeout, 30.0))
            got = 0
            tmp = local_path + ".part"
            with open(tmp, "wb") as fp:
                while got < length:
                    chunk = s.recv(min(65536, length - got))
                    if not chunk:
                        break
                    fp.write(chunk)
                    got += len(chunk)
            if got != length:
                os.replace(tmp, local_path) if os.path.exists(tmp) else None
                return False, f"数据不完整 {got}/{length}"
            os.replace(tmp, local_path)
            return True, f"{got} bytes"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def download_http(host: str, remote_path: str, local_path: str,
                  http_port: int = HTTP_PORT, timeout: float = 30.0) -> tuple[bool, str]:
    candidates = [remote_path]
    if remote_path.startswith("/tmp/SD0"):
        candidates.insert(0, remote_path.replace("/tmp/SD0", "", 1))
    last = ""
    for cand in candidates:
        try:
            url = f"http://{host}:{http_port}{cand if cand.startswith('/') else '/' + cand}"
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp, open(local_path, "wb") as fp:
                total = 0
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    fp.write(chunk)
                    total += len(chunk)
            if total > 0:
                return True, f"{total} bytes (HTTP)"
            last = "空文件"
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
    return False, last


def fetch(info: dict, item: dict, dest: str, http_port: int = HTTP_PORT,
          sock_port: int = SOCKET_PORT, prefer_http: bool = False) -> tuple[bool, str]:
    name = os.path.basename(item["path"])
    if not name:
        return False, "无法确定文件名"
    local = os.path.join(dest, name)
    os.makedirs(dest, exist_ok=True)
    if os.path.exists(local) and item.get("size") and os.path.getsize(local) == item["size"]:
        return True, "已存在，跳过"
    order = ["http", "socket"] if (prefer_http or info["family"] != "hisilicon") else ["socket", "http"]
    errs = []
    for how in order:
        ok, msg = (download_socket(info["host"], item["path"], local, sock_port) if how == "socket"
                   else download_http(info["host"], item["path"], local, http_port))
        if ok:
            return True, f"{name}: {msg}"
        errs.append(f"{how}: {msg}")
    return False, f"{name}: " + " | ".join(errs)


# ----------------------------------------------------------------- CLI
def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n}B"


def main() -> int:
    ap = argparse.ArgumentParser(description="XTU GO 相机 macOS 端内容传输工具")
    ap.add_argument("--host", help="相机地址（默认自动探测网关）")
    ap.add_argument("--http-port", type=int, default=HTTP_PORT)
    ap.add_argument("--socket-port", type=int, default=SOCKET_PORT)
    ap.add_argument("--dest", default="./xtu-download", help="下载目录")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info", help="显示相机信息与识别结果")
    p_ls = sub.add_parser("ls", help="列出相机 SD 卡文件")
    p_ls.add_argument("--json", action="store_true")
    p_get = sub.add_parser("get", help="下载文件（索引/路径/all）")
    p_get.add_argument("target", nargs="+")
    p_get.add_argument("--http", action="store_true", help="优先用 HTTP 而不是 socket")
    sub.add_parser("preview", help="打印实时预览地址")

    args = ap.parse_args()
    info = discover(args.host, args.http_port)

    if args.cmd == "info":
        print(json.dumps({k: v for k, v in info.items() if k != "raw"},
                         ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "preview":
        h = info["host"]
        print(f"RTSP : rtsp://{h}:554/livestream/12   (大码流 /11)")
        print(f"HTTP : http://{h}:80/12?trans=tcp&action=play&media=video_data")
        print("macOS 播放: ffplay -fflags nobuffer 'rtsp://%s:554/livestream/12'" % h)
        return 0

    files = list_files(info, args.http_port)
    if args.cmd == "ls":
        if args.json:
            print(json.dumps(files, ensure_ascii=False, indent=2))
            return 0
        if not files:
            print("相机上没有找到文件（或该型号列表接口不同）。")
            return 1
        print(f"{'#':>3}  {'类型':<5} {'大小':>9}  {'创建时间':<19} 路径")
        for i, f in enumerate(files):
            print(f"{i:>3}  {f['type']:<5} {human(f['size']):>9}  {f['create'][:19]:<19} {f['path']}")
        print(f"\n共 {len(files)} 个文件")
        return 0

    # get
    picked: list[dict] = []
    for t in args.target:
        if t == "all":
            picked.extend(files)
            continue
        if t.isdigit() and int(t) < len(files):
            picked.append(files[int(t)])
            continue
        match = [f for f in files if f["path"] == t or f["path"].endswith("/" + os.path.basename(t))]
        if match:
            picked.append(match[0])
        else:
            print(f"! 找不到 {t}")
    if not picked:
        return 1
    ok_n = 0
    for f in picked:
        t0 = time.time()
        ok, msg = fetch(info, f, args.dest, args.http_port, args.socket_port, args.http)
        dt = time.time() - t0
        speed = (f.get("size") or 0) / dt / 1024 / 1024 if dt > 0 and f.get("size") else 0
        print(("✓ " if ok else "✗ ") + msg + (f"  [{speed:.1f} MB/s]" if ok and speed else ""))
        ok_n += 1 if ok else 0
    print(f"\n完成 {ok_n}/{len(picked)} → {os.path.abspath(args.dest)}")
    return 0 if ok_n == len(picked) else 2


if __name__ == "__main__":
    sys.exit(main())
