# StrmFlow

StrmFlow 是面向 **OpenList + Emby** 的 STRM 媒体自动化服务。它负责媒体发现、目录整理、百度网盘分享追更、Emby 媒体信息探测，以及不消耗服务器视频流量的 302 直链播放。

## 功能

- FastAPI 管理端，SQLite 持久化，支持登录和运行日志。
- 按“一级目录 → 二级分类 → 媒体资源”选择已存在的 STRM。
- 自动生成 Emby 兼容的 `Season XX/片名 - SxxExx.strm` 结构。
- 自动识别多季资源，同集多文件按 4K、HDR/Dolby Vision、帧率、片源、编码和大小择优。
- 使用百度网盘官方 `bdpan` CLI 检查分享、增量转存并自动同步新集。
- 后台调用 Emby 原生能力提取并持久化时长、容器、分辨率、音视频流等媒体信息。
- 独立 Emby 302 网关：控制请求透明代理，视频请求跳转到 OpenList 解析出的网盘 CDN。
- 企业微信 Webhook 通知：剧集更新和分享链接失效。
- 状态面板：OpenList、Emby、百度账号/容量、SQLite、自动追更和 302 网关。

## 快速部署（Docker Compose）

发布镜像：

```text
ghcr.io/niccfun/strmflow:latest
```

镜像同时支持 `linux/amd64` 和 `linux/arm64`。每个 GitHub Release 会发布以下标签：

- `latest`
- 完整版本，例如 `0.3.0`
- 次版本，例如 `0.3`
- 主版本，例如 `0`

### 1. 获取部署文件

```bash
git clone https://github.com/niccfun/strmflow.git
cd strmflow
cp .env.example .env
```

编辑 `.env`，至少设置：

```env
APP_USER=admin
APP_PASSWORD=请替换为强密码
SESSION_SECRET=请替换为稳定随机值

OPENLIST_URL=http://openlist:5244
OPENLIST_WEB_URL=http://192.168.1.10:5244
OPENLIST_TOKEN=your-openlist-admin-token

EMBY_URL=http://emby:8096
EMBY_WEB_URL=http://192.168.1.10:8096
EMBY_API_KEY=your-emby-api-key
```

生成会话密钥示例：

```bash
openssl rand -hex 32
```

### 2. 创建共享网络并启动

Compose 使用固定外部网络 `strmflow`：

```bash
docker network inspect strmflow >/dev/null 2>&1 || docker network create strmflow
docker compose pull
docker compose up -d
docker compose logs -f strmflow
```

访问：

- 管理端：`http://HOST:18787`
- 302 网关：`http://HOST:18096`

固定部署某个版本：

```bash
STRMFLOW_VERSION=0.3.0 docker compose pull
STRMFLOW_VERSION=0.3.0 docker compose up -d
```

如果 GHCR 包仍为私有，先登录再拉取：

```bash
echo 'GITHUB_TOKEN' | docker login ghcr.io -u GITHUB_USER --password-stdin
```

### 3. 接入 Emby 和 OpenList

让现有容器加入同一网络；容器名需要与 `.env` 中的主机名一致：

```bash
docker network connect strmflow emby
docker network connect strmflow openlist
```

如果 Emby/OpenList 的 Compose 也由你维护，可直接声明外部网络：

```yaml
networks:
  strmflow:
    name: strmflow
    external: true
```

然后把对应服务加入 `strmflow` 网络。后端地址使用容器内网地址，例如 `http://emby:8096`；浏览器跳转地址使用局域网或公网地址，不能填写容器 DNS 名称。

### 4. 首次设置

登录后进入“系统设置”：

1. 设置**只读源 STRM 根目录**，例如 `/temp_strm`。
2. 设置**目标 STRM 根目录**，例如 `/local_media/emby_strm`。
3. 在 OpenList 中确认两个路径均存在，且源、目标不重叠。
4. 如需自动追更，在“百度网盘自动追更”中完成授权并设置转存根目录。
5. 如需 302 播放，在“302 管理”中启用网关，并填写 Emby/OpenList 容器内网地址。

## 升级与回滚

升级到最新版本：

```bash
docker compose pull
docker compose up -d
```

固定或回滚版本：

```bash
STRMFLOW_VERSION=0.3.0 docker compose up -d
```

数据库迁移会在启动时自动执行。生产升级前建议备份 `data/`：

```bash
docker compose stop strmflow
cp -a data "data-backup-$(date +%Y%m%d-%H%M%S)"
docker compose up -d
```

## 目录映射

`bdpan` 只能写入百度网盘 `/apps/bdpan/`。假设：

```text
网盘转存根目录：media
只读源 STRM 根目录：/temp_strm
```

应在 OpenList 中把“我的应用数据/bdpan/media”挂载为 `/temp_strm`。典型结构：

```text
/temp_strm/
└── TV/
    └── 国产剧/
        └── 交锋 (2026)/
            ├── S01E01 4KHDR60FPS.mp4
            └── S01E02 4KHDR60FPS.mp4
```

发布后的目标目录：

```text
/local_media/emby_strm/
└── tv/
    └── 国产剧/
        └── 交锋 (2026)/
            └── Season 01/
                ├── 交锋 (2026) - S01E01.strm
                └── 交锋 (2026) - S01E02.strm
```

StrmFlow 会校验源目录、目标目录以及底层网盘源路径，阻止把生成的 STRM 写回原始视频目录。

## 百度网盘自动追更

Docker 镜像会通过固定提交和 SHA-256 校验后的百度官方安装脚本安装 `bdpan`。官方安装器同时提供 Linux amd64 和 arm64 版本。

工作流程：

1. 检查分享中的原始视频文件，忽略分享内已有的 `.strm`。
2. 首次检查建立基线，不重复转存全部历史文件。
3. 后续按剧集识别新增、漏存和质量升级，只选择每集最佳文件。
4. 提交转存后等待网盘落盘，再触发 OpenList 扫描、STRM 发布和 Emby 刷新。
5. 新集进入串行媒体信息队列，避免并发读取多个网盘视频。

自动检查默认最短周期为 5 分钟并带少量抖动；失败会退避重试。配置、检查状态和待同步状态均保存在 SQLite。

本机开发环境安装和登录：

```bash
./scripts/install-bdpan.sh
./scripts/login-bdpan.sh
```

## Emby 302 网关

完整播放链路：

```text
Emby 客户端
  → StrmFlow 302 网关
  → 读取 Emby STRM 媒体源
  → OpenList /api/fs/link
  → HTTP 302 到网盘 CDN
```

网关仅改写可直连的 `PlaybackInfo` 和视频流地址；播放开始、进度、停止播放、图片、字幕以及 WebSocket 控制流继续透明转发给 Emby，因此播放历史和继续观看仍由 Emby 原生管理。

直链缓存保存在 SQLite。实际缓存时间取配置上限和上游过期时间中的较小值，新集发布后会自动预热。首次媒体探测可能较慢，`PlaybackInfo` 会使用更长的独立超时。

## 媒体信息维护

同步新集后，后台工作器调用 Emby 的 `PlaybackInfo` 探测媒体，随后重新读取 Emby 条目，只有确认信息已被 Emby 持久化后才完成任务。失败按 30 秒、2 分钟和 5 分钟重试。

系统设置支持：

- 手动“立即扫描缺失项”；
- 每天指定时间扫描；
- 按 `APP_TIMEZONE` 计算计划时间；
- 跳过零字节或无效 STRM。

SQLite 的 `media_probes` 表只保存任务状态，不复制 Emby 的媒体技术信息。

## 配置参考

完整默认值见 [`.env.example`](.env.example)。常用配置：

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `HOST` / `PORT` | 管理服务监听地址 | `0.0.0.0` / `8787` |
| `DATABASE_URL` | SQLite 地址 | `sqlite+aiosqlite:///./data/strmflow.db` |
| `OPENLIST_URL` | 后端访问 OpenList 的地址 | `http://openlist:5244` |
| `OPENLIST_WEB_URL` | 浏览器访问 OpenList 的地址 | 空 |
| `EMBY_URL` | 后端访问 Emby 的地址 | `http://emby:8096` |
| `EMBY_WEB_URL` | 浏览器访问 Emby 的地址 | 空 |
| `EMBY_302_PORT` | 302 网关端口 | `18096` |
| `MEDIA_PROBE_ENABLED` | 启用媒体信息队列 | `true` |
| `APP_TIMEZONE` | 调度时区 | `Asia/Hong_Kong` |
| `BDPAN_ENABLED` | 默认启用自动追更 | `false` |
| `BDPAN_SAVE_ROOT` | `/apps/bdpan/` 下的转存根目录 | `StrmFlow` |

路径设置、302、自动追更、媒体信息计划和通知配置可在页面中修改，并保存在 SQLite；页面值优先于对应环境变量默认值。

## 数据与安全

持久化目录：

```text
data/
├── strmflow.db       # 应用数据和运行配置
└── bdpan/            # OAuth 配置和本地密钥
```

- 不要提交 `.env`、`data/` 或 bdpan Token。
- `data/bdpan` 建议仅允许容器运行用户读取。
- OpenList Token 建议使用具备所需路径权限的账户。
- 对公网开放时应在反向代理中启用 HTTPS、访问控制和限流。
- 企业微信 Webhook 在 API 和页面中仅显示掩码，日志不会记录密钥。

## 本地开发

要求 Python 3.12+ 和 [uv](https://docs.astral.sh/uv/)：

```bash
cp .env.example .env
uv sync --all-groups
uv run strmflow
```

质量检查：

```bash
uv run ruff check .
uv run ruff format --check src tests
uv run pytest
```

本地构建镜像：

```bash
docker build -t strmflow:local .
```

## 项目结构

```text
src/strmflow/
├── api/                 # FastAPI 路由和鉴权
├── core/                # 配置、异常、会话和运行日志
├── infrastructure/      # SQLite、SQLAlchemy 和迁移入口
├── migrations/          # Alembic 迁移
├── repositories/        # 媒体、任务和运行配置持久化
├── services/            # OpenList、Emby、bdpan、302、通知和探测服务
├── utils/               # 路径与剧集识别工具
├── web/                 # 管理页面和 SVG 资源
├── container.py         # 依赖装配
└── main.py              # FastAPI 应用工厂
```

## 发布流程

推送 `v*` 标签后，GitHub Actions 会：

1. 使用 uv 安装锁定依赖；
2. 执行 Ruff 和完整测试；
3. 创建 GitHub Release；
4. 使用 QEMU + Buildx 构建 `linux/amd64`、`linux/arm64`；
5. 将多架构镜像和版本标签推送到 GHCR。

## 许可与依赖

百度网盘能力来自官方项目 [baidu-netdisk/bdpan-storage](https://github.com/baidu-netdisk/bdpan-storage)。使用自动转存前，请阅读官方提示并妥善保管授权信息。
