# stage：演出层

把 `games` 冻好的 replay 快照回放成直播画面。一个 compose 栈 = 一个频道。两个入口都靠
**URL 里的 `?token=`** 准入（复制带 token 的链接即用，零输入框），各用一个独立 token：

- 观众画面（给 OBS）：`https://<域名>/?token=<STAGE_TOKEN>#/stage`
- 导播台（给你）：`https://<域名>/?token=<CONSOLE_TOKEN>#/console`

两 token 分离 = 权限隔离：舞台链接泄漏给观众也只能看、不能控场。

```
replay 目录们（文件系统） → FastAPI 演出引擎（播放头/队列/overlay，状态全在服务端）
                              ├─ WS /ws：先快照后增量，刷新即重建
                              ├─ /#/stage   观众画面（1080×1920 竖屏，OBS 捕获这个）
                              └─ /#/console 导播台（播停/跳局/排片/节奏/overlay）
```

## 本机开发

```bash
cd stage && uv sync && (cd web && npm install)
# 终端1：后端
BRAWL_STAGE_REPLAY_ROOT=/tmp/stage-dev/replays \
BRAWL_STAGE_STAGE_TOKEN=dev-stage \
BRAWL_STAGE_CONSOLE_TOKEN=dev-token \
uv run python -m server.app
# 终端2：前端热更（注意 ?token= 在 hash 前）
cd web && npm run dev   # 打开 http://localhost:5173/?token=dev-token#/console
uv run pytest -q
```

## 起栈（生产/调试并行）

```bash
cp .env.live.example .env.live   # 填真值；.env.debug 同理
# profiles 写在 env 文件的 COMPOSE_PROFILES 里（默认 app,edge；不要公网就改成 app）
docker compose --env-file .env.live  up -d --build
docker compose --env-file .env.debug up -d --build
```

edge = 本栈自己的 Caddy（Cloudflare DNS-01 签证书）+ frpc 外连 frp 中继（与
wangzonghao 共用中继；443 已被它占用，本栈走 `FRP_REMOTE_PORT` 如 8443，访问
`https://stage.example.com:8443`）。

## OBS

OBS 跑在外面的电脑上（别在 N100 上编码推流），加"浏览器源"指向 stage URL，
画布 1080×1920。声音勾"通过 OBS 控制音频"。

## 发布资源包

一局 replay 目录同时也是发布资源目录。各消费端都以同一个目录为输入，并把派生产物写回目录内：

```bash
# games 项目：公开台词配音，生成 tts/*.opus + tts/manifest.json
cd games && uv run brawl avalon-tts /path/to/replay


# stage 项目：离线录制舞台画面，生成 video.mp4
cd stage && uv run stage-record /path/to/replay

# stage 项目：用 stage 背景图 + title.txt 生成封面，生成 covers/{4-3,3-4,16-9}.png
cd stage && uv run stage-cover /path/to/replay
```

`stage-record` 不再接收输出路径，固定写 `<replay_dir>/video.mp4`；文件已存在时会在开工前询问是否覆盖。
`stage-cover` 默认读取 `<replay_dir>/title.txt` 的两行文案和 `<replay_dir>/avatars/1..10.{png,webp,jpg,jpeg}`，固定写 `<replay_dir>/covers/` 下三张比例图；文件已存在时加 `--force` 覆盖。

## 将来接礼物

- 礼物/平台对接：写一个自动调 `POST /api/overlays` 的 worker 即可，overlay 抽象不动。
