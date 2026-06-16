// 音频播放闸门：处理移动端的自动播放策略。
//
// iOS Safari / Android Chrome 禁止"无用户手势"的带声音播放——programmatic 的 play() 会被
// 静默 reject（连报错都没有）。桌面浏览器和 OBS 浏览器源有"媒体互动"豁免，可直接自动播。
//
// 更坑的是 iOS 的解锁按"元素"计：只有在用户手势里被 play 过的那个 audio 元素才获授权，
// 之后新 new 出来的元素一律仍未授权。所以"每次都 new Audio() 再 play"会导致每换一段声音
// 又被拒、又弹遮罩。对策：预建一个固定的元素池，在用户手势里一次性激活整池，之后所有播放
// 复用池里的元素、绝不新建——一次点击即全程解锁。
//
// 策略：乐观播放，play() 一旦被拒就升起一次性"点击开启声音"遮罩；用户点一下即激活元素池。
// 桌面 / OBS 的 play() 永不被拒，遮罩永不出现，零打扰。把"播放音频"收成 Player 走 context
// 下发，被拒上报也由它内部完成——<Audio>、useSfx 的调用点照常调 play，不出现任何分叉。

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

// 极短静音 WAV。用途：mount 时探测当前是否需要手势（带声音的静音播放在无手势时也会被拒）。
const SILENCE =
  "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA=";

// 池容量：一拍内同时发声的元素数（人声 1 + 盖章连击若干 + 结果音）取上界还留余量。
const POOL_SIZE = 8;

export interface Player {
  // 播放音频；被拒时内部升起解锁遮罩。返回元素供调用方暂停/清理。
  play(url: string, opts?: { currentTime?: number; volume?: number }): HTMLAudioElement;
}

// 复用的元素池（module 单例，stage 单页单实例）。轮转分配，避免新建未授权元素。
let pool: HTMLAudioElement[] = [];
let cursor = 0;

function ensurePool(): void {
  if (pool.length === 0) pool = Array.from({ length: POOL_SIZE }, () => new window.Audio());
}

// 在用户手势回调里调：muted 播一遍再停，给池中每个元素盖上 iOS 的"已授权"章。
function primePool(): void {
  ensurePool();
  for (const el of pool) {
    el.muted = true;
    el.play().then(() => { el.pause(); el.currentTime = 0; el.muted = false; }).catch(() => {});
  }
}

function load(el: HTMLAudioElement, url: string, opts?: { currentTime?: number; volume?: number }): HTMLAudioElement {
  el.pause();
  el.muted = false;
  el.src = url;
  el.currentTime = opts?.currentTime ? Math.max(opts.currentTime, 0) : 0;
  el.volume = opts?.volume ?? 1;
  return el;
}

// 默认 Player：直接新建元素播放、吞掉失败。无 Provider 时（如测试）退化成原先的行为。
const bare: Player = {
  play(url, opts) {
    const el = load(new window.Audio(), url, opts);
    el.play().catch(() => {});
    return el;
  },
};

const PlayerContext = createContext<Player>(bare);
export { PlayerContext };
export const usePlayer = (): Player => useContext(PlayerContext);

// 挂在 Stage 顶层。返回下发用的 player，以及遮罩开关 needGesture / 解锁动作 unlock。
export function useAudioGate(): { player: Player; needGesture: boolean; unlock: () => void } {
  const [needGesture, setNeedGesture] = useState(false);
  // gen 仅用来在解锁后改变 player 身份，触发消费方 effect 重跑，从而按时间轴重新对齐补播。
  const [gen, setGen] = useState(0);

  // 进场即探测：带声音的静音 WAV 在无手势时也会被拒，据此立刻决定要不要升遮罩。
  useEffect(() => {
    new window.Audio(SILENCE).play().catch(() => setNeedGesture(true));
  }, []);

  const unlock = useCallback(() => {
    primePool(); // 手势内激活整池，解锁后续所有播放
    setNeedGesture(false);
    setGen((g) => g + 1);
  }, []);

  const player = useMemo<Player>(
    () => ({
      play(url, opts) {
        const el = load(take(), url, opts);
        el.play().catch(() => setNeedGesture(true));
        return el;
      },
    }),
    // gen 变 → player 换身份 → 消费方 effect 重跑重对齐
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [gen],
  );

  return { player, needGesture, unlock };
}

// 轮转取下一个（已激活的）池元素复用。
function take(): HTMLAudioElement {
  ensurePool();
  const el = pool[cursor];
  cursor = (cursor + 1) % POOL_SIZE;
  return el;
}

export function AudioUnlock({ onUnlock }: { onUnlock: () => void }) {
  return (
    <button className="audio-unlock" onClick={onUnlock}>
      <span className="audio-unlock-icon">🔊</span>
      <span className="audio-unlock-text">点击开启声音</span>
    </button>
  );
}
