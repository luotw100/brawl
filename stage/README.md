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

## 将来接 TTS / 礼物

- TTS：replay 目录下放 `audio/<call_id>.mp3`，发言指令自动带 `audio_url`，
  文字与音频各播各的（文字保证先打完，见 engine 的 duration 折算）。
- 礼物/平台对接：写一个自动调 `POST /api/overlays` 的 worker 即可，overlay 抽象不动。
