# StrmFlow

使用 FastAPI 重构的 OpenList STRM 追更服务。保留原项目的登录、媒体记录、OpenList
扫描、STRM 发布和 Emby 刷新功能，并通过 Provider 接口预留 `bdpan` 二进制转存能力。

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

首次保存媒体后会自动执行 OpenList 扫描、STRM 发布和 Emby 媒体库刷新。电视剧会从
`Season 02`、`S02`、`第2季` 等源目录，或 `S02E01`、`2x01` 等文件名中识别季度；
检测到多季时会按 Emby 推荐结构分别发布。只有一个可识别季度时仍优先使用用户填写的
默认季数，无法识别季度时也使用该值。例如：

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

## Emby 302 播放网关

左侧“302 管理”可以启停独立端口的 Emby 播放网关，并配置 Emby/OpenList 上游地址、
监听地址、端口、直链缓存时间、缓存数量、上游超时和请求体缓冲上限。配置保存在
SQLite，保存后会动态启动或重启网关，无需重启 StrmFlow 管理服务。

Docker 部署时建议让 StrmFlow、Emby 和 OpenList 加入同一个网络，在 302 管理中直接
填写容器 DNS 地址，例如 `http://emby:8096` 和 `http://openlist:5244`。这两条上游链路
走 Docker 内网，不依赖宿主机映射端口；Emby API Key、OpenList Token 与路径密码仍从
`.env` 读取。

客户端连接网关端口后，STRM 视频流请求会查询 Emby 媒体源，再通过 OpenList 获取
`raw_url` 并返回 HTTP 302。普通请求继续反向代理到 Emby。管理页会显示请求量、302
次数、缓存命中率、反代数量、错误数量和最近跳转记录。

```env
EMBY_302_ENABLED=false
EMBY_302_HOST=0.0.0.0
EMBY_302_PORT=18096
EMBY_302_CACHE_TTL=180
EMBY_302_CACHE_MAX=1000
EMBY_302_BODY_BUFFER_MAX=1048576
EMBY_302_TIMEOUT_MS=30000
```

管理接口为 `GET/PUT /api/emby302`，缓存清理接口为
`POST /api/emby302/cache/clear`。

## bdpan 转存扩展接口

转存业务与 FastAPI 路由解耦，抽象定义在
`services/transfers/base.py`。当前 `BdpanTransferProvider` 使用
`asyncio.create_subprocess_exec` 参数数组启动二进制，不经过 shell。

默认 `BDPAN_ENABLED=false`，此时可以验证最终命令但不会执行：

```bash
curl -u admin:change-me http://127.0.0.1:8787/api/transfers/capabilities

curl -u admin:change-me -H 'content-type: application/json' \
  -d '{"provider":"bdpan","shareUrl":"https://pan.baidu.com/s/xxx","extractCode":"abcd","destination":"/影视/待整理"}' \
  http://127.0.0.1:8787/api/transfers/preview
```

二进制就绪后，在 `.env` 中设置实际参数模板并启用：

```env
BDPAN_ENABLED=true
BDPAN_BINARY=/usr/local/bin/bdpan
BDPAN_TRANSFER_ARGS=["transfer","--share-url","{share_url}","--destination","{destination}","--extract-code","{extract_code}"]
```

创建及查询异步任务（任务状态、输出及失败原因均持久化到 SQLite）：

```text
POST /api/transfers
POST /api/items/{item_id}/transfer
GET  /api/transfers
GET  /api/transfers/{job_id}
```

如果实际 `bdpan` CLI 参数不同，只需调整 `BDPAN_TRANSFER_ARGS`；如果其调用协议不仅是
命令行参数，则新增一个 `TransferProvider` 实现并在 `container.py` 注册即可。

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
`/app/data`。

备份数据库前建议先停止容器，或使用 SQLite 在线备份命令：

```bash
sqlite3 data/strmflow.db ".backup 'data/strmflow-backup.db'"
```
