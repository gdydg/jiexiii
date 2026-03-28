from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, HttpUrl
from playwright.sync_api import sync_playwright
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

app = FastAPI()


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


# 全局状态（内存存储）
source_configs: Dict[str, Dict[str, str]] = {}
alive_streams: Dict[str, Dict[str, str]] = {}
scan_interval_seconds = 300
last_scan_at: Optional[datetime] = None
state_lock = threading.Lock()
stop_event = threading.Event()
worker_thread: Optional[threading.Thread] = None


def extract_stream_urls(target_url: str):
    extracted_urls = set()  # 使用 set 去重

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
            # 访问目标网页，wait_until="networkidle" 表示等待直到网络不再有大量活动
            page.goto(target_url, timeout=30000, wait_until="networkidle")
            # 额外等待 5 秒，确保动态加载的视频流请求已经发出
            time.sleep(5)
        except Exception as e:
            print(f"访问网页时发生错误: {e}")
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
    global last_scan_at

    with state_lock:
        pages = dict(source_configs)

    fresh_alive: Dict[str, Dict[str, str]] = {}

    for page_url, source_meta in pages.items():
        stream_urls = extract_stream_urls(page_url)
        for stream_url in stream_urls:
            if ffprobe_is_alive(stream_url):
                fresh_alive[stream_url] = {
                    "source_page": page_url,
                    "group_name": source_meta.get("group_name", "auto-discovered"),
                    "channel_name": source_meta.get("channel_name", "live"),
                    "last_ok": datetime.now(timezone.utc).isoformat(),
                }

    with state_lock:
        alive_streams.clear()
        alive_streams.update(fresh_alive)
        last_scan_at = datetime.now(timezone.utc)


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
    global scan_interval_seconds, worker_thread

    with state_lock:
        source_configs.clear()
        for src in payload.sources:
            source_configs[str(src.url)] = {
                "group_name": src.group_name.strip(),
                "channel_name": src.channel_name.strip(),
            }
        scan_interval_seconds = payload.interval_seconds

    # 懒启动调度线程
    if worker_thread is None or not worker_thread.is_alive():
        stop_event.clear()
        worker_thread = threading.Thread(target=scheduler_loop, daemon=True)
        worker_thread.start()

    return {
        "message": "已导入来源并启动定时抓取",
        "source_count": len(source_configs),
        "interval_seconds": scan_interval_seconds,
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
        )


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
