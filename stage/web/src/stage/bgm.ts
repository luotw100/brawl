// 背景音乐 BGM：一条贯穿整场的低音量循环床。
//
// BGM_VOLUME / BGM_PLAYLIST 必须与后端 server/bgm.py 保持同一套——跨语言没法共享代码，改一处
// 就同步改另一处。音乐文件由 Vite 从 web/public/bgm/ 托管在 /bgm/<name>.opus，走 Git LFS。
//
// 与音效/人声不同，BGM 是连续床：按固定顺序连播、放完回到第一首、整场不停（跨多局、幕间也
// 铺着）。只在 idle（未开演）时静默。

import { useEffect, useRef } from "react";

const BGM_BASE = "/bgm/";
export const BGM_VOLUME = 0.16; // 与 server/bgm.py 的 BGM_VOLUME 同值
export const BGM_PLAYLIST = ["crusade", "angevin", "master-of-the-feast", "volatile-reaction"];

// 整场背景床：常驻一个 <audio>，按固定顺序接力换曲（放完下一首、到尾回第一首）；phase 为
// idle 时暂停，其余一直铺着。挂在 Stage 顶层，全场仅一次装配。
export function useBgm(phase: string | undefined): void {
  const elRef = useRef<HTMLAudioElement | null>(null);
  const idxRef = useRef(0);

  useEffect(() => {
    const el = new window.Audio();
    el.volume = BGM_VOLUME;
    el.preload = "auto";
    el.src = `${BGM_BASE}${BGM_PLAYLIST[idxRef.current]}.opus`;
    const onEnded = () => {
      idxRef.current = (idxRef.current + 1) % BGM_PLAYLIST.length;
      el.src = `${BGM_BASE}${BGM_PLAYLIST[idxRef.current]}.opus`;
      el.play().catch(() => {});
    };
    el.addEventListener("ended", onEnded);
    elRef.current = el;
    return () => { el.removeEventListener("ended", onEnded); el.pause(); elRef.current = null; };
  }, []);

  useEffect(() => {
    const el = elRef.current;
    if (!el) return;
    if (phase === "idle") el.pause();
    else el.play().catch(() => {}); // 自动播放被拦就静默失败，与人声/音效同口径
  }, [phase]);
}
