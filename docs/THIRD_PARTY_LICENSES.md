# 第三方组件与开源协议

本软件只使用允许闭源商用的开源组件（MIT / BSD / Apache / PSF / MPL 等），**不使用 AGPL / GPL**。
版本以 `requirements.txt` 为准（全部固定版本）。软件内“关于”页面也列出了同样的清单（`web/licenses.json`）。

分发时需保留各组件的版权声明与协议文本（BSD / MIT / Apache 均要求）；第 5 步打包时应把各包 `*.dist-info/` 中的 LICENSE 文件一并放入安装包的 `licenses/` 目录。

## 直接依赖

| 组件 | 版本 | 协议 | 用途 |
|---|---|---|---|
| fastapi | 0.143.0 | MIT | 本地 HTTP 接口 |
| uvicorn | 0.54.0 | BSD-3-Clause | 本地 Web 服务（只监听 127.0.0.1） |
| python-multipart | 0.0.32 | Apache-2.0 | 上传 PDF |
| anthropic | 1.13.0 | MIT | Claude 官方 SDK |
| openai | 3.29.0 | Apache-2.0 | OpenAI 兼容接口（OpenAI / DeepSeek / 通义千问 / Kimi / Gemini / Ollama） |
| scrapling[fetchers] | 0.4.15 | BSD-3-Clause | 网页爬虫（需保留版权声明） |
| markdownify | 1.2.3 | MIT | 网页转 Markdown（Scrapling 调用） |
| cryptography | 50.0.2（Intel Mac：48.0.1，49 起没有 Intel Mac 安装包） | Apache-2.0 OR BSD-3-Clause | 本地加密 API Key（Fernet） |
| pypdf | 6.20.1 | BSD-3-Clause | 读取 PDF 产品目录 |
| Telethon | 1.45.0 | MIT | Telegram 账号池 / 监控 / 通知号（第 2 步） |
| python-socks | 3.1.1 | Apache-2.0 | Telethon 走 SOCKS5 / HTTP 代理（每个 TG 账号独立代理） |
| openpyxl | 3.1.5 | MIT | 生成 Excel（每日推送、线索库导出） |
| onnxruntime | 1.31.0（Intel Mac：1.23.2） | MIT | 本机语义相似度（运行 ONNX 模型，不联网、不花 API 费用） |
| coloredlogs | 15.0.1 | MIT | 仅 Intel Mac：onnxruntime 1.23.2 的依赖 |
| humanfriendly | 10.0 | MIT | 仅 Intel Mac：coloredlogs 的依赖 |
| sympy | 1.14.0 | BSD-3-Clause | 仅 Intel Mac：onnxruntime 1.23.2 的依赖 |
| mpmath | 1.3.0 | BSD-3-Clause | 仅 Intel Mac：sympy 的依赖 |
| tokenizers | 0.23.3 | Apache-2.0 | 语义模型的分词器 |
| numpy | 2.5.4 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | 向量计算 |
| huggingface_hub | 2.2.0 | Apache-2.0 | 首次使用时下载语义模型（固定版本 + SHA-256 校验） |
| playwright | 1.63.0 | Apache-2.0 | 官网登录后只读：弹出真实浏览器窗口让客户自己登录（第 2 步起为直接依赖） |

## 间接依赖

| 组件 | 版本 | 协议 | 备注 |
|---|---|---|---|
| annotated-doc | 0.0.5 | MIT | |
| annotated-types | 0.8.0 | MIT | |
| anyio | 4.15.1 | MIT | |
| apify_fingerprint_datapoints | 0.15.0 | Apache-2.0 | Scrapling 浏览器指纹数据 |
| beautifulsoup4 | 4.15.0 | MIT | |
| browserforge | 1.2.4 | Apache-2.0 | |
| certifi | 2026.7.22 | MPL-2.0 | 文件级弱 copyleft：不修改其文件即可闭源商用 |
| cffi | 2.1.1 | MIT-0 | |
| click | 8.5.0 | BSD-3-Clause | |
| cssselect | 1.6.0 | BSD-3-Clause | |
| curl_cffi | 0.16.3 | MIT | Scrapling 静态抓取 |
| docstring_parser | 0.18.0 | MIT | |
| et_xmlfile | 2.0.0 | MIT | openpyxl 依赖 |
| filelock | 4.1.1 | MIT | huggingface_hub 依赖 |
| flatbuffers | 25.12.19 | Apache-2.0 | onnxruntime 依赖 |
| fsspec | 2026.9.0 | BSD-3-Clause | huggingface_hub 依赖 |
| greenlet | 3.5.6 | MIT AND PSF-2.0 | |
| h11 | 0.16.0 | MIT | |
| hf-xet | 1.7.0 | Apache-2.0 | huggingface_hub 下载加速 |
| httpcore2 | 2.13.1 | BSD-3-Clause | anthropic / openai SDK 的 HTTP 层 |
| httpx2 | 2.13.1 | BSD-3-Clause | 同上 |
| idna | 3.20 | BSD-3-Clause | |
| jiter | 0.17.0 | MIT | |
| lxml | 6.1.3 | BSD-3-Clause | |
| msgspec | 0.22.0 | BSD-3-Clause | |
| opentelemetry-api | 1.45.1 | Apache-2.0 | |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | |
| orjson | 3.13.0 | MPL-2.0 AND (Apache-2.0 OR MIT) | 同 certifi，不修改即可商用 |
| patchright | 1.63.0 | Apache-2.0 | 仅隐身模式使用 |
| playwright | 1.63.0 | Apache-2.0 | 仅隐身 / 动态页面使用；浏览器本体需另行下载（`scrapling install`） |
| Protego | 0.7.0 | BSD-3-Clause | robots.txt 解析 |
| protobuf | 7.36.2 | BSD-3-Clause | onnxruntime 依赖 |
| pyaes | 1.6.1 | MIT | Telethon 依赖 |
| pyasn1 | 0.6.4 | BSD-2-Clause | |
| pycparser | 3.11 | BSD-3-Clause | |
| pydantic | 2.14.1 | MIT | |
| pydantic_core | 2.50.1 | MIT | |
| pyee | 13.0.1 | MIT | |
| PyYAML | 6.0.3 | MIT | huggingface_hub 依赖 |
| rsa | 4.9.1 | Apache-2.0 | Telethon 依赖 |
| six | 1.17.0 | MIT | |
| sniffio | 1.3.1 | MIT OR Apache-2.0 | |
| soupsieve | 3.0.3 | MIT | |
| starlette | 1.8.0 | BSD-3-Clause | |
| tqdm | 4.70.1 | MPL-2.0 AND MIT | huggingface_hub 依赖；MPL 文件级，不修改即可闭源商用 |
| tld | 0.13.2 | MPL-1.1 OR GPL-2.0-only OR LGPL-2.1-or-later | **三选一授权，本软件选用 MPL-1.1**（不是 GPL）；Scrapling 依赖 |
| truststore | 0.10.5 | MIT | |
| typing-inspection | 0.4.4 | MIT | |
| typing_extensions | 4.16.0 | PSF-2.0 | |
| w3lib | 2.5.0 | BSD-3-Clause | |

## 模型与浏览器（不随源码分发的大文件）

| 组件 | 版本 | 协议 | 说明 |
|---|---|---|---|
| Qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q | revision `faf4aa4225822f3bc6376869cb1164e8e3feedd0` | Apache-2.0 | 本机多语言语义模型（约 235 MB，ONNX 量化版）。基于 sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2（Apache-2.0）。首次使用时在后台下载并做 SHA-256 校验；第 5 步打包时可放进安装包（需附 Apache-2.0 协议文本与 NOTICE） |
| Chromium / Chrome for Testing（Playwright 下载，revision 1243）+ chromium-headless-shell + ffmpeg | 跟随 playwright 1.63.0 | BSD-3-Clause 及其组件协议 | **不打包进安装包**（第 5a 步）：安装包第一次打开时从 Playwright 官方 CDN 后台下载到数据目录/ms-playwright（约 550 MB 解压后；带进度、自动重试 3 次、可手动重试）；下载完成前“登录官网后读取”和“隐身模式”提示稍等 |

## 桌面窗口（第 5a 步，运行时依赖，随安装包分发）

| 组件 | 版本 | 协议 | 用途 |
|---|---|---|---|
| pywebview | 6.2.1 | BSD-3-Clause | 软件在自己的窗口里打开（Mac 用系统 WKWebView，Windows 用系统 WebView2） |
| bottle | 0.13.4 | MIT | pywebview 依赖 |
| proxy_tools | 0.1.0 | MIT | pywebview 依赖 |
| pyobjc-core / pyobjc-framework-Cocoa / Quartz / Security / UniformTypeIdentifiers / WebKit | 12.2.2 | MIT | 仅 Mac：pywebview 调用系统窗口和 WebKit；菜单栏图标（0.2.2，PRD 9.0.1）直接用 PyObjC 调系统 NSStatusItem，和窗口共用主线程 |
| pythonnet | 3.2.1 | MIT | 仅 Windows：pywebview 调用 WebView2；系统托盘图标（0.2.2）用它调用 .NET 自带的 System.Windows.Forms.NotifyIcon（系统组件，不随安装包分发） |
| clr_loader | 0.3.1 | MIT | 仅 Windows：pythonnet 依赖 |
| Microsoft Edge WebView2 Runtime | 系统自带 | 微软运行库条款 | Windows 10/11 自带，不随安装包分发 |
| 菜单栏 / 托盘图标 | — | — | 没有引入新库（没有用 pystray：LGPL-3.0）；图标由 brand/logo.svg 生成（packaging/make_icons.py --tray → web/tray/） |
| Python 运行时（python-build-standalone 3.12.15 / GitHub Actions 的 Python 3.12） | 3.12 | PSF-2.0（内含 OpenSSL Apache-2.0、SQLite 公有领域、zlib、libffi MIT 等） | PyInstaller 把 Python 解释器打进安装包 |

## 打包工具（只在构建时使用，不随源码分发；PyInstaller 的启动器会进入安装包）

| 工具 | 版本 | 协议 | 说明 |
|---|---|---|---|
| PyInstaller | 6.22.3 | GPL-2.0-or-later **附打包例外** | 例外条款明确允许用它打包和分发非 GPL（含商业闭源）软件；进入安装包的只有它的启动器（bootloader，同样适用例外） |
| pyinstaller-hooks-contrib | 2026.8 | GPL-2.0 / Apache-2.0（运行时 hook 为 Apache-2.0） | 构建时使用 |
| altgraph / macholib | 0.17.5 / 1.16.4 | MIT | PyInstaller 依赖（Mac） |
| dmgbuild | 1.6.7 | MIT | 生成 Mac 的 .dmg（拖到“应用程序”窗口） |
| ds_store / mac_alias | 1.3.3 / 2.2.3 | MIT | dmgbuild 依赖 |
| Pillow | 12.3.0 | MIT-CMU（HPND） | 生成图标和 DMG 背景 |
| Inno Setup | 6.x | Inno Setup License（免费，可用于商业软件，需保留版权声明） | 生成 Windows 安装程序（GitHub Actions 上运行）；中文界面翻译文件 packaging/windows/ChineseSimplified.isl 取自 Inno Setup 官方仓库 jrsoftware/issrc 标签 is-6_4_3（Files/Languages/Unofficial，维护者 Zhenghan Yang），按 Inno Setup License 原样随源码保存（构建时不再下载） |
| uv | 0.13.0 | MIT OR Apache-2.0 | 本机下载可移植的 Python 3.12（python-build-standalone，MPL-2.0 构建脚本 + PSF Python） |
| macOS hdiutil / codesign / iconutil / qlmanage | 系统自带 | Apple | 生成 DMG、ad-hoc 签名、图标 |

## 外部服务（不是代码依赖，由客户自备账号与 Key）

Telegram（MTProto，客户自己的 api_id / api_hash 和账号）、OpenRouter 及其他 OpenAI / Anthropic 兼容中转站、Anthropic Claude API、OpenAI / DeepSeek / 通义千问 / Kimi / Google Gemini API、Ollama（本地，MIT）、OpenRouter Decisions API（判断模型）、Serper / Brave Search API、YouTube Data API v3。各服务的使用条款由客户自行遵守。

## 更新方法

升级依赖后重新生成本清单：在虚拟环境中运行 `pip freeze`，并用 `importlib.metadata` 读取每个包的 `License-Expression` / 分类器核对协议；出现 GPL / AGPL（且无其他可选协议）的包不得引入。
