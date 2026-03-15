from fastapi import FastAPI, HTTPException
from playwright.sync_api import sync_playwright
import time

app = FastAPI()

def extract_stream_urls(target_url: str):
    extracted_urls = set() # 使用 set 去重
    
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

# 健康检查接口，用于 Zeabur/Render 判断服务是否存活
@app.get("/")
def health_check():
    return {"status": "ok", "message": "Playwright 爬虫服务运行正常"}

# 核心抓取接口
@app.get("/api/get_urls")
def get_urls(url: str):
    if not url:
        raise HTTPException(status_code=400, detail="请提供有效的目标 URL")
        
    urls = extract_stream_urls(url)
    return {
        "target_url": url,
        "count": len(urls),
        "stream_urls": urls
    }
