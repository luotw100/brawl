// 导播台：播放控制 / 队列排片 / 节奏旋钮 / overlay 挂撤。
// 准入只认 URL 里的 ?token=（复制带 token 的链接即用，零输入框）；
// 数据走带 token 的 /api/state 轮询（公开 WS 频道上没有运营信息）。

import { useEffect, useRef, useState } from "react";
import type { ConsoleState, OverlayItem } from "../protocol";
import "./console.css";

export default function Console() {
  const token = new URLSearchParams(location.search).get("token") ?? "";
  return <Panel token={token} />;
}

function Panel({ token }: { token: string }) {
  const [st, setSt] = useState<ConsoleState | null>(null);
  const [err, setErr] = useState("");
  const alive = useRef(true);

  // token 走 query：链接复制即用，且 WS/audio 等无法带头的场景统一。
  const withToken = (path: string) =>
    `${path}${path.includes("?") ? "&" : "?"}token=${encodeURIComponent(token)}`;

  const api = async (method: string, path: string, body?: unknown) => {
    const resp = await fetch(withToken(path), {
      method,
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (resp.status === 401) { setErr("token 无效（检查 URL 里的 ?token=）"); throw new Error(); }
    if (!resp.ok) { setErr(`${path} → ${resp.status} ${await resp.text()}`); throw new Error(); }
    setErr("");
    return resp.json();
  };

  const refresh = () => api("GET", "/api/state").then((s) => { if (alive.current) setSt(s); }).catch(() => {});

  useEffect(() => {
    alive.current = true;
    refresh();
    const t = window.setInterval(refresh, 2000);
    return () => { alive.current = false; clearInterval(t); };
  }, []);

  const act = (method: string, path: string, body?: unknown) =>
    api(method, path, body).then(refresh).catch(() => {});

  if (!st) return <div className="console"><h1>🎛️ 导播台</h1><p>加载中…</p></div>;
  const k = st.knobs;
  const libraryById = new Map(st.library.map((item) => [item.id, item]));
  return (
    <div className="console">
      <header>
        <h1>🎛️ 导播台</h1>
        <span className="pill ok">轮询中</span>
        {err && <span className="err">{err}</span>}
      </header>

      <div className="cols">
        <section>
          <h2>播放</h2>
          <div className="row">
            <span className="pill">{st.phase}</span>
            <span>{st.replay_id ?? "—"}</span>
          </div>
          <div className="row">
            <button onClick={() => act("POST", "/api/playback", { action: "step" })}>⏭ 下一拍</button>
            <button onClick={() => act("POST", "/api/playback", { action: "skip" })}>⏭ 跳下一局</button>
          </div>
          <h2>节奏</h2>
          <label>幕间停留 {k.intermission_seconds}s
            <input type="range" min="0" max="120" step="5" defaultValue={k.intermission_seconds}
              onChange={(e) => act("POST", "/api/knobs", { intermission_seconds: Number(e.target.value) })} />
          </label>
          <label>对话窗口 {k.feed_window} 条
            <input type="range" min="1" max="3" step="1" defaultValue={k.feed_window}
              onChange={(e) => act("POST", "/api/knobs", { feed_window: Number(e.target.value) })} />
          </label>
          <label className="row">
            <input type="checkbox" checked={k.loop_pool}
              onChange={(e) => act("POST", "/api/knobs", { loop_pool: e.target.checked })} />
            队列耗尽自动从片库随机补（永不黑屏）
          </label>
        </section>

        <section>
          <h2>队列（即将播出）</h2>
          <ul className="list">
            {st.queue.map((r, i) => (
              <li key={`${r}-${i}`}>
                <span className="replay-line">
                  <span>{i + 1}. {r}</span>
                  {libraryById.get(r) && <ReplayTags game={libraryById.get(r)!.game} tags={libraryById.get(r)!.tags} />}
                </span>
                <span className="ops">
                  <button disabled={i === 0} onClick={() => act("PUT", "/api/queue", { queue: move(st.queue, i, -1) })}>↑</button>
                  <button disabled={i === st.queue.length - 1} onClick={() => act("PUT", "/api/queue", { queue: move(st.queue, i, 1) })}>↓</button>
                  <button onClick={() => act("PUT", "/api/queue", { queue: st.queue.filter((_, j) => j !== i) })}>✕</button>
                </span>
              </li>
            ))}
            {st.queue.length === 0 && <li className="empty">队列为空</li>}
          </ul>
          <h2>片库</h2>
          <ul className="list">
            {st.library.map((item) => (
              <li key={item.id}>
                <span className="replay-line">
                  <span>{item.id}</span>
                  <ReplayTags game={item.game} tags={item.tags} />
                </span>
                <button onClick={() => act("PUT", "/api/queue", { queue: [...st.queue, item.id] })}>＋排片</button>
              </li>
            ))}
          </ul>
        </section>

        <section>
          <h2>overlay 画布</h2>
          <OverlayForm onSubmit={(item) => act("POST", "/api/overlays", item)} />
          <ul className="list">
            {st.overlays.map((o) => (
              <li key={o.id}>
                <span>[{o.kind}] {o.content.slice(0, 30)}</span>
                <button onClick={() => act("DELETE", `/api/overlays/${o.id}`)}>撤下</button>
              </li>
            ))}
            {st.overlays.length === 0 && <li className="empty">画布干净</li>}
          </ul>
        </section>
      </div>
    </div>
  );
}

function ReplayTags({ game, tags }: { game: string; tags: string[] }) {
  return (
    <span className="tags">
      <span className="tag game">{game}</span>
      {tags.map((tag) => <span className="tag" key={tag}>{tag}</span>)}
    </span>
  );
}

function move(arr: string[], i: number, delta: number): string[] {
  const next = [...arr];
  const [x] = next.splice(i, 1);
  next.splice(i + delta, 0, x);
  return next;
}

function OverlayForm({ onSubmit }: { onSubmit: (item: Partial<OverlayItem>) => void }) {
  const [kind, setKind] = useState<OverlayItem["kind"]>("marquee");
  const [content, setContent] = useState("");
  const [x, setX] = useState(50);
  const [y, setY] = useState(90);
  const [ttl, setTtl] = useState("");
  return (
    <div className="ov-form">
      <div className="row">
        <select value={kind} onChange={(e) => setKind(e.target.value as OverlayItem["kind"])}>
          <option value="marquee">滚动条</option>
          <option value="text">悬浮文字</option>
          <option value="image">图片(URL)</option>
        </select>
        <input placeholder={kind === "image" ? "图片 URL" : "文案，如：感谢 @观众 送出的火箭！"}
          value={content} onChange={(e) => setContent(e.target.value)} />
      </div>
      <div className="row">
        <label>x% <input type="number" value={x} onChange={(e) => setX(Number(e.target.value))} /></label>
        <label>y% <input type="number" value={y} onChange={(e) => setY(Number(e.target.value))} /></label>
        <label>秒后自动撤(空=常驻) <input type="number" value={ttl} onChange={(e) => setTtl(e.target.value)} /></label>
        <button disabled={!content}
          onClick={() => { onSubmit({ kind, content, x, y, ttl_seconds: ttl === "" ? null : Number(ttl) }); setContent(""); }}>
          🚀 挂上去
        </button>
      </div>
    </div>
  );
}
