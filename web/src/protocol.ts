// 舞台协议信封（与 src/brawl/stage/protocol.py 对应）：客户端是纯渲染函数，
// 没有 reducer、没有本地状态折叠，收到 StateMsg 画什么是什么。
// 玩法指令/Scene 形状在 ./games/<game>.ts（与 src/brawl/games/<game>/protocol.py 对应）。

import type { AvalonDirective, AvalonScene } from "./games/avalon";
import type { WerewolfDirective, WerewolfScene } from "./games/werewolf";

export interface HostSpeech { kind: "host"; name: string; beat: string; text: string; audio_url: string | null }
export interface NarrationCue {
  name: string;
  beat: string;
  text: string;
  audio_url: string | null;
  at: number;
  duration: number;
}
export interface AudioTrack {
  kind: "main" | "narration" | "sfx" | string;
  audio_url: string | null;
  start: number;
  duration: number;
  volume: number;
  text: string;
  name: string | null;
  beat: string | null;
}

export type Directive = AvalonDirective | WerewolfDirective;
export type Scene = AvalonScene | WerewolfScene;

export interface FeedItem {
  seq: number;
  directive: Directive;
  started_at: number;
  duration: number;
  clock_running: boolean;
  speed: number;
  content_at: number;
  narrations: NarrationCue[];
  tracks: AudioTrack[];
}

export interface StateMsg {
  type: "state";
  ts: number; // 服务端当前时刻（epoch 秒），客户端对钟用
  phase: "playing";
  scene: Scene;
  prefetch: string[]; // 接下来几拍的音频 URL，提前预热进缓存
}
