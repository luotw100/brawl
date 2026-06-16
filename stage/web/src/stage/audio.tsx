// 音频播放闸门：处理移动端的自动播放策略。
//
// iOS Safari / Android Chrome 禁止"无用户手势"的带声音播放——programmatic 的 play() 会被
// 静默 reject（连报错都没有）。桌面浏览器和 OBS 浏览器源有"媒体互动"豁免，可直接自动播。
//
// 策略：乐观播放，play() 一旦被拒就升起一次性"点击开启声音"遮罩；用户点一下（产生 user
// activation）即解锁后续所有播放。桌面 / OBS 的 play() 永不被拒，遮罩永不出现，零打扰。
//
// 设计上把"播放一个音频"收成一个 Player 对象走 context 下发，被拒上报也由它内部完成——
// 这样 <Audio>、useSfx 的调用点不出现任何"解锁了没/能不能放"的分叉，照常调 play 即可。

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

// 极短静音 WAV。用途有二：mount 时探测当前是否需要手势；在用户手势回调里"破冰"解锁。
const SILENCE =
  "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA=";

export interface Player {
  // 播放音频；被拒时内部升起解锁遮罩。返回元素供调用方暂停/清理。
  play(url: string, opts?: { currentTime?: number; volume?: number }): HTMLAudioElement;
}

function makeAudio(url: string, opts?: { currentTime?: number; volume?: number }): HTMLAudioElement {
  const el = new window.Audio(url);
  if (opts?.currentTime) el.currentTime = Math.max(opts.currentTime, 0);
  if (opts?.volume != null) el.volume = opts.volume;
  return el;
}

// 默认 Player：无脑尝试播放、吞掉失败。无 Provider 时（如测试）退化成原先的行为。
const bare: Player = {
  play(url, opts) {
    const el = makeAudio(url, opts);
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
    new window.Audio(SILENCE).play().catch(() => {}); // 手势内破冰，解锁后续播放
    setNeedGesture(false);
    setGen((g) => g + 1);
  }, []);

  const player = useMemo<Player>(
    () => ({
      play(url, opts) {
        const el = makeAudio(url, opts);
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

export function AudioUnlock({ onUnlock }: { onUnlock: () => void }) {
  return (
    <button className="audio-unlock" onClick={onUnlock}>
      <span className="audio-unlock-icon">🔊</span>
      <span className="audio-unlock-text">点击开启声音</span>
    </button>
  );
}
