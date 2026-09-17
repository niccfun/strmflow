<p align="center">
  <img src="src/strmflow/web/static/strmflow.svg" alt="StrmFlow" width="88" height="88">
</p>

<h1 align="center">StrmFlow</h1>

<p align="center">面向 OpenList 与 Emby 的 STRM 整理、追更和直链播放管理服务。</p>

StrmFlow 通过 Web 管理端连接现有的 OpenList 与 Emby：从指定目录发现媒体，生成 Emby 友好的 STRM 目录结构，并可配合百度网盘官方 `bdpan` CLI 自动追更。启用独立 302 网关后，视频流量会由 OpenList 解析为网盘直链，播放控制请求仍由 Emby 处理。

> StrmFlow 不提供媒体资源，也不替代 OpenList、Emby 或百度网盘。部署前需要准备可访问的 OpenList 实例；使用 Emby 相关功能时还需要 Emby 实例和 API Key。

## 核心功能

- **媒体发现与整理**：按“一级目录 → 二级分类 → 媒体资源”浏览源目录，支持剧集与电影。
- **Emby 目录规范化**：为剧集生成 `Season XX/片名 - SxxExx.strm` 结构，并处理多季资源。
- **质量择优**：同一集存在多个文件时，按 4K、HDR/Dolby Vision、帧率、片源、编码和大小选择优先版本；后续质量升级可作为 Emby 多版本保留。
- **百度网盘自动追更**：检查分享链接、建立初始基线、增量转存新增或更高质量文件，并触发 OpenList 扫描、STRM 发布和 Emby 刷新。
- **Emby 媒体增强**：串行调用 Emby `PlaybackInfo` 补全媒体信息；剧集缺少主图时，可通过 FFprobe/FFmpeg 截取代表帧并上传到 Emby。
- **Emby 302 网关**：透明代理 Emby 控制请求，将可直连的视频请求重定向到 OpenList 返回的网盘 CDN，并提供缓存与预热。
- **运行管理**：提供登录保护、服务状态、运行日志、SQLite 持久化和企业微信机器人通知。

## 工作流程

```text
OpenList 源 STRM 目录
        │
        ├─ 手动选择媒体并发布
        │
百度分享 ── bdpan 增量转存 ── OpenList 扫描
        │
        ▼
StrmFlow 规范化目标 STRM 目录
        │
        ├─ Emby 刷新与媒体增强
        └─ Emby 302 网关 ── OpenList /api/fs/link ── 网盘 CDN
```

运行配置分为两类：

- 登录、服务地址、Token 和数据库地址通过 `.env` 提供；
- 路径、自动追更、302 网关、媒体增强和通知设置在 Web 管理端保存到 SQLite。

## 环境要求

### Docker 部署

- Docker Engine
- Docker Compose v2（使用 `docker compose` 命令）
- 已运行的 OpenList
- 使用 Emby 功能时：已运行的 Emby 和可用的 API Key

官方镜像由 GitHub Actions 构建，支持 `linux/amd64` 和 `linux/arm64`：

```text
ghcr.io/niccfun/strmflow:latest
```

### 本地开发

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- 使用剧集截图功能时，系统 `PATH` 中需要 `ffmpeg` 和 `ffprobe`；Docker 镜像已内置

## 快速开始：预构建镜像

### 1. 准备部署文件

```bash
mkdir -p strmflow
cd strmflow
```

下载环境变量模板：

```bash
curl -fsSL https://raw.githubusercontent.com/niccfun/strmflow/main/.env.example -o .env
```

创建 `compose.yaml`：

```yaml
services:
  strmflow:
    image: ghcr.io/niccfun/strmflow:${STRMFLOW_VERSION:-latest}
    container_name: strmflow
    restart: unless-stopped
    env_file: .env
    environment:
      TZ: Asia/Shanghai
    ports:
      - "18787:8787"
      - "18096:18096"
    volumes:
      - ./data:/app/data
      - ./data/bdpan:/root/.config/bdpan
    extra_hosts:
      - "host.docker.internal:host-gateway"
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
    networks:
      - strmflow

networks:
  strmflow:
    name: strmflow
    external: true
```

### 2. 配置环境变量

编辑 `.env`，至少替换密码和访问凭据：

```env
APP_USER=admin
APP_PASSWORD=请替换为强密码

OPENLIST_URL=http://openlist:5244
OPENLIST_WEB_URL=http://192.168.1.10:5244
OPENLIST_TOKEN=your-openlist-admin-token

EMBY_URL=http://emby:8096
EMBY_WEB_URL=http://192.168.1.10:8096
EMBY_API_KEY=your-emby-api-key
```

`OPENLIST_URL` 和 `EMBY_URL` 的默认值分别为：

```env
OPENLIST_URL=http://openlist:5244
EMBY_URL=http://emby:8096
```

这两个地址使用 Docker 容器 DNS。对应容器需要名为 `openlist`、`emby`，并与 StrmFlow 加入同一个 `strmflow` 网络；如果容器名不同，请同步修改 URL 中的主机名。

`OPENLIST_WEB_URL` 和 `EMBY_WEB_URL` 是浏览器可访问的地址，可使用局域网域名、IP 或反向代理地址，不能填写仅容器内部可解析的服务名。

`SESSION_SECRET` 通常无需配置。首次启动时会自动生成会话签名密钥，并以 `0600` 权限保存到 `data/.session_secret`。

### 3. 创建网络并启动

```bash
docker network inspect strmflow >/dev/null 2>&1 || docker network create strmflow
docker compose pull
docker compose up -d
docker compose logs -f strmflow
```

让现有的 OpenList 和 Emby 容器加入共享网络：

```bash
docker network connect strmflow openlist
docker network connect strmflow emby
```

容器已在该网络中时，重复执行 `docker network connect` 会报已连接，可忽略该步骤。也可以在 OpenList/Emby 自己的 Compose 文件中声明并使用外部网络：

```yaml
networks:
  strmflow:
    name: strmflow
    external: true
```

### 4. 访问管理端

- 管理端：`http://HOST:18787`
- 302 网关：`http://HOST:18096`（需要先在管理端启用）

使用 `.env` 中的 `APP_USER` 和 `APP_PASSWORD` 登录。

### 5. 完成首次设置

进入“系统设置”，依次完成：

1. 设置**只读源 STRM 根目录**，例如 `/temp_strm`。
2. 设置**目标 STRM 根目录**；代码默认值为 `/local_media/emby-strm`。
3. 确认两个目录都能通过 OpenList 访问，且源目录、目标目录及底层原始媒体目录互不重叠。
4. 在媒体列表中选择资源，核对名称、年份、分类和季号后预览并发布。
5. 按需启用“自动追更”“媒体增强”“302 管理”和企业微信通知。

## 使用说明

### 发布 STRM

源目录需要按三层结构组织：

```text
/temp_strm/
└── TV/
    └── 国产剧/
        └── 示例剧 (2026)/
            ├── S01E01.strm
            └── S01E02.strm
```

发布后，目标目录示例：

```text
/local_media/emby-strm/
└── tv/
    └── 国产剧/
        └── 示例剧 (2026)/
            └── Season 01/
                ├── 示例剧 (2026) - S01E01.strm
                └── 示例剧 (2026) - S01E02.strm
```

基本操作流程：

1. 在首页依次选择一级目录、分类和媒体资源。
2. 确认媒体类型、标题、年份、分类、季号和追更状态。
3. 先生成同步预览，检查目标文件名和重复项。
4. 执行发布；完成后可触发 Emby 媒体库刷新。

StrmFlow 会检查路径重叠，避免将生成的 STRM 写回源目录或底层原始视频目录。

### 百度网盘自动追更

Docker 镜像通过固定提交及 SHA-256 校验后的百度官方安装脚本安装 `bdpan`，支持镜像对应的 amd64 和 arm64 平台。

`bdpan` 的转存路径位于百度网盘 `/apps/bdpan/` 下。默认转存根目录为 `media`，即实际网盘路径：

```text
/apps/bdpan/media
```

典型配置方式：在 OpenList 中将“我的应用数据/bdpan/media”对应的媒体树通过 Strm 存储暴露到只读源目录，例如 `/temp_strm`。

自动追更流程：

1. 在“自动追更”中完成百度网盘授权。
2. 设置转存根目录；页面填写相对于 `/apps/bdpan/` 的路径，例如 `media`。
3. 导入或编辑媒体时填写百度网盘分享链接，并将状态设为追更中。
4. 首次检查只建立基线，不重复转存已存在的全部历史文件。
5. 后续检查会选择新增、缺失或质量升级的剧集文件，忽略分享中的 `.strm` 文件。
6. 文件落盘后自动触发 OpenList 扫描、STRM 发布、Emby 刷新和媒体增强队列。

默认检查周期为 10 分钟，允许配置的最短周期为 5 分钟；实际调度带少量随机抖动，失败时会退避重试。自动追更默认关闭。

本地开发环境可使用仓库脚本安装和登录：

```bash
./scripts/install-bdpan.sh
./scripts/login-bdpan.sh
```

### Emby 302 网关

播放链路：

```text
Emby 客户端
  → StrmFlow 302 网关
  → 读取 Emby 中的 STRM 媒体源
  → OpenList /api/fs/link
  → HTTP 302 跳转到网盘 CDN
```

在“302 管理”中配置并启用网关后，将 Emby 客户端的服务器地址改为：

```text
http://HOST:18096
```

网关会透明代理登录、图片、字幕、播放进度、停止播放和 WebSocket 等控制请求。对于可识别的 STRM 视频流，它会先校验 Emby 播放凭据，再请求 OpenList 直链并返回 302；播放历史和继续观看仍由 Emby 管理。

直链缓存保存在 SQLite，缓存时间取配置上限与上游过期时间中的较小值。新集发布后会预热最近剧集的直链。302 网关默认关闭。

### 媒体增强

媒体增强由两部分组成：

- 调用 Emby 原生 `PlaybackInfo` 获取时长、容器、分辨率和音视频流等信息；
- 对没有自身 `Primary` 图片的 `Episode`，读取 STRM 并通过 FFprobe/FFmpeg 生成一张代表帧，再上传到 Emby。

扫描阶段只查询 Emby，并以默认 8 路并发判断缺失项；真正读取网盘视频的任务由单一工作器串行处理，避免同时打开多个远程视频。已有集图片不会被覆盖，零字节或无效 STRM 会被跳过。

管理端支持手动扫描、每日定时扫描、媒体库范围选择、最近扫描批次日志，以及截图位置、宽度、质量和超时设置。默认截图位置为完整时长的 30%，最大宽度为 1920 像素，JPEG 质量参数为 2。

### 企业微信通知

在“系统设置”中填写企业微信群机器人 Webhook，可分别启用：

- 剧集更新通知；
- 分享链接失效通知。

Webhook 在页面和 API 返回中只显示掩码，运行日志不会记录完整密钥。

## 配置参考

### 环境变量

| 变量 | 必需 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `STRMFLOW_VERSION` | 否 | `latest` | Compose 镜像标签；不是应用内部配置。 |
| `HOST` | 否 | `0.0.0.0` | 管理服务监听地址。 |
| `PORT` | 否 | `8787` | 管理服务容器内监听端口。 |
| `APP_USER` | 否 | `admin` | Web 管理端用户名。 |
| `APP_PASSWORD` | 是 | 空 | Web 管理端密码；空值会导致服务启动失败。 |
| `SESSION_SECRET` | 否 | 自动生成 | 显式设置会覆盖 `data/.session_secret` 的自动生成方式。 |
| `OPENLIST_URL` | 否 | `http://openlist:5244` | StrmFlow 后端访问 OpenList 的地址。 |
| `OPENLIST_WEB_URL` | 否 | 空 | 浏览器访问 OpenList 的地址。 |
| `OPENLIST_TOKEN` | 是 | 空 | OpenList API Token；需要支持项目使用的文件及管理接口。 |
| `OPENLIST_PATH_PASSWORD` | 否 | 空 | OpenList 路径设置了访问密码时填写。 |
| `EMBY_URL` | 否 | `http://emby:8096` | StrmFlow 后端访问 Emby 的地址。 |
| `EMBY_WEB_URL` | 否 | 空 | 浏览器访问 Emby 的地址。 |
| `EMBY_API_KEY` | 按需 | 空 | 使用 Emby 刷新、302 或媒体增强功能时需要。 |
| `DATABASE_URL` | 否 | `sqlite+aiosqlite:///./data/strmflow.db` | SQLAlchemy 异步数据库地址。默认使用 SQLite。 |
| `TZ` | 否 | Compose 中为 `Asia/Shanghai` | 定时任务和页面显示使用的运行时区。 |
| `TURNSTILE_SITE_KEY` | 否 | 空 | Cloudflare Turnstile Site Key，需与 Secret 同时配置。 |
| `TURNSTILE_SECRET_KEY` | 否 | 空 | Cloudflare Turnstile Secret Key，需与 Site Key 同时配置。 |

完整的推荐模板见 [`.env.example`](.env.example)。

### Web 管理端配置

| 配置 | 默认值 | 保存位置 |
| --- | --- | --- |
| 只读源 STRM 根目录 | 未设置 | SQLite |
| 目标 STRM 根目录 | `/local_media/emby-strm` | SQLite |
| 百度网盘自动追更 | 关闭 | SQLite |
| 百度网盘转存根目录 | `media` | SQLite |
| 自动检查周期 | `10` 分钟 | SQLite |
| 302 网关 | 关闭 | SQLite |
| 302 网关端口 | `18096` | SQLite |
| 直链缓存上限 | `21600` 秒 | SQLite |
| 每日媒体增强扫描 | 关闭，时间 `03:00` | SQLite |
| 媒体检查并发 | `8` | SQLite |
| 媒体增强队列并发 | `1`（固定串行） | 运行逻辑 |
| 企业微信通知 | 关闭 | SQLite |

Web 页面保存的运行配置会覆盖对应的首次启动默认值。

## 数据目录与安全

持久化数据位于宿主机 `./data`：

```text
data/
├── strmflow.db       # 应用数据、任务状态和运行配置
├── .session_secret   # 自动生成的会话签名密钥，权限为 0600
└── bdpan/            # bdpan OAuth 配置和本地密钥
```

建议：

- 不要提交 `.env`、`data/` 或任何 Token。
- 备份和迁移时完整保留 `data/`，否则登录会话和运行状态会丢失。
- OpenList Token 应限制在完成文件管理和扫描所需的最小权限范围内。
- 对公网开放时，在反向代理层启用 HTTPS、访问控制和限流。
- 除登录接口外，管理 API 默认要求会话或 Basic Auth；生产服务不开放 Swagger、ReDoc 和 OpenAPI 文档入口。
- Compose 示例将 Docker 日志限制为 `10m × 3`，避免日志无限占用磁盘。

## 升级与回滚

升级到最新镜像：

```bash
docker compose pull
docker compose up -d
```

数据库迁移会在服务启动时自动执行。升级前建议备份：

```bash
docker compose stop strmflow
cp -a data "data-backup-$(date +%Y%m%d-%H%M%S)"
docker compose up -d
```

固定版本或回滚到指定版本：

```bash
STRMFLOW_VERSION=0.4.0 docker compose pull
STRMFLOW_VERSION=0.4.0 docker compose up -d
```

如需长期固定版本，也可以直接修改 `.env` 中的 `STRMFLOW_VERSION`。

如果 GHCR 包需要认证，可先登录：

```bash
echo 'GITHUB_TOKEN' | docker login ghcr.io -u GITHUB_USER --password-stdin
```

## 本地开发

安装依赖并启动：

```bash
cp .env.example .env
uv sync --all-groups
uv run strmflow
```

启动前至少需要设置 `APP_PASSWORD` 和 `OPENLIST_TOKEN`。本地没有容器 DNS 时，请把 `OPENLIST_URL`、`EMBY_URL` 改为本机可访问的地址。

质量检查：

```bash
uv run ruff check .
uv run ruff format --check src tests
uv run pytest
```

仓库中的 `compose.yaml` 用于**源码构建**，会使用本地 `Dockerfile`，并自动创建名为 `strmflow` 的网络：

```bash
docker compose up -d --build
docker compose logs -f strmflow
```

仅重启现有构建：

```bash
docker compose up -d
```

## 常见问题

### 服务启动后立即退出

检查日志：

```bash
docker compose logs --tail=200 strmflow
```

`APP_PASSWORD` 或 `OPENLIST_TOKEN` 为空时，运行时配置校验会阻止服务启动。

### OpenList 或 Emby 显示不可用

确认三个容器位于同一网络，并验证容器名与 `.env` 主机名一致：

```bash
docker network inspect strmflow
docker exec strmflow getent hosts openlist
docker exec strmflow getent hosts emby
```

如果服务运行在宿主机或其他网络，请使用实际可达地址替换默认容器 DNS。

### 页面能打开，但浏览器跳转到 OpenList/Emby 失败

后端地址和浏览器地址用途不同：

- `OPENLIST_URL` / `EMBY_URL`：供 StrmFlow 容器访问；
- `OPENLIST_WEB_URL` / `EMBY_WEB_URL`：供用户浏览器访问。

浏览器地址不能使用只在 Docker 网络中可解析的 `openlist` 或 `emby` 主机名。

### 302 网关已启用，但无法播放

依次检查：

1. Emby 与 OpenList 地址在 StrmFlow 容器内可访问；
2. `EMBY_API_KEY` 和 `OPENLIST_TOKEN` 有效；
3. STRM 内容指向 OpenList 可解析的路径；
4. 客户端连接的是 `http://HOST:18096`，而不是原 Emby 端口；
5. 宿主机防火墙和反向代理已放行网关端口。

### 百度网盘自动追更不可用

在“自动追更”页面确认：

- `bdpan` 状态可用且已完成授权；
- 转存根目录位于 `/apps/bdpan/` 下；
- 媒体状态为追更中，并已填写有效分享链接；
- OpenList 中的源目录正确映射到转存目录。

## 项目结构

```text
.
├── src/strmflow/
│   ├── api/                 # FastAPI 路由与鉴权
│   ├── core/                # 配置、安全、错误和运行日志
│   ├── infrastructure/      # SQLite、SQLAlchemy 与迁移入口
│   ├── migrations/          # Alembic 数据库迁移
│   ├── repositories/        # 媒体、任务和运行配置持久化
│   ├── services/            # OpenList、Emby、bdpan、302、通知和媒体增强
│   ├── utils/               # 路径与剧集命名工具
│   ├── web/                 # 管理页面与静态资源
│   ├── container.py         # 依赖装配
│   └── main.py              # FastAPI 应用工厂
├── tests/                   # 测试套件
├── scripts/                 # bdpan 安装与登录脚本
├── compose.yaml             # 本地源码构建 Compose
├── Dockerfile
└── pyproject.toml
```

## 发布与版本

项目版本来自 `pyproject.toml` 和 `src/strmflow/__init__.py`。推送 `v*` 标签后，GitHub Actions 会执行 Ruff、测试、GitHub Release 创建和 amd64/arm64 镜像发布，并生成 `latest`、完整版本、次版本和主版本标签。

## 许可与致谢

本仓库当前未包含独立的 `LICENSE` 文件；使用或分发前请确认项目作者声明的授权范围。

百度网盘能力依赖官方项目 [baidu-netdisk/bdpan-storage](https://github.com/baidu-netdisk/bdpan-storage)。启用自动转存前，请阅读其官方说明并妥善保管授权信息。
