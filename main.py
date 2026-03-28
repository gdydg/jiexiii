from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field, HttpUrl
from playwright.sync_api import sync_playwright
import subprocess
import threading
import time
import json
import os
from urllib import request
from datetime import datetime, timezone
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlparse

app = FastAPI()

ADMIN_HTML = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>直播源抓取管理台</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif; max-width: 960px; margin: 24px auto; padding: 0 16px; }
    h1 { margin-bottom: 8px; }
    .hint { color: #555; margin-bottom: 16px; }
    .card { border: 1px solid #ddd; border-radius: 8px; padding: 16px; margin-bottom: 16px; }
    .row { display: grid; grid-template-columns: 1fr 1fr 2fr auto; gap: 8px; margin-bottom: 8px; }
    input { padding: 8px; border: 1px solid #bbb; border-radius: 6px; }
    button { padding: 8px 12px; border: none; border-radius: 6px; cursor: pointer; background: #2563eb; color: white; }
    button.secondary { background: #334155; }
    button.danger { background: #dc2626; }
    pre { background: #0b1020; color: #c6f6d5; padding: 12px; border-radius: 8px; overflow: auto; }
  </style>
</head>
<body>
  <h1>直播源抓取管理台</h1>
  <div class="hint">在线录入：分组名、频道名、待抓取链接，然后提交到 /api/sources/import。</div>

  <div class="card">
    <label>抓取间隔(秒)：<input id="interval" type="number" value="300" min="30" max="86400" /></label>
    <div id="rows"></div>
    <button id="addRowBtn" class="secondary">+ 增加一行</button>
    <button id="submitBtn">提交导入</button>
    <button id="scanBtn" class="secondary">立即扫描一次</button>
    <button id="errorBtn" class="secondary">查看最近错误</button>
  </div>

  <div class="card">
    <h3>状态</h3>
    <button id="statusBtn" class="secondary">刷新状态</button>
    <pre id="output">等待操作...</pre>
  </div>

  <script>
    const rowsEl = document.getElementById('rows');
    const outputEl = document.getElementById('output');

    function addRow(group = '', channel = '', url = '') {
      const row = document.createElement('div');
      row.className = 'row';
      row.innerHTML = `
        <input placeholder="分组名" value="${group}" />
        <input placeholder="频道名" value="${channel}" />
        <input placeholder="待抓取链接 (https://...)" value="${url}" />
        <button class="danger">删除</button>
      `;
      row.querySelector('button').addEventListener('click', () => row.remove());
      rowsEl.appendChild(row);
    }

    function collectSources() {
      const rows = Array.from(rowsEl.querySelectorAll('.row'));
      return rows.map(r => {
        const [group, channel, url] = r.querySelectorAll('input');
        return {
          group_name: group.value.trim(),
          channel_name: channel.value.trim(),
          url: url.value.trim(),
        };
      }).filter(s => s.group_name && s.channel_name && s.url);
    }

    async function submitImport() {
      const payload = {
        interval_seconds: Number(document.getElementById('interval').value || 300),
        sources: collectSources(),
      };
      const resp = await fetch('/api/sources/import', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await resp.json();
      outputEl.textContent = JSON.stringify(data, null, 2);
    }

    async function fetchStatus() {
      const resp = await fetch('/api/scheduler/status');
      const data = await resp.json();
      outputEl.textContent = JSON.stringify(data, null, 2);
    }

    async function scanOnce() {
      const resp = await fetch('/api/scan/once', { method: 'POST' });
      const data = await resp.json();
      outputEl.textContent = JSON.stringify(data, null, 2);
    }

    async function fetchErrors() {
      const resp = await fetch('/api/scan/errors?limit=100');
      const data = await resp.json();
      outputEl.textContent = JSON.stringify(data, null, 2);
    }

    document.getElementById('addRowBtn').addEventListener('click', () => addRow());
    document.getElementById('submitBtn').addEventListener('click', submitImport);
    document.getElementById('statusBtn').addEventListener('click', fetchStatus);
    document.getElementById('scanBtn').addEventListener('click', scanOnce);
    document.getElementById('errorBtn').addEventListener('click', fetchErrors);

    addRow('默认分组', '示例频道', 'https://example.com/live');
  </script>
</body>
</html>
"""


class SourceInput(BaseModel):
    group_name: str = Field(..., min_length=1, description="输出到 m3u 的分组名")
    channel_name: str = Field(..., min_length=1, description="输出到 m3u 的频道名")
    url: HttpUrl = Field(..., description="待抓取页面链接")


class ImportSourcesRequest(BaseModel):
    sources: List[SourceInput] = Field(..., min_length=1, description="待抓取来源配置列表")
    interval_seconds: int = Field(300, ge=30, le=86400, description="抓取间隔（秒）")


class SchedulerState(BaseModel):
    running: bool
    interval_seconds: int
    source_count: int
    stream_count: int
    last_run_at: Optional[str]
    last_error_count: int


# 全局状态（内存存储）
source_configs: Dict[str, Dict[str, str]] = {}
alive_streams: Dict[str, Dict[str, str]] = {}
scan_interval_seconds = 300
max_streams_per_source = 3
last_scan_at: Optional[datetime] = None
last_scan_errors: List[str] = []
state_lock = threading.Lock()
stop_event = threading.Event()
worker_thread: Optional[threading.Thread] = None


def start_scheduler_if_needed():
    global worker_thread
    if worker_thread is None or not worker_thread.is_alive():
        stop_event.clear()
        worker_thread = threading.Thread(target=scheduler_loop, daemon=True)
        worker_thread.start()


def load_sources_from_env() -> Dict[str, object]:
    """
    支持两种环境变量导入格式：
    1) SOURCE_IMPORT_CONFIG_JSON='{"interval_seconds":1800,"sources":[{"group_name":"g","channel_name":"c","url":"https://..."}]}'
    2) SOURCE_IMPORT_SOURCES 按行配置：group_name|channel_name|url
       另配 AUTO_IMPORT_INTERVAL_SECONDS=1800
    """
    raw_json = os.getenv("SOURCE_IMPORT_CONFIG_JSON", "").strip()
    raw_lines = os.getenv("SOURCE_IMPORT_SOURCES", "").strip()
    interval_from_env = os.getenv("AUTO_IMPORT_INTERVAL_SECONDS", "").strip()
    interval = 300
    if interval_from_env.isdigit():
        interval = max(30, min(int(interval_from_env), 86400))

    configs: Dict[str, Dict[str, str]] = {}

    if raw_json:
        try:
            payload = json.loads(raw_json)
            if isinstance(payload, dict):
                interval = int(payload.get("interval_seconds", interval))
                interval = max(30, min(interval, 86400))
                for item in payload.get("sources", []) or []:
                    if not isinstance(item, dict):
                        continue
                    url = str(item.get("url", "")).strip()
                    group_name = str(item.get("group_name", "")).strip()
                    channel_name = str(item.get("channel_name", "")).strip()
                    if url and group_name and channel_name:
                        configs[url] = {
                            "group_name": group_name,
                            "channel_name": channel_name,
                        }
        except Exception as e:
            print(f"SOURCE_IMPORT_CONFIG_JSON 解析失败: {e}")

    if raw_lines:
        for line in raw_lines.splitlines():
            row = line.strip()
            if not row or row.startswith("#"):
                continue
            parts = [p.strip() for p in row.split("|", 2)]
            if len(parts) != 3:
                print(f"忽略无效 SOURCE_IMPORT_SOURCES 行: {row}")
                continue
            group_name, channel_name, url = parts
            if group_name and channel_name and url:
                configs[url] = {
                    "group_name": group_name,
                    "channel_name": channel_name,
                }

    return {"interval_seconds": interval, "sources": configs}


def is_direct_stream_url(target_url: str) -> bool:
    path = (urlparse(target_url).path or "").lower()
    return any(path.endswith(ext) for ext in [".m3u8", ".flv", ".mpd", ".mp4", ".ts", ".m4s"])


def extract_bilibili_stream_urls(target_url: str) -> List[str]:
    """
    通过 Bilibili 公开接口获取直播流，避免页面抓取超时或反爬导致拿不到流。
    """
    parsed = urlparse(target_url)
    host = (parsed.netloc or "").lower()
    if "live.bilibili.com" not in host:
        return []

    room_path = (parsed.path or "").strip("/")
    if room_path.isdigit():
        room_id = room_path
    else:
        qs_room_id = parse_qs(parsed.query).get("room_id", [""])[0]
        if not qs_room_id.isdigit():
            return []
        room_id = qs_room_id

    try:
        common_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Referer": target_url,
            "Origin": "https://live.bilibili.com",
            "Accept": "application/json, text/plain, */*",
        }

        room_init_url = f"https://api.live.bilibili.com/room/v1/Room/room_init?id={room_id}"
        room_init_req = request.Request(room_init_url, headers=common_headers)
        with request.urlopen(room_init_req, timeout=10) as resp:
            room_init = json.loads(resp.read().decode("utf-8", errors="ignore"))

        real_room_id = str(room_init.get("data", {}).get("room_id", room_id))
        play_info_url = (
            "https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo"
            f"?room_id={real_room_id}&protocol=0,1&format=0,2&codec=0,1&qn=10000&platform=web&ptype=8"
        )
        play_info_req = request.Request(play_info_url, headers=common_headers)
        with request.urlopen(play_info_req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="ignore"))

        playurl = payload.get("data", {}).get("playurl_info", {}).get("playurl", {})
        streams = playurl.get("stream", []) or []
        urls = set()
        for stream in streams:
            for fmt in stream.get("format", []) or []:
                for codec in fmt.get("codec", []) or []:
                    base_url = codec.get("base_url", "")
                    for info in codec.get("url_info", []) or []:
                        host_url = info.get("host", "")
                        extra = info.get("extra", "")
                        if host_url and base_url:
                            urls.add(f"{host_url}{base_url}{extra}")
        return list(urls)
    except Exception as e:
        print(f"Bilibili API 抓流失败: {e}; target={target_url}")
        return []


def extract_stream_urls(target_url: str):
    extracted_urls = set()  # 使用 set 去重
    if is_direct_stream_url(target_url):
        return [target_url]

    bilibili_urls = extract_bilibili_stream_urls(target_url)
    if bilibili_urls:
        return bilibili_urls

    with sync_playwright() as p:
        # Docker Linux 环境下必需的启动参数，关闭沙盒和 GPU 加速
        browser = p.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu'
            ]
        )

        # 创建上下文，并伪装成普通的 Windows Chrome 浏览器，降低被反爬拦截的概率
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        # 监听网络请求
        def handle_request(request):
            url = request.url
            # 提取 .m3u8 或 .m4s 媒体流链接
            if ".m3u8" in url or ".m4s" in url:
                extracted_urls.add(url)

        page.on("request", handle_request)

        try:
            # 直播页经常有长连接，networkidle 容易超时，这里改用 domcontentloaded
            page.goto(target_url, timeout=45000, wait_until="domcontentloaded")
            # 额外等待一段时间，确保动态加载的视频流请求已经发出
            time.sleep(8)
        except Exception as e:
            print(f"访问网页时发生错误: {e}; target={target_url}")
        finally:
            browser.close()

    return list(extracted_urls)


def ffprobe_is_alive(stream_url: str, timeout_seconds: int = 8) -> bool:
    """调用 ffprobe 测活，能读取到视频/音频流即认为可用。"""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type",
        "-of",
        "default=nokey=1:noprint_wrappers=1",
        stream_url,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        output = (result.stdout or "") + (result.stderr or "")
        return result.returncode == 0 and any(t in output for t in ["video", "audio"])
    except FileNotFoundError:
        # 环境没有 ffprobe，直接返回 False 并在日志中提示
        print("ffprobe 未安装，无法执行测活。")
        return False
    except subprocess.TimeoutExpired:
        return False


def run_scan_once():
    global last_scan_at, last_scan_errors

    with state_lock:
        pages = dict(source_configs)

    fresh_alive: Dict[str, Dict[str, str]] = {}
    errors: List[str] = []

    for page_url, source_meta in pages.items():
        stream_urls = extract_stream_urls(page_url)
        if not stream_urls:
            errors.append(f"未抓到流: {page_url}")
        kept_for_source = 0
        for stream_url in stream_urls:
            if kept_for_source >= max_streams_per_source:
                break
            if ffprobe_is_alive(stream_url):
                fresh_alive[stream_url] = {
                    "source_page": page_url,
                    "group_name": source_meta.get("group_name", "auto-discovered"),
                    "channel_name": source_meta.get("channel_name", "live"),
                    "last_ok": datetime.now(timezone.utc).isoformat(),
                }
                kept_for_source += 1
            else:
                errors.append(f"测活失败: {stream_url}")

    with state_lock:
        alive_streams.clear()
        alive_streams.update(fresh_alive)
        last_scan_at = datetime.now(timezone.utc)
        last_scan_errors = errors


def scheduler_loop():
    while not stop_event.is_set():
        start = time.time()
        run_scan_once()
        elapsed = time.time() - start

        with state_lock:
            interval = scan_interval_seconds

        sleep_for = max(1, int(interval - elapsed))
        stop_event.wait(timeout=sleep_for)


# 健康检查接口，用于 Zeabur/Render 判断服务是否存活
@app.get("/")
def health_check():
    return {"status": "ok", "message": "Playwright 爬虫服务运行正常"}


@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)


@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    return ADMIN_HTML


# 核心抓取接口（单次）
@app.get("/api/get_urls")
def get_urls(url: str):
    if not url:
        raise HTTPException(status_code=400, detail="请提供有效的目标 URL")

    urls = extract_stream_urls(url)
    return {
        "target_url": url,
        "count": len(urls),
        "stream_urls": urls,
    }


@app.post("/api/sources/import")
def import_sources(payload: ImportSourcesRequest):
    global scan_interval_seconds

    with state_lock:
        source_configs.clear()
        for src in payload.sources:
            source_configs[str(src.url)] = {
                "group_name": src.group_name.strip(),
                "channel_name": src.channel_name.strip(),
            }
        scan_interval_seconds = payload.interval_seconds

    # 懒启动调度线程
    start_scheduler_if_needed()

    return {
        "message": "已导入来源并启动定时抓取",
        "source_count": len(source_configs),
        "interval_seconds": scan_interval_seconds,
    }


@app.get("/api/sources/import")
def import_sources_help():
    return {
        "message": "该接口仅支持 POST 导入。请使用 JSON 请求体提交 sources 和 interval_seconds。",
        "example": {
            "sources": [
                {
                    "group_name": "央视",
                    "channel_name": "CCTV-1",
                    "url": "https://example.com/live/cctv1"
                }
            ],
            "interval_seconds": 300
        }
    }


@app.post("/api/scan/once")
def scan_once():
    run_scan_once()
    with state_lock:
        return {
            "message": "单次扫描完成",
            "alive_count": len(alive_streams),
            "last_run_at": last_scan_at.isoformat() if last_scan_at else None,
        }


@app.get("/api/scheduler/status", response_model=SchedulerState)
def scheduler_status():
    with state_lock:
        return SchedulerState(
            running=worker_thread is not None and worker_thread.is_alive(),
            interval_seconds=scan_interval_seconds,
            source_count=len(source_configs),
            stream_count=len(alive_streams),
            last_run_at=last_scan_at.isoformat() if last_scan_at else None,
            last_error_count=len(last_scan_errors),
        )


@app.get("/api/scan/errors")
def scan_errors(limit: int = 50):
    with state_lock:
        capped_limit = max(1, min(limit, 200))
        return {
            "last_error_count": len(last_scan_errors),
            "errors": last_scan_errors[:capped_limit],
        }


@app.get("/m3u", response_class=PlainTextResponse)
def m3u_output():
    with state_lock:
        items = list(alive_streams.items())

    lines = ["#EXTM3U"]
    for idx, (url, meta) in enumerate(items, start=1):
        tvg_name = meta.get("channel_name") or f"live-{idx}"
        group = meta.get("group_name") or "auto-discovered"
        source = meta.get("source_page", "")
        display_name = f"{tvg_name} ({source})" if source else tvg_name
        lines.append(f'#EXTINF:-1 tvg-name="{tvg_name}" group-title="{group}",{display_name}')
        lines.append(url)

    playlist = "\n".join(lines) + "\n"
    return playlist


@app.on_event("shutdown")
def shutdown_event():
    stop_event.set()
    if worker_thread and worker_thread.is_alive():
        worker_thread.join(timeout=2)


@app.on_event("startup")
def startup_event():
    global scan_interval_seconds
    loaded = load_sources_from_env()
    env_sources = loaded.get("sources", {})
    if not isinstance(env_sources, dict) or not env_sources:
        return

    with state_lock:
        source_configs.clear()
        source_configs.update(env_sources)
        scan_interval_seconds = int(loaded.get("interval_seconds", 300))

    start_scheduler_if_needed()
    print(f"已从环境变量导入 {len(source_configs)} 条来源，抓取间隔={scan_interval_seconds}s")
