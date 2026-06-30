// 音效 cue 表 + 直播页播放调度。
//
// cuesFor 必须与后端各玩法 plugin.py 的 cues 保持同一套映射——
// 跨语言没法共享代码，改一处就同步改另一处。音效文件由 Vite 从 web/public/sfx/ 托管在 /sfx/<name>.opus。
//
// 播放对钟与人声 <Audio> 同一套：用 elapsedOf(started_at) 把 cue 的拍内偏移换算成"还差多少秒
// 触发"，暂停/重连/中途进场都按服务端时间轴对齐。中途进场时已过去太久的 cue 不补放（LATE_GRACE），
// 免得一进来糊一脸历史音效。

import { useEffect } from "react";
import type { Directive, FeedItem } from "../protocol";
import { type Channel, elapsedOf } from "../useChannel";

const SFX_BASE = "/sfx/";
const SFX_VOLUME = 0.7; // 统一压在人声之下（与 server/sfx.py 的 SFX_VOLUME 同值）
const LATE_GRACE = 0.4; // 拍内偏移已过去超过这么久就不补放

export interface Cue { sfx: string; at: number } // at = 拍内偏移秒

// 用到的全部音效名（中途进场时一次性预热进 HTTP 缓存，避免首次触发的网络抖动）
const ALL: string[] = [
  "open", "think", "stamp", "vote-pass", "vote-fail",
  "quest-success", "quest-fail", "assassin-hit", "assassin-miss", "win-good", "win-evil",
];

export function cuesFor(d: Directive, duration: number): Cue[] {
  switch (d.kind) {
    case "game_open": return [{ sfx: "open", at: 0 }];
    case "deal": return [];  // 发牌不配音效（"咔咔"声刺耳）——画面照常飞牌，AvalonPlugin.cues 同此
    case "speech": return d.monologue ? [{ sfx: "think", at: 0 }] : [];
    case "vote_reveal": {
      // 逐票"啪"盖章 + 结果音；错峰节奏与 Stage.tsx 的 --vote-stagger 同步
      const n = d.votes.length;
      const stagger = (duration * 0.62) / Math.max(n, 1);
      const cues: Cue[] = d.votes.map((_, i) => ({ sfx: "stamp", at: i * stagger }));
      cues.push({ sfx: d.approved ? "vote-pass" : "vote-fail", at: n * stagger });
      return cues;
    }
    case "quest_reveal": return [{ sfx: d.result === "success" ? "quest-success" : "quest-fail", at: 0 }];
    case "assassination": return [{ sfx: d.hit ? "assassin-hit" : "assassin-miss", at: 0 }];
    case "game_over": return [{ sfx: d.winner === "good" ? "win-good" : "win-evil", at: 0 }];
    case "ww_game_open": return [{ sfx: "open", at: 0 }];
    case "ww_deal": return [];
    case "ww_night_start": return [];
    case "ww_wolf_kill": return [{ sfx: "assassin-hit", at: duration * 0.6 }];
    case "ww_seer_check": return [];
    case "ww_witch_action": return [];
    case "ww_death_resolved": return [{ sfx: d.deaths.length ? "quest-fail" : "quest-success", at: 0 }];
    case "ww_role_reveal": return [];
    case "ww_idiot_reveal": return [];
    case "ww_speech": return d.monologue ? [{ sfx: "think", at: 0 }] : [];
    case "ww_vote_reveal": {
      const n = d.votes.length;
      const stagger = (duration * 0.62) / Math.max(n, 1);
      const cues: Cue[] = d.votes.map((_, i) => ({ sfx: "stamp", at: i * stagger }));
      cues.push({ sfx: d.exiled !== null ? "vote-fail" : "vote-pass", at: n * stagger });
      return cues;
    }
    case "ww_hunter_shot": return [{ sfx: d.target_no !== null ? "assassin-hit" : "assassin-miss", at: 0 }];
    case "ww_game_over": return [{ sfx: d.winner === "good" ? "win-good" : "win-evil", at: 0 }];
    case "host":
    case "intermission": return [];
  }
}

// 每当切到新一拍，按 cue 表排程这拍的音效。挂在 Stage 顶层，跟随 last.seq 重置。
export function useSfx(ch: Channel, last: FeedItem | undefined): void {
  useEffect(() => {
    for (const name of ALL) fetch(`${SFX_BASE}${name}.opus`).catch(() => {});
  }, []);

  const seq = last?.seq;
  useEffect(() => {
    if (!last) return;
    const cues = cuesFor(last.directive, last.duration);
    if (cues.length === 0) return;
    const elapsed = elapsedOf(ch, last.started_at, Date.now());
    const timers = cues.flatMap((c) => {
      const delay = c.at - elapsed;
      if (delay < -LATE_GRACE) return []; // 已过去太久，不补放
      return [window.setTimeout(() => {
        const el = new window.Audio(`${SFX_BASE}${c.sfx}.opus`);
        el.volume = SFX_VOLUME;
        el.play().catch(() => {});
      }, Math.max(delay, 0) * 1000)];
    });
    return () => { for (const t of timers) window.clearTimeout(t); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seq]);
}
