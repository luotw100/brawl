// 协议形状（v2 "电视信号"）：与 server/protocol.py 一一对应。
// 客户端是纯渲染函数：没有 reducer、没有本地状态折叠，收到 StateMsg 画什么是什么。

export interface SeatInfo {
  no: number;
  name: string;
  persona: string;
  role: string;
  side: "good" | "evil";
  model: string;
}

export interface GameOpen { kind: "game_open"; players: number; good: number; evil: number }
export interface Deal { kind: "deal"; seats: SeatInfo[]; leader_no: number; lady_holder: number | null }
export interface Speech {
  kind: "speech";
  phase: "propose" | "discuss" | "lady" | "banter";
  seat_no: number;
  text: string;
  monologue: string;
  quest: number | null;
  attempt: number | null;
  team: number[] | null;
  lady_target: number | null;
  lady_truth: string | null;
  audio_url: string | null;
}
export interface SeatVote { seat_no: number; approve: boolean; monologue: string }
export interface VoteReveal {
  kind: "vote_reveal"; quest: number; attempt: number; votes: SeatVote[]; approved: boolean; rejections: number;
}
export interface QuestCard { seat_no: number; success: boolean; monologue: string }
export interface QuestReveal {
  kind: "quest_reveal"; quest: number; team: number[]; cards: QuestCard[];
  fails: number; fails_required: number; result: "success" | "fail";
}
export interface Assassination {
  kind: "assassination"; assassin_no: number; target_no: number; hit: boolean;
  text: string; monologue: string; audio_url: string | null;
}
export interface GameOver { kind: "game_over"; winner: "good" | "evil"; win_reason: string; seats: SeatInfo[] }
export interface HostSpeech { kind: "host"; name: string; beat: string; text: string; audio_url: string | null }
export interface Intermission {
  kind: "intermission"; last_winner: string | null; last_reason: string | null; next_ready: boolean;
}

export type Directive =
  | GameOpen | Deal | Speech | VoteReveal | QuestReveal | Assassination | GameOver | HostSpeech | Intermission;

export interface OverlayItem {
  id: string;
  kind: "text" | "image" | "marquee";
  content: string;
  x: number;
  y: number;
  width: number | null;
  style: Record<string, string>;
  ttl_seconds: number | null;
}

export interface ProposalLog {
  kind: "proposal"; quest: number; attempt: number; leader_no: number;
  team: number[]; approved: boolean; approves: number[]; rejects: number[];
}
export interface QuestLog {
  kind: "quest"; quest: number; team: number[]; result: "success" | "fail";
  fails: number; fail_seats: number[];
}
export type TimelineEntry = ProposalLog | QuestLog;

export interface FeedItem { seq: number; directive: Directive; started_at: number; duration: number }

export interface Scene {
  seats: SeatInfo[];
  leader_no: number | null;
  lady_holder: number | null;
  quest_no: number;
  attempt: number;
  missions: (string | null)[];
  rejections: number;
  team: number[];
  speaking: number | null;
  winner: string | null;
  win_reason: string | null;
  feed: FeedItem[];
  timeline: TimelineEntry[];
}

export interface StateMsg {
  type: "state";
  ts: number; // 服务端当前时刻（epoch 秒），客户端对钟用
  phase: "idle" | "playing" | "intermission";
  paused: boolean;
  scene: Scene;
  overlays: OverlayItem[];
  prefetch: string[]; // 接下来几拍的音频 URL，提前预热进缓存
}

// 导播台专属（/api/state，带 token）
export interface ConsoleState extends StateMsg {
  replay_id: string | null;
  queue: string[];
  library: string[];
  knobs: { pace: number; intermission_seconds: number; loop_pool: boolean; feed_window: number };
}
