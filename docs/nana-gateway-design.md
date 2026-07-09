## Nana Banana 浏览器自动化网关 — 实现方案

### 概述

将 Google Labs FX Flow（Nana Banana）的图片生成能力封装为本地 HTTP API，通过 Playwright 浏览器自动化模拟真实用户操作，避免反向代理导致的封号风险。

对外接口设计与现有 `tools/image2/image_gateway_client.py` 保持一致（generate → job_id → poll result），使 Asset Pipeline 和其他系统可以无缝切换 image2 / nana 后端。

### 技术栈

| 组件 | 选型 | 理由 |
|---|---|---|
| 浏览器自动化 | Playwright (Python) | 比 Selenium 更稳定，原生支持 CDP 连接已有浏览器、auto-wait、网络拦截 |
| API 框架 | FastAPI + uvicorn | 异步、轻量，和现有 image2 gateway 模式一致 |
| 任务队列 | asyncio.Queue | 单 worker 串行处理，无需 Redis/Celery |
| 会话管理 | Chrome User Data Directory 复用 | 直接读取本机 Chrome profile，免登录 |

### 架构

```
                    ┌───────────────────────────┐
                    │  FastAPI Server (:3201)    │
                    │                           │
 POST /generate ──► │  Task Queue (asyncio)      │
 GET  /result/{id}  │       │                   │
 POST /cancel/{id}  │       ▼                   │
                    │  Playwright Worker        │
                    │  (单实例，串行)            │
                    │       │                   │
                    │       ▼                   │
                    │  Chrome (CDP / Launch)    │
                    │  labs.google/fx/flow/...  │
                    └───────────────────────────┘
```

### 浏览器连接策略

**推荐方案：连接已运行的 Chrome（CDP 模式）**

这是最安全的方案——用用户自己正在使用的 Chrome，真实的浏览器指纹、扩展、Cookie，不会被 Google 识别为自动化。

```python
# 前置步骤：用户用以下命令启动 Chrome（仅首次需要）
# Windows:
# "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222
# macOS:
# /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222

# Playwright 连接到已运行的 Chrome
from playwright.async_api import async_playwright

pw = await async_playwright().start()
browser = await pw.chromium.connect_over_cdp("http://localhost:9222")
context = browser.contexts[0]  # 复用已有的 context（包含登录态）
page = context.pages[0] if context.pages else await context.new_page()
```

**备选方案：Playwright Launch + User Data Dir**

如果 CDP 不方便，可以指定 Chrome 的 user data directory 启动新的 Chromium：

```python
# Windows Chrome profile 路径通常在:
# C:\Users\<username>\AppData\Local\Google\Chrome\User Data
browser = await pw.chromium.launch_persistent_context(
    user_data_dir=r"C:\Users\Administrator\AppData\Local\Google\Chrome\User Data",
    channel="chrome",       # 使用系统安装的 Chrome
    headless=False,         # 首次调试用 headed，稳定后切 headless
    args=["--disable-blink-features=AutomationControlled"],  # 减少自动化检测特征
)
```

> **注意**：Chrome 不允许两个进程同时使用同一个 user data dir。如果用户的 Chrome 已经在运行，必须用 CDP 模式。

### API 设计

与 image2 网关接口对齐：

#### POST /api/v1/nana/generate

```json
// Request
{
  "prompt": "A risograph print of a forest spirit...",
  "width": 1024,          // 可选，Flow 页面可能不支持精确像素控制
  "height": 1024,         // 可选
  "aspect_ratio": "1:1",  // 可选，优先级高于 width/height
  "seed": -1              // -1 表示随机（Flow 可能不支持 seed）
}

// Response
{
  "status": "submitted",
  "job_id": "uuid-xxx",
  "result_url": "http://localhost:3201/api/v1/nana/result/uuid-xxx"
}
```

#### GET /api/v1/nana/result/{job_id}

```json
// 生成中
{ "status": "pending", "job_id": "uuid-xxx" }

// 完成
{
  "status": "done",
  "job_id": "uuid-xxx",
  "image_url": "http://localhost:3201/images/uuid-xxx.png",
  "filename": "nana_uuid-xxx.png",
  "width": 1024,
  "height": 1024
}

// 失败
{ "status": "error", "job_id": "uuid-xxx", "detail": "timeout after 120s" }
```

#### POST /api/v1/nana/cancel/{job_id}

中断当前正在执行的浏览器操作。

#### GET /api/v1/nana/health

```json
{ "ok": true, "browser_connected": true, "queue_size": 0 }
```

### Playwright 自动化工作流

核心自动化逻辑（伪代码 + 注释）：

```python
async def generate_in_flow(page, prompt: str) -> str:
    """
    在 Google Labs FX Flow 页面中执行一次图片生成。
    返回生成图片的本地文件路径。
    """
    project_id = "aae032c7-27b7-4ee2-85b1-944b48351403"  # 可配置
    url = f"https://labs.google/fx/zh/tools/flow/project/{project_id}"

    # 1. 导航到 Flow 项目页面
    await page.goto(url, wait_until="networkidle")
    # TODO: 首次实现时确认页面加载完成的标志（某个元素出现、URL 稳定等）

    # 2. 定位 prompt 输入框
    # ⚠️ 以下选择器需要用 Chrome DevTools 实际抓取确认
    prompt_input = page.locator('[data-testid="prompt-input"]')
    # 备选选择器策略（按优先级尝试）：
    #   - textarea / contenteditable 元素
    #   - 带 placeholder 的输入框
    #   - 特定 class 的 div[contenteditable]
    #
    # 开发者第一步：打开 Flow 页面 → F12 → 右键输入框 → Inspect
    # 记录实际的 selector，填入此处

    await prompt_input.click()
    # 清空已有内容
    await prompt_input.fill("")  # 如果是 textarea/input
    # 或 await page.keyboard.press("Control+a"); await page.keyboard.press("Backspace")

    # 3. 输入 prompt
    # 用 type() 而不是 fill()，模拟真实打字速度（降低检测风险）
    await prompt_input.type(prompt, delay=50)  # 每字符 50ms 间隔

    # 4. 点击生成按钮
    # ⚠️ 选择器需实际确认
    generate_btn = page.locator('button:has-text("生成"), button:has-text("Generate")')
    # 备选：找带 send/arrow icon 的 button，或按 aria-label 定位
    await generate_btn.click()

    # 5. 等待图片生成完成
    # 策略：监听新图片出现 / 加载指示器消失 / 特定状态元素变化
    #
    # 方案 A：等待 loading spinner 消失
    # await page.wait_for_selector('[data-testid="loading"]', state="hidden", timeout=120000)
    #
    # 方案 B：等待结果图片元素出现
    # result_img = page.locator('img[data-testid="generated-image"]')
    # await result_img.wait_for(state="visible", timeout=120000)
    #
    # 方案 C：监听网络请求（最可靠）
    # async with page.expect_response(
    #     lambda resp: "generated" in resp.url or "image" in resp.url,
    #     timeout=120000
    # ) as response_info:
    #     pass
    # image_url = response_info.value.url

    # 6. 获取生成的图片
    # 方案 A：从 <img> 元素的 src 属性获取 URL，然后下载
    # img_element = page.locator('img.result-image')  # 选择器需确认
    # img_src = await img_element.get_attribute("src")
    #
    # 方案 B：右键保存 / 从 blob URL 提取
    # 如果 Flow 使用 blob: URL，需要通过 JS 提取：
    # img_data = await page.evaluate("""
    #     async (selector) => {
    #         const img = document.querySelector(selector);
    #         const response = await fetch(img.src);
    #         const blob = await response.blob();
    #         return new Promise(resolve => {
    #             const reader = new FileReader();
    #             reader.onload = () => resolve(reader.result);
    #             reader.readAsDataURL(blob);
    #         });
    #     }
    # """, "img.result-image")
    # # img_data 是 data:image/png;base64,... 格式

    # 7. 保存到本地
    # image_path = save_image(img_data, job_id)
    # return image_path

    raise NotImplementedError("开发者需要用 Chrome DevTools 确认实际 DOM 选择器后填入")
```

### DOM 选择器抓取指南（开发者第一步）

在开始编码前，开发者需要完成以下步骤来确定实际的 DOM 选择器：

```
1. 用 Chrome 打开 Flow 页面
2. F12 打开 DevTools
3. 用 Elements 面板检查以下元素：
   a. Prompt 输入框（textarea? input? contenteditable div?）
      → 记录 tag name, class, id, data-* attributes, aria-* attributes
   b. 生成/发送按钮（button? icon button?）
      → 记录 text content, aria-label, class
   c. 加载状态指示器（spinner? progress bar? skeleton?）
      → 记录出现/消失的元素和 class
   d. 生成结果的图片元素（img? canvas? background-image?）
      → 记录 tag name, src 类型（http URL? blob:? data URI?）
   e. 如果有"下载"按钮，记录其选择器
4. 在 Console 面板中测试选择器：
   document.querySelector('你的选择器')  // 应该返回目标元素
5. 将确认的选择器填入 config 或代码中的常量
```

**建议把选择器提取为配置文件**，因为 Google 可能更新 UI：

```python
# selectors.json — 便于维护，Google 更新 UI 后只改这个文件
{
  "prompt_input": "textarea.prompt-input, [contenteditable][data-testid='prompt']",
  "generate_button": "button[aria-label='Generate'], button.send-button",
  "loading_indicator": ".loading-spinner, .generating-state",
  "result_image": "img.generated-image, .result-container img",
  "download_button": "button[aria-label='Download']",
  "new_generation_button": "button[aria-label='New generation']"
}
```

### 网络拦截方案（备选，更可靠）

如果 DOM 选择器不稳定，可以用 Playwright 的网络拦截来捕获图片：

```python
captured_images = []

async def capture_image_response(response):
    """拦截所有图片类型的响应"""
    if response.status == 200:
        content_type = response.headers.get("content-type", "")
        url = response.url
        # 匹配图片生成 API 的响应
        if "image/" in content_type and ("generat" in url or "result" in url):
            body = await response.body()
            captured_images.append({
                "url": url,
                "data": body,
                "content_type": content_type,
            })

page.on("response", capture_image_response)

# 执行生成操作后...
await page.wait_for_timeout(5000)  # 等待网络请求完成
if captured_images:
    # 保存最后一张捕获的图片
    image_data = captured_images[-1]["data"]
```

### 防检测策略

```python
# 1. 启动参数
launch_args = [
    "--disable-blink-features=AutomationControlled",  # 移除 navigator.webdriver 标记
    "--no-first-run",
    "--no-default-browser-check",
]

# 2. 注入脚本，移除自动化特征
await page.add_init_script("""
    // 覆盖 navigator.webdriver
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    // 覆盖 chrome.runtime（某些检测脚本检查这个）
    window.chrome = { runtime: {} };
    // 覆盖 permissions query
    const originalQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (parameters) =>
        parameters.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : originalQuery(parameters);
""")

# 3. 模拟人类行为
import random
await page.wait_for_timeout(random.randint(500, 1500))  # 随机等待
await prompt_input.type(prompt, delay=random.randint(30, 80))  # 随机打字速度
```

### 错误处理与重试

```python
class GenerationError(Exception):
    pass

class LoginPageError(GenerationError):
    """检测到登录页面，需要用户手动重新登录"""
    pass

class TimeoutError(GenerationError):
    """生成超时"""
    pass

async def safe_generate(page, prompt, timeout_sec=120):
    try:
        # 检查是否在登录页面
        if await page.locator('input[type="email"]').is_visible():
            raise LoginPageError("Google 登录页面检测到，请手动登录后重试")

        # 执行生成
        result = await asyncio.wait_for(
            generate_in_flow(page, prompt),
            timeout=timeout_sec
        )
        return result

    except asyncio.TimeoutError:
        raise TimeoutError(f"生成超时 ({timeout_sec}s)")

    except LoginPageError:
        raise  # 不重试，需要用户介入

    except Exception as e:
        # 其他错误：截图保存用于调试，然后重试一次
        await page.screenshot(path=f"error_{job_id}.png")
        raise GenerationError(f"生成失败: {e}")
```

### 完整服务入口结构

```
tools/nana_gateway/
├── README.md                 # 本文档
├── requirements.txt          # playwright, fastapi, uvicorn
├── nana_gateway_server.py    # FastAPI 服务 + Playwright worker
├── selectors.json            # DOM 选择器配置（需开发者首次抓取填入）
├── nana_gateway_client.py    # CLI 客户端（仿 image_gateway_client.py）
└── tests/
    └── test_selectors.py     # 选择器验证脚本
```

### requirements.txt

```
playwright>=1.40
fastapi>=0.100
uvicorn>=0.23
python-multipart>=0.0.6
```

### 分步实现计划

**Phase 1：DOM 探查 + 选择器确认（1-2 小时）**
1. 打开 Flow 页面，用 DevTools 抓取所有选择器
2. 填入 `selectors.json`
3. 写一个简单的 `test_selectors.py` 验证每个选择器能命中目标元素

**Phase 2：Playwright 自动化核心（2-4 小时）**
1. 实现 `generate_in_flow()` — 输入 prompt → 点击生成 → 等待结果 → 获取图片
2. 先用 headed 模式调试，确认每步操作正确
3. 加入网络拦截作为备用图片获取方案
4. 处理 blob URL / data URI 等不同图片返回格式

**Phase 3：API 层（1-2 小时）**
1. 实现 FastAPI server（generate / result / cancel / health）
2. 实现 asyncio.Queue 串行任务队列
3. 实现 job 状态管理（pending → running → done/error）
4. 图片存储和 HTTP 服务

**Phase 4：客户端 CLI（1 小时）**
1. 仿照 `tools/image2/image_gateway_client.py` 写 `nana_gateway_client.py`
2. 支持 generate / health / download 命令
3. 支持 `--output` 下载图片到指定路径

**Phase 5：防检测 + 稳定性（1-2 小时）**
1. 加入反自动化检测脚本
2. 模拟人类打字速度和随机等待
3. 加入登录检测 + 截图调试
4. headless 模式测试

**Phase 6：集成（可选）**
1. 在 Asset Pipeline 中加入 nana 后端选项
2. 或写一个简单的 Web UI 用于团队使用

### 已知风险和缓解

| 风险 | 概率 | 缓解 |
|---|---|---|
| Google 更新 Flow UI 导致选择器失效 | 高 | selectors.json 集中管理，更新成本低 |
| Google 检测到自动化行为 | 低 | CDP 模式 + 真实 Chrome + 人类模拟 |
| 单次生成耗时过长（>2 分钟）| 中 | 可配置超时 + 截图保存用于调试 |
| 登录 session 过期 | 低 | LoginPageError 检测 + 通知用户重新登录 |
| blob URL 无法直接下载 | 中 | FileReader + data URI 转换，或网络拦截 |
| Flow 页面有 reCAPTCHA | 低 | CDP 模式使用真实浏览器，通常不触发 |

### 与 image2 的切换

对上层系统（Asset Pipeline / ComfyUI 后处理）而言，只需要改 base URL：

```python
# 使用 image2 (gpt-image-2)
BASE = "http://192.168.2.184:3200"
POST {BASE}/generate

# 使用 nana gateway (Nana Banana)
BASE = "http://localhost:3201"
POST {BASE}/api/v1/nana/generate
```

客户端 CLI 用法对比：

```powershell
# image2
python tools/image2/image_gateway_client.py generate --prompt "..." --output result.png

# nana (接口一致)
python tools/nana_gateway/nana_gateway_client.py generate --prompt "..." --output result.png
```
