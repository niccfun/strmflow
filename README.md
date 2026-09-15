# StrmFlow

使用 FastAPI 重构的 OpenList STRM 追更服务。保留原项目的登录、媒体记录、OpenList
扫描、STRM 发布和 Emby 刷新功能，并集成百度网盘官方 `bdpan` CLI 自动追更。

## 项目结构

```text
src/strmflow/
├── api/                 # FastAPI 路由与鉴权依赖
├── core/                # 配置、异常、会话签名
├── domain/              # 领域模型
├── infrastructure/      # SQLite 引擎及 SQLAlchemy ORM
├── migrations/          # Alembic 数据库迁移
├── repositories/        # 媒体记录、转存任务持久化
├── schemas/             # API 请求/响应模型
├── services/
│   ├── openlist.py      # OpenList HTTP 客户端
│   ├── storage.py       # STRM 存储识别与媒体发现
│   ├── media.py         # 追更记录、预览、发布
│   ├── emby.py          # Emby 客户端
│   ├── bdpan.py         # 百度官方 CLI 安全适配器
│   ├── bdpan_automation.py # 分享检查、增量转存与自动同步调度
│   ├── system_status.py # OpenList、Emby、网盘容量与运行状态聚合
│   └── transfers/       # 可插拔转存 Provider
├── web/templates/       # 原项目管理页面
├── container.py         # 服务装配
└── main.py              # FastAPI 应用工厂
```

## 使用 uv 启动

```bash
cp .env.example .env
uv sync --all-groups
uv run strmflow
```

访问 `http://127.0.0.1:8787`。API 文档位于 `/docs`，OpenAPI 描述位于
`/openapi.json`。

运行测试：

```bash
uv run pytest
```

生产启动：

```bash
uv run uvicorn strmflow.main:app --host 0.0.0.0 --port 8787
```

## 最小配置

```env
APP_USER=admin
APP_PASSWORD=change-me
SESSION_SECRET=replace-with-a-long-random-value
OPENLIST_URL=http://127.0.0.1:5244
OPENLIST_TOKEN=your-openlist-admin-token
EMBY_URL=http://127.0.0.1:8096
EMBY_API_KEY=your-emby-api-key
```

其余配置见 [`.env.example`](.env.example)。媒体记录及转存任务保存在本地 SQLite：

```env
DATABASE_URL=sqlite+aiosqlite:///./data/strmflow.db
```

SQLite 已启用 WAL、外键检查和 5 秒 busy timeout。应用启动时会自动执行 Alembic
迁移；也可以手动运行：

```bash
uv run alembic upgrade head
```

### 从旧 JSON 自动迁移

首次启动且 `media_items` 表为空时，应用会尝试读取旧项目的 OpenList 文件：

```text
/local_media/emby-strm/.openlist-strm-sync/media.json
```

导入结果记录在 `app_metadata` 表中，后续启动不会重复导入。旧 JSON 不会被修改或
删除，可继续作为备份。自定义旧文件路径可以设置 `MEDIA_DB_PATH`；关闭导入可以设置：

```env
LEGACY_JSON_IMPORT=false
```

## 页面路径设置

先在设置中指定一个只读源 STRM 根目录。“添加媒体”时不再单独填写源路径，页面会在
该目录下按“一级目录（TV/Movies）→ 二级分类（国产剧等）→ 媒体资源目录”三级选择，
并自动填充标题、年份、源路径和 Emby 目标映射；“重新扫描”可强制刷新 OpenList
目录缓存。打开添加弹窗时只刷新一级目录；选择一级目录后才刷新其二级分类，选择
二级分类后才刷新媒体资源。每次只读取当前层级，不再预先递归扫描整棵目录树。

“添加媒体”提供两个独立入口：

- **已保存媒体**：按上述三级目录选择已经存在于只读源 STRM 根目录中的媒体，保存后
  立即扫描并同步；
- **百度网盘分享**：粘贴分享链接后先检查视频和 STRM 文件；分享中包含多个媒体时可
  选择其中一个，再选择只读源根目录下的一级目录、二级分类并确认片名。系统随后提交
  转存，文件落盘后自动触发 OpenList 扫描、STRM 整理和 Emby 刷新。

分享检查结果只在服务进程内保留 15 分钟，页面拿到的是临时候选编号，不包含百度
`fs_id`。检查和转存接口分别为 `POST /api/bdpan/share/inspect` 与
`POST /api/bdpan/share/import`。

首次保存媒体后会自动执行 OpenList 扫描、STRM 发布和 Emby 媒体库刷新。电视剧会从
`Season 02`、`S02`、`第2季` 等源目录，或 `S02E01`、`2x01` 等文件名中识别季度；
检测到多季时会按 Emby 推荐结构分别发布。只有一个可识别季度时仍优先使用用户填写的
默认季数，无法识别季度时也使用该值。例如：

同一季度同一集出现多个文件时，系统会按集号合并并择优保留一个版本：优先 4K/1080p、
Remux/蓝光/Web-DL、HDR、编码格式和帧率，最后优先没有 `(1)`、`-2`、`副本` 等重复后缀
的规范文件名。低质量或重复文件不会计入集数，也不会复制到目标目录；已存在于目标目录
的历史重复文件会在发布时清理。预览和同步结果会返回被跳过的重复文件清单。

```text
tv/国产剧/交锋 (2026)/
├── Season 01/交锋 (2026) - S01E01.strm
└── Season 02/交锋 (2026) - S02E01.strm
```

登录后可通过左侧导航栏的“系统设置”修改：

- 只读源 STRM 根目录：在该目录下递归发现媒体，只读取和扫描，不写入；
- 目标 STRM 根目录：修改后会同步调整已有媒体的目标目录。

页面设置保存在 SQLite 的 `app_metadata` 表中，优先于 `.env` 中的 `LIST_ROOT` 和
`EMBY_STRM_ROOT`。接口为 `PUT /api/settings/paths`。

服务地址分为两套，不能混用：`OPENLIST_URL`、`EMBY_URL` 是 StrmFlow 后端访问其他
Docker 容器的内网地址；`OPENLIST_WEB_URL`、`EMBY_WEB_URL` 是用户浏览器能够访问的
外网或局域网地址。媒体详情弹窗中的 OpenList 跳转只使用
`OPENLIST_WEB_URL`，后端扫描、同步及 302 网关仍使用内网地址。

左侧“运行日志”会合并服务端 HTTP 访问记录和浏览器操作记录，显示状态码（包括 302
跳转）、客户端、耗时、协议、响应大小及 User-Agent；使用紧凑列表展示，并支持手动
刷新和实时刷新。服务日志接口为 `GET /api/logs`，清空接口为 `DELETE /api/logs`。

应用事件同时输出到标准输出，可直接使用 `docker compose logs -f strmflow` 查看。
除 Uvicorn 访问记录外，还会记录数据库与组件启动、追更调度状态、每个分享的检查开始
和结果、新剧集文件清单、转存任务、OpenList 扫描、STRM 整理发布、Emby 刷新、通知
发送及重试时间。访问令牌、提取码和 Webhook 密钥不会写入日志。

## 状态总览

左侧“状态总览”集中显示以下运行信息，并在页面停留期间每 30 秒自动刷新：

- OpenList 在线状态、版本、响应耗时及全部挂载状态；
- Emby 在线状态、服务器名称、版本、操作系统及响应耗时；
- `bdpan` CLI 状态、百度网盘授权账号和授权有效期；
- 百度网盘已用、总计、剩余容量及使用比例；
- 自动追更调度、媒体与同步数量、转存任务、SQLite 和 302 网关状态；
- OpenList/Emby 内外网地址，以及只读源与目标 STRM 根目录。

由于 `bdpan` CLI 没有容量查询命令，StrmFlow 会读取 bdpan 配置中的
`auth.access_token`：明文 Token 直接使用，`enc:v1:` 格式则使用同目录 `.token_key`
以 AES-256-GCM 解密，然后请求百度官方 `/api/quota`。Token 只存在于该次后端请求，
不会返回页面或写入日志。配置路径默认是 `~/.config/bdpan/config.json`，也兼容
`BDPAN_CONFIG_PATH` 指定文件或目录。状态接口为 `GET /api/status/overview`，附加
`?refresh=1` 可跳过短时缓存并立即刷新全部状态。

## Emby 302 播放网关

左侧“302 管理”可以启停独立端口的 Emby 播放网关，并配置 Emby/OpenList 上游地址、
监听地址、端口、直链缓存时间、缓存数量、上游超时和请求体缓冲上限。配置保存在
SQLite，保存后会动态启动或重启网关，无需重启 StrmFlow 管理服务。

Docker 部署时建议让 StrmFlow、Emby 和 OpenList 加入同一个网络，在 302 管理中直接
填写容器 DNS 地址，例如 `http://emby:8096` 和 `http://openlist:5244`。这两条上游链路
走 Docker 内网，不依赖宿主机映射端口；Emby API Key、OpenList Token 与路径密码仍从
`.env` 读取。

客户端连接网关端口后，STRM 视频流请求会查询 Emby 媒体源，读取 STRM 内的
OpenList 媒体路径，再通过 `/api/fs/link` 解析为网盘 CDN 直链并返回 HTTP 302。
发布时会把 Strm 挂载生成的清单内容直接写入目标 STRM，而不是再写一个指向源
`.strm` 的地址。这样 Emby 看到的是实际 `.mp4`/`.mkv` 媒体路径，可以正常探测时长，
播放进度和继续观看均由 Emby 原生记录。旧版本产生的嵌套 STRM 会在下次同步时自动
升级，并触发一次 Emby 媒体库刷新。
播放快速路径不再预先请求 `/api/fs/get`；每次媒体同步后会在后台预热最新 6 个
STRM 直链。网关会对 `PlaybackInfo` 做最小改写：仅对 OpenList STRM 媒体源强制直连、
禁用转码并把 `DirectStreamUrl` 指向当前网关；随后由 GET 视频流请求完成 CDN 302 跳转。
播放进度、停止播放等其余 HTTP 控制请求会保留方法、查询参数、认证 Header、原始请求体
和上游响应，全部透明代理到 Emby；WebSocket 控制通道也会双向透传。管理页会显示请求量、
302 次数、缓存命中率、反代数量、错误数量和最近跳转记录。

```env
EMBY_302_ENABLED=false
EMBY_302_HOST=0.0.0.0
EMBY_302_PORT=18096
EMBY_302_CACHE_TTL=21600
EMBY_302_CACHE_MAX=1000
EMBY_302_BODY_BUFFER_MAX=1048576
EMBY_302_TIMEOUT_MS=30000
```

管理接口为 `GET/PUT /api/emby302`，缓存清理接口为
`POST /api/emby302/cache/clear`。直链缓存会写入 SQLite，容器重启后继续有效；
实际有效时间取“配置上限”与上游直链过期时间中的较小值，并预留安全余量。

## 百度网盘自动追更

StrmFlow 使用百度网盘官方 `bdpan` CLI 3.8.7 的 `transfer list` 和
`transfer select` 命令。子进程通过参数数组启动，不经过 shell；提取码和会话标识不会
写入命令预览或任务记录。

本机安装和登录：

```bash
./scripts/install-bdpan.sh
./scripts/login-bdpan.sh
```

两个入口会从官方仓库下载固定提交中的脚本，先校验 SHA-256 再执行。Docker 镜像构建时
会自动安装 CLI；网页登录后可在“系统设置 → 百度网盘自动追更”确认官方安全提示、生成
OOB 授权链接并提交网页返回的 32 位授权码。授权码只通过标准输入传给 CLI。Docker
中的授权配置持久化在宿主机 `./data/bdpan`，不要提交该目录。

### 工作方式

1. 在媒体详情中填写百度网盘分享链接；支持链接自带 `?pwd=abcd`，也支持粘贴
   “链接 + 提取码”整段文本。
2. 只有“更新中”且配置了分享链接的媒体会进入队列。首次检查只记录已有视频文件作为
   基线，不会把分享中的全部历史内容再次转存。
3. 后续检查按字符串保存官方返回的 `fs_id`，仅将新增视频按原相对目录分组并提交
   `transfer select`；同一文件提交后立即记入状态，避免异步任务尚未完成时重复提交。
4. 等待文件落盘后，自动触发对应源目录的 OpenList 扫描、STRM 整理发布与 Emby
   媒体库刷新；如果文件尚未出现，会延迟重试同步。

检查器为单任务串行执行，目录翻页和递归查询之间留有间隔；检查周期最短 5 分钟，默认
10 分钟并带 ±10% 抖动。失败会指数退避，遇到官方定义的“已有转存任务”状态至少等待
5 分钟，单次选择数量也可限制。该设计不会并发扫多个分享，也关闭 CLI 自动版本检查。

### 目录映射

`bdpan` 只能写入百度网盘的 `/apps/bdpan/` 应用目录。假设系统设置如下：

```text
网盘转存根目录：StrmFlow
只读源 STRM 根目录：/temp_strm
```

请在 OpenList 中把 `我的应用数据/bdpan/StrmFlow` 挂载为 `/temp_strm`。例如媒体源路径
`/temp_strm/TV/国产剧/交锋 (2026)` 对应的新增文件会转存到：

```text
我的应用数据/bdpan/StrmFlow/TV/国产剧/交锋 (2026)/Season 02/
```

系统级配置和每个媒体的基线、下次检查时间、失败退避、待同步状态均保存在 SQLite 的
`app_metadata` 表中。默认值也可以在 `.env` 中设置：

```env
BDPAN_ENABLED=false
BDPAN_BINARY=bdpan
BDPAN_TIMEOUT=3600
BDPAN_CHECK_INTERVAL_MINUTES=10
BDPAN_SAVE_ROOT=StrmFlow
BDPAN_SETTLE_SECONDS=90
BDPAN_MAX_NEW_ITEMS=20
```

自动追更接口：

```text
GET  /api/bdpan
PUT  /api/bdpan
POST /api/bdpan/check
POST /api/bdpan/items/{item_id}/check
POST /api/bdpan/login/start
POST /api/bdpan/login/complete
POST /api/bdpan/share/inspect
POST /api/bdpan/share/import
```

## 企业微信通知

“系统设置 → 企业微信通知”支持配置企业微信群机器人的 Webhook，并可分别启用：

- 剧集更新：新增剧集完成网盘转存、OpenList 扫描和 STRM 同步后发送；
- 链接失效：百度返回 `errno=13004`（分享失效、取消或不存在）时发送，同一链接在恢复
  前只通知一次。

通知使用企业微信普通 `text` 消息，并通过简洁的小图标区分媒体、更新数量、状态和时间。

只接受企业微信官方 `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...` 地址。
Webhook 密钥保存在 SQLite，API 和页面只返回末四位掩码；运行日志也会隐藏 `key`。
设置页提供测试发送和清除 Webhook 操作。接口如下：

```text
GET  /api/notifications/wecom
PUT  /api/notifications/wecom
POST /api/notifications/wecom/test
```

原有通用转存 Provider 接口继续保留，可用于命令预览和手动异步任务：

```text
POST /api/transfers/preview
POST /api/transfers
POST /api/items/{item_id}/transfer
GET  /api/transfers
GET  /api/transfers/{job_id}
```

实现依据：[百度网盘官方 bdpan-storage](https://github.com/baidu-netdisk/bdpan-storage)、
[官方 Skill 说明](https://github.com/baidu-netdisk/bdpan-storage/blob/main/skills/baidu-drive/SKILL.md)、
[官方命令参考](https://github.com/baidu-netdisk/bdpan-storage/blob/main/skills/baidu-drive/reference/bdpan-commands.md)。

## Docker Compose

```bash
docker network create services-network 2>/dev/null || true
docker compose up -d --build
```

如果 Emby、OpenList 由其他 Compose 项目管理，将它们加入同一网络（将容器名替换为
实际名称）：

```bash
docker network connect services-network emby
docker network connect services-network openlist
```

容器对外默认映射为 `18787:8787` 和 `18096:18096`，宿主机 `./data` 会挂载到容器
`/app/data`；`./data/bdpan` 另外挂载到 CLI 配置目录，以便容器重建后保留授权。

备份数据库前建议先停止容器，或使用 SQLite 在线备份命令：

```bash
sqlite3 data/strmflow.db ".backup 'data/strmflow-backup.db'"
```
