// 频道客户端：连 /ws 收 StateMsg，断线自动重连。客户端零状态——只保存最新一帧。
// receivedAt 用于把服务端时间轴换算到本地：某拍已演出的秒数 =
//   item.clock_running
//     ? (msg.ts - item.started_at) + (Date.now() - receivedAt) / 1000
//     : 0

import { useEffect, useRef, useState } from "react";
import type { FeedItem, StateMsg } from "./protocol";

export interface Channel {
  connected: boolean;
  state: StateMsg | null;
  receivedAt: number; // 本地 Date.now()（毫秒）
}

export function elapsedOf(ch: Channel, item: FeedItem, now: number): number {
  if (!ch.state) return 0;
  if (!item.clock_running) return 0;
  return ch.state.ts - item.started_at + (now - ch.receivedAt) / 1000;
}

export function useChannel(): Channel {
  const [ch, setCh] = useState<Channel>({ connected: false, state: null, receivedAt: 0 });
  const retry = useRef(0);

  useEffect(() => {
    let ws: WebSocket | null = null;
    let closed = false;
    let timer: number;

    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}/ws`);
      ws.onopen = () => { retry.current = 0; };
      ws.onmessage = (e) => {
        setCh({ connected: true, state: JSON.parse(e.data), receivedAt: Date.now() });
      };
      ws.onclose = () => {
        setCh((prev) => ({ ...prev, connected: false }));
        if (!closed) {
          timer = window.setTimeout(connect, Math.min(1000 * 2 ** retry.current, 10000));
          retry.current += 1;
        }
      };
    };
    connect();
    return () => { closed = true; clearTimeout(timer); ws?.close(); };
  }, []);

  return ch;
}
