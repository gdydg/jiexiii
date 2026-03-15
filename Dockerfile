# 使用微软官方的 Playwright Python 镜像 (基于 Ubuntu Jammy)
# 它已经内置了 Chromium、Firefox 等浏览器以及所需的全部 Linux 依赖
FROM mcr.microsoft.com/playwright/python:v1.42.0-jammy

# 设置容器内的工作目录
WORKDIR /app

# 先复制依赖清单，并安装 Python 库
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 将当前目录下的所有代码文件复制到容器的工作目录中
COPY . .

# 暴露 FastAPI 默认运行的 8000 端口
EXPOSE 8000

# 启动服务，监听 0.0.0.0 确保外部可以访问
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
