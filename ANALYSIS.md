# XTU GO (骁途 / GKU) 8.5.3 逆向分析：功能地图与 macOS 内容传输方案

> 目标样本：`XTUGO_8.5.3_253_com.gku.xtugo.apk`
> SHA-256 `5edd835be4d244d16f22890f0d1ddefddc7152b7cf8a2ebb3fb91230d3e69976`（REA 归档清单证据 `ev_c1ecd071…`：4968 个条目、4648 个节点、0 完整性矛盾）
> 反编译：`jadx 1.5.x`（18323 个 java 文件，127 个类反编译失败）+ `apktool`（清单/资源）
> 结论性质：全部为**静态观察**；除"协议实现已在本地假相机上跑通"外，**未在真机上验证**。

---

## 一、结论速览（重点：Mac 怎么拿相机里的照片/视频）

1. **相机是一台"Wi-Fi AP + HTTP/TCP 服务器"**，App 不做云中转。手机连上相机的
   Wi-Fi，相机自己就是网关（`192.168.0.1` / DHCP router），所有内容都在局域网内
   `http://<相机>` 和 `tcp://<相机>:8080` 上完成。
2. **文件列表**：HiSilicon 家族 `GET /cgi-bin/hi3510/getfilelistinfoios.cgi?-start=&-end=`
   返回 JSON 数组（`path/size/create/time`）；SigmaStar 家族
   `GET /cgi-bin/hi3510/getfilelist.cgi?-start=&-end=&-type=Photo|Video` 返回分号分隔路径。
3. **文件下载有两条路**：
   - **HiSilicon：TCP 8080 私有帧协议**（`SocketHisiFile`）—— 72 字节定长头 + 裸文件流，
     **无鉴权、无 Range**。这是 App 实际使用的方式。
   - **SigmaStar / Amba：HTTP GET** `http://<相机><path>`（OkHttp 流式落盘）；
     HiSilicon 的照片也能走 HTTP（全屏预览就用 `http://<ip>/<path>`）。
4. **预览**：`rtsp://<相机>:554/livestream/12`（`/11` 为大码流）；另有
   `http://<相机>:80/12?trans=tcp&action=play&media=video_data` 的 HTTP 流。
5. **反向写入**只有固件通道（8080 的 `RECV_FILE` 或 `fileupload.cgi` 的 multipart），
   相机 SD 卡对 App 是只读的。
6. **macOS 落地**：不需要模拟器、不需要手机。已按上述协议写好并验证了
   `xtu.py`（`info` / `ls` / `get` / `preview`），连上相机 Wi-Fi 即可 `python3 xtu.py get all`。

---

## 二、应用结构

`com.gku.xtugo`，`versionName 8.5.3_253_2026-10-29-15-20-21`，`versionCode 253`，
`compileSdk 36`，`usesCleartextTraffic=true`（全局允许明文 HTTP —— 相机协议的必然结果）。

代码里同时存在**四代相机芯片家族**的完整实现，靠 `getdeviceattr.cgi` 的 `type`/`name` 分流：

| 家族 | 识别特征 | 传输实现 |
|---|---|---|
| **HiSilicon**（Hi3518EV300 等） | `type` 不是 SSC*/CV75/H75N | CGI + **TCP 8080 `SocketHisiFile`** |
| **SigmaStar**（SSC8838 / SSC8826 / SSC8838C） | `type` 命中 `SSConstant.CHIP*` | CGI + **HTTP GET**（`SSFileInfoBean.getRemotePath`） |
| **Amba**（H75N / CV75） | `type` 命中 `AMBA/CV75` | **7878 命令通道 + 8787 数据通道** + RTSP + HTTP |
| **iCatch**（行车记录仪 `com.gku.dashcam.icatch`） | 独立入口 | 原生 SDK：`libcontrol.so`（1116 处 `com::icatchtek::` 符号）、`libreliant.so`、`libusb_transport.so`、`libdepth_net_transport.so` |

主流程（`HomeActivity`）先试 HTTP `getdeviceattr.cgi`，失败再试 Amba 的 7878 会话；
日志字符串就是 `"连上hisi或者sigmastar相机了"` —— 说明当前主推的是 HiSilicon/SigmaStar。

---

## 三、功能地图

| 功能 | 关键实现 | 与相机交互 |
|---|---|---|
| 连相机 / 配网 | `actioncam/*/wifi`、`bluetooth/BLEConnectUtils`（BLE 名以 `XTU_` 开头） | 手机加入相机 AP；AP 名以 `3516A`/`himc`/`YUTU` 开头（`HiDefine.DV_NETWORK_PREFIX*`），默认密码 `12345678` |
| 设备识别 | `HttpRequest.getDeviceAttrCameraInfo` | `getdeviceattr.cgi` |
| 激活 | `p065dv/ui/BaseActivateActivity`、`getactivateinfo.cgi`、`setactivateinfo.cgi?-status=&-version=`、`settrial.cgi` | 新机型可能要求先激活才能取流/取文件（**未验证**） |
| 实时预览 | `C2459DV.getVideoRtspURL`、`TelevisionActivity`（ExoPlayer RTSP） | `rtsp://ip:554/livestream/12`；HTTP-FLV 变体 `:80/12?trans=tcp&action=play&media=video_data` |
| 拍摄控制 | `biz/Command.java`、`HttpProxy` | `record.cgi?-cmd=start/stop`、`record2.cgi`（loop/timelapse/slow/recsnap）、`photo.cgi`（continuous/phototimer/phototimelapse）、`setcamerastatus.cgi?-status=` |
| 相机设置 | `setcurparameter.cgi?-workmode=&-name=&-value=`、`getcurallinfo.cgi`、`getallworkmode.cgi`、`getbatterycapacity.cgi`、`getsdstate.cgi`、`setsystime.cgi`、`sdcommand.cgi?-format`、`reset.cgi` | 全部明文 GET |
| SD 卡浏览 | `Hisi_CameraPicFragment`/`Hisi_CameraRecordFragment`、`RemoteFileManager`、`SSFileUtils` | 见"文件列表" |
| 下载 | `HisiDownloader` + `SocketHisiFile`；`SSDownloadUtil`（OkHttp） | 见"文件下载" |
| 缩略图 | Glide 直接取 `http://ip/<path>`；SigmaStar 用 `/thumb<path>` | HTTP |
| 删除 | `deletefile.cgi?-name=`、`deleteallfiles.cgi` | HTTP |
| 固件升级 | `UpgradeManager`（8080 `RECV_FILE`）、`fileupload.cgi`（multipart，字段 `sd`）、`upgrade.cgi` 触发 | 反向写入通道 |
| App 自身升级 | `server4.gkuvision.com/push/api/getNewestVersion?firmWareModel=XTUGO_Android` | HTTPS |
| 账号/云 | `loginmodule`（登录/注册/找回/公告/反馈/注销）、`module_base_xtugo/api` | HTTPS |
| 本地相册 | `localimage`（`DCIM/xtugo/{photo,video}/ActionCam`）、`ImageEditLib`、`videoedit`（剪辑/配乐/录音） | 本地 |
| 轨迹/地图 | `TrackActivity` + 高德 SDK `libAMapSDK_MAP_v10_0_600.so`（另见清单里的 Google Maps API key） | 本地/网络 |
| 分享 | `SendSoftActivity`、清单里 `<queries>` 的微信/QQ/抖音/WhatsApp；字节 `zeus` 直播/投稿 SDK | 系统分享 |
| 直播推流 | `remote_live`（`SelectLiveActivity`、`StreamConfigActivity`、`StreamingActivity`、`QRShowActivity`） | RTMP/自定义 |
| 行车记录仪 | `com.gku.dashcam.icatch` + `cn.rxt.qscase`（OEM 复用壳） | iCatch SDK |

> 说明：`module_camera/hisi`、`module_camera/amba` 是"新 UI"抽象层；`actioncam/hisilicon/dv`
> 是旧 UI（jadx 里被重命名为 `p065dv`）。两套并存，说明代码是多年迭代堆叠的。

---

## 四、内容传输协议（核心）

### 4.1 寻址

```java
// NetworkDeviceUtils.getIpDevice()
for (RouteInfo r : connectivityManager.getLinkProperties(networkDevice).getRoutes())
    if (r.isDefaultRoute()) return r.getGateway().getHostAddress();
// 兜底：WifiManager.getDhcpInfo().serverAddress/gateway
```

即 **相机 = 当前 Wi-Fi 的 DHCP 路由器**。SigmaStar 的常量还把 `SS_IP` 写死为
`192.168.0.1`（`SSConstant.SS_IP`），Amba 预览同样默认 `192.168.0.1`。

macOS 等价物：`ipconfig getoption en0 router`（Wi-Fi 接口的 DHCP 路由器）。

### 4.2 设备识别

```
GET http://<ip>/cgi-bin/hi3510/getdeviceattr.cgi
→ var name="XTU S7PRO MAX";\r\n var type="Hi3518EV300";\r\n var softversion="…";\r\n var hardversion="NewAPP";
```

`HttpRequest.getDeviceAttrCameraInfo` 用 OkHttp，5 秒超时、失败不重试；`type` 决定家族。

### 4.3 文件列表

**HiSilicon**

```
GET /cgi-bin/hi3510/getfilecount.cgi
→ var count="123";

GET /cgi-bin/hi3510/getfilelistinfoios.cgi?-start=0&-end=49
→ [{"path":"/DCIM/100MEDIA/IMG_0001.JPG","create":"2026-11-01 10:00:00","time":12,"size":4006}, …]
```

App 分页 50 条一页，直到 `start` 超出 `count`（`Hisi_CameraPicFragment.loadList`）；
`StringParser.getMediaList` 用 `JSONArray` 解析，字段名固定 `path/create/time/size`。

**SigmaStar**

```
GET /cgi-bin/hi3510/getfiletypecount.cgi
→ var imagecount="10"; var videocount="3";

GET /cgi-bin/hi3510/getfilelist.cgi?-start=0&-end=N&-type=Photo|Video
→ /DCIM/…/A.JPG;/DCIM/…/B.DNG;…
```

路径用 `;` 切分（`RemoteFileManager.getFileNameList`）。大小/时间另取
`getfileinfo.cgi?-name=<path>`。注意：SigmaStar 照片常是 DNG，App 下载时会强制
把后缀换成 `.JPG`（`HisiDownloader`：`!isHisi() && type==1`）。

### 4.4 文件下载

#### (A) HiSilicon：TCP 8080 私有帧协议（App 主路径）

`SocketHisiFile` + `TCP_MSG_S` 两张类合起来就是完整规范：

```
连接 tcp://<ip>:8080   (connect 超时 5s，SO_TIMEOUT 5s)

请求 72 字节，小端
  偏移 0   int32   0            命令 = MSG_TYPE.GET_FILE
  偏移 4   int32   len          路径字节长度
  偏移 8   byte[64] path        UTF-8，右补 \0（**超过 64 字节会被截断，必须 ≤64**）

应答 72 字节，小端
  偏移 0   int32   status       0 = OK，非 0 失败
  偏移 4   uint32  contentLength 文件大小
  偏移 8   byte[64] 保留

随后：contentLength 字节裸文件数据，发完相机即关连接
```

实现要点（照抄 Java 的语义）：
- 读满 72 字节再解析；`contentLength` 用 `& 0xFFFFFFFFL` 当无符号处理。
- 循环 `read()` 直到累计等于 `contentLength`，否则视为失败
  （`CODE_ERROR_HISI_DOWNLOAD_CONTENTLENGTH_NOT_EQUALS`）。
- 没有 Range / 断点续传，没有鉴权令牌，没有会话概念 —— 一个文件一个 TCP 连接。

#### (B) SigmaStar / Amba / 回退：HTTP GET

```
GET http://<ip><path>            # path 来自列表，例如 /DCIM/100MEDIA/VID_0002.MP4
```

Amba 家族路径可能带 `/tmp/SD0` 前缀，App 会按需去掉（`SSFileInfoBean.getRemotePath`）。
HiSilicon 的全屏照片预览同样走 HTTP（`Hisi_PicPreviewActivity` → `getUrlDownload`）。

#### (C) Amba：双通道

- 命令通道 `tcp://<ip>:7878`：Gson JSON，`{"msg_id":…,"token":…,"param":…,"offset":…,"fetch_size":…}`，
  无长度前缀，靠 JSON 边界累积解析（`SocketAmbaCmd`）。
- 数据通道 `tcp://<ip>:8787`：先由 7878 下发 `GET_FILE`（含 offset/fetch_size），
  再从 8787 读裸字节（`SocketAmbaDownload`）。
- 文件回放同时存在 HTTP 形态：`http://<ip><path>`（`AmbaRemoteVideoActivity`）。

### 4.5 反向（Mac → 相机）

只有固件通道：

- 8080：`TCP_MSG_S{cmd=RECV_FILE(2), dataLen=文件长度, data=文件名}` → 读 72 字节应答
  → 发 32 字节 ASCII MD5 → 发文件字节（`SendSoftActivity`，界面文案就是 `firm_sending`）。
- HTTP：`POST /cgi-bin/hi3510/fileupload.cgi`，`multipart/form-data`，字段名 `sd`
  （`UpgradeManager` 手写 HTTP 报文，直接开 socket 写）。
- `upgrade.cgi` 触发升级。

### 4.6 预览

```
rtsp://<ip>:554/livestream/12      # 小码流；/11 大码流（isPreviewBigBitRate）
http://<ip>:80/12?trans=tcp&action=play&media=video_data
```

macOS 用 `ffplay`/`VLC`/`IINA` 直接打开 RTSP 即可。

---

## 五、macOS 落地方案

### 方案 A（推荐，已实现并验证）：`xtu.py`

```bash
networksetup -setairportnetwork en0 "<相机SSID>" "12345678"   # 或手动连
python3 xtu.py info
python3 xtu.py ls
python3 xtu.py --dest ~/Pictures/xtu get all
python3 xtu.py preview        # 打印 RTSP/HTTP 地址
```

内部逻辑：Wi-Fi 接口的 DHCP router 定位相机 → `getdeviceattr.cgi` 判家族 →
按家族走 socket:8080 或 HTTP → 落盘并校验字节数。Hisi 默认 socket（与 App 一致），
`--http` 可强制走 HTTP。

### 方案 B：不写代码，纯命令行

```bash
# 列表（HiSilicon）
curl -s "http://192.168.0.1/cgi-bin/hi3510/getfilelistinfoios.cgi?-start=0&-end=50" | jq .
# 单文件（照片，HTTP 一定可用）
curl -O "http://192.168.0.1/DCIM/100MEDIA/IMG_0001.JPG"
# 视频（Hisi 建议用 socket 协议，或试 HTTP）
curl -O "http://192.168.0.1/DCIM/100MEDIA/VID_0002.MP4"
# 实时预览
ffplay -fflags nobuffer "rtsp://192.168.0.1:554/livestream/12"
```

### 方案 C：把整台相机当 U 盘？—— 不行

清单里没有 SMB/FTP/WebDAV/MTP 的任何痕迹；USB 相关只有 iCatch 的 UVC 传输
（`libusb_transport.so`，用于取流不是取文件）。所以 macOS 上唯一的通用路径就是
上面这套局域网协议。

### 工程注意事项

| 事项 | 说明 |
|---|---|
| 路径长度 | 8080 协议只有 64 字节放路径；超长路径必须改走 HTTP |
| 断点续传 | 8080/App 都不支持；HTTP 是否支持 `Range` 未验证（相机若支持，可用 `curl -C -`） |
| 大文件 | App 串行下载（单线程队列，`HisiDownloader.queueEvent`），实测吞吐取决于相机 Wi-Fi |
| 并发 | 8080 是单连接单文件；并发多开会话可能导致相机端拒绝 |
| 激活门槛 | 新机型可能必须先在 App 里激活；Mac 端若只能 `getdeviceattr` 却列不出文件，优先怀疑这个 |
| 安全 | 明文 HTTP + 无鉴权 8080，属于纯局域网设计；不要在公共网络里连相机 |

---

## 六、验证记录

`xtugo-mac/mock_camera.py` 按同一协议实现了假相机（HTTP CGI + 8080 socket），
`xtu.py` 端到端跑通：

| 用例 | 结果 |
|---|---|
| HiSilicon 档 `info` | 正确识别 `family=hisilicon`, `type=Hi3518EV300` |
| HiSilicon 档 `ls` | 3 个文件、大小/时间正确 |
| HiSilicon 档 `get all` | 3/3 成功，**下载字节与源逐字节相同**（SHA-256 比对一致） |
| SigmaStar 档 `ls` | `getfiletypecount` + `getfilelist` + `getfileinfo` 补大小，正确 |
| SigmaStar 档 `get` | HTTP 下载 1/1 成功 |
| HTTP 回退 `get --http` | 成功 |

这验证的是**协议实现与反编译结果一致**，不是真机行为。

---

## 七、局限与未知

1. **未在真机验证**：手头没有 XTU 相机。型号差异（尤其"是否必须先激活"）无法确认。
2. **列表索引基**：App 首屏用 `-start=0`，我按 0 起实现；若某固件是 1 起，会出现偏移一条，
   已在实现里用"读空即停"兜底，但未验证。
3. **Amba 家族**：`msg_id` 只确认了个别常量（如 `MSG_ID_SET_ACTIVATE`、`PUT_FILE`、
   文件列表 `FileListInfoResponseBean`），完整消息表未穷举；7878 的报文边界靠 JSON 累积解析，
   没有长度前缀，实现风险较高，故 `xtu.py` 未实现 Amba 下载。
4. **iCatch 家族**（行车记录仪）：走原生 SDK（`libcontrol.so` 等），协议在 so 内部，
   本次未展开。
5. **HTTP Range**：App 不用，未见证据；是否支持需真机 `curl -r` 试。
6. **REA 的 Android provider 不可用**（`REA_JADX_MCP_JAR` 未配置），因此 Android 侧
   使用 jadx/apktool CLI 直接反编译；归档身份与原生库身份仍由 REA 证据锚定
   （`ev_c1ecd071…` APK 清单、`ev_341f3b81…` `libcontrol.so` 导出表）。

## 八、下一步可做

- 有真机时：先 `xtu.py info`，再 `ls`；若空，抓 App 的 `getactivateinfo.cgi` 流程补激活。
- 想要"自动把相机照片同步进 macOS 照片 App"：在 `xtu.py` 外套一层 `launchd` 定时任务 +
  `osascript` 导入 Photos。
- 若要支持 Amba：先抓 App 与相机的 7878 报文（在 Mac 上做 AP 桥接或让手机走代理），
  补齐 `msg_id` 表。


---

## 九、真机校正（2026-11-11，XTU S7PRO MAX / Hi3519DV500）

设备：`name=XTU S7PRO MAX`、`type=Hi3519DV500`、`softversion=20.8.6.1.20261026.CN`、`region=CN`。

| 项 | 真机实测 | 与原分析的差异 |
|---|---|---|
| 列表路径 | `sd/DCIM/100XTUDV/NORM0006.MP4`（**相对路径**） | 原文按 `/DCIM/...` 推断，错 |
| 列表字段 | `path/create("2026/11/11 11:06:44")/time("38")/size("200071183")`，数值是字符串 | 与 JSON 结构一致，类型不同 |
| 8080 协议 | `cmd=0 && len=200071183`，7 秒传完 200MB（≈28MB/s） | 帧格式推断正确 |
| HTTP :80 | `GET sd/DCIM/...` → **400**（相机要求带前导 `/`） | 原文假设 HTTP 可直接取文件，需要路径变体 |
| HTTP :8080 | 连接被重置（该端口是二进制协议） | — |
| RTSP 554 | 开放 | 与推断一致 |
| 每页条数 | 相机返回条数可能少于 `-end - -start` | 分页要用实际条数推进 |

结论：HiSilicon 家族的 8080 私有协议是主通道，HTTP 只是可选补充；
`getfilelistinfoios.cgi` 的分页语义要以实际返回条数为准。
