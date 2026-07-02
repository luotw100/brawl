// 舞台协议形状（v2 "电视信号"）：与 server/protocol.py 的信封
// + server/games/<game>/protocol.py 的玩法字段对应。
// 客户端是纯渲染函数：没有 reducer、没有本地状态折叠，收到 StateMsg 画什么是什么。

export interface AvalonSeatInfo {
  no: number;
  name: string;
  persona: string;
  role: string;
  side: "good" | "evil";
  model: string;
  avatar_url?: string | null;  // 厂商 logo；缺则回退 hash 渐变 + 名字首字
  model_display?: string | null;  // 对外展示的模型名；头像下半透明白小字，缺则不显
}

export interface GameOpen { kind: "game_open"; players: number; good: number; evil: number; seats: AvalonSeatInfo[] }
export interface Deal { kind: "deal"; seats: AvalonSeatInfo[]; leader_no: number; lady_holder: number | null }
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
export interface LadyReveal {
  kind: "lady_reveal";
  holder_no: number;
  target_no: number;
  truth: "good" | "evil";
  text: string;
  monologue: string;
  choose_monologue: string;
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
export interface GameOver { kind: "game_over"; winner: "good" | "evil"; win_reason: string; seats: AvalonSeatInfo[] }
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
export interface Intermission {
  kind: "intermission"; last_winner: string | null; last_reason: string | null; next_ready: boolean;
}

export type AvalonDirective =
  | GameOpen | Deal | Speech | LadyReveal | VoteReveal | QuestReveal | Assassination | GameOver | HostSpeech | Intermission;

export interface WerewolfSeatInfo {
  no: number;
  name: string;
  persona: string;
  role: string;
  side: "good" | "werewolf";
  model: string;
  alive: boolean;
  revealed: boolean;
  can_vote: boolean;
  avatar_url?: string | null;
  model_display?: string | null;
}
export interface WerewolfGameOpen {
  kind: "ww_game_open"; board_name: string; players: number; wolves: number; gods: number; villagers: number;
  seats: WerewolfSeatInfo[];
}
export interface WerewolfDeal { kind: "ww_deal"; seats: WerewolfSeatInfo[] }
export interface WerewolfNightStart { kind: "ww_night_start"; day: number }
export interface WerewolfWolfVote { seat_no: number; target_no: number; speech: string; monologue: string }
export interface WerewolfWolfKill { kind: "ww_wolf_kill"; day: number; target_no: number; votes: WerewolfWolfVote[] }
export interface WerewolfSeerCheck {
  kind: "ww_seer_check"; day: number; seat_no: number; target_no: number; result: "good" | "werewolf";
  monologue: string; audio_url: string | null;
}
export interface WerewolfWitchAction {
  kind: "ww_witch_action"; day: number; seat_no: number; save: boolean; poison_target_no: number | null;
  monologue: string; audio_url: string | null;
}
export interface WerewolfDeathInfo { seat_no: number; role: string; reason: string; day: number }
export interface WerewolfDeathResolved { kind: "ww_death_resolved"; day: number; deaths: WerewolfDeathInfo[] }
export interface WerewolfRoleReveal { kind: "ww_role_reveal"; day: number; seat_no: number; role: string; reason: string }
export interface WerewolfIdiotReveal { kind: "ww_idiot_reveal"; day: number; seat_no: number }
export interface WerewolfSpeech {
  kind: "ww_speech"; day: number | null; phase: "discussion" | "last_words" | "hunter" | "banter";
  seat_no: number; text: string; monologue: string; audio_url: string | null;
}
export interface WerewolfExileVote { seat_no: number; target_no: number; monologue: string }
export interface WerewolfVoteReveal {
  kind: "ww_vote_reveal"; day: number; votes: WerewolfExileVote[]; exiled: number | null; tied: boolean;
}
export interface WerewolfHunterShot {
  kind: "ww_hunter_shot"; day: number; hunter_no: number; target_no: number | null;
  text: string; monologue: string; audio_url: string | null;
}
export interface WerewolfGameOver {
  kind: "ww_game_over"; winner: "good" | "werewolf"; win_reason: string; day: number;
  seats: WerewolfSeatInfo[]; deaths: WerewolfDeathInfo[];
}

export type WerewolfDirective =
  | WerewolfGameOpen | WerewolfDeal | WerewolfNightStart | WerewolfWolfKill | WerewolfSeerCheck
  | WerewolfWitchAction | WerewolfDeathResolved | WerewolfRoleReveal | WerewolfIdiotReveal
  | WerewolfSpeech | WerewolfVoteReveal | WerewolfHunterShot | WerewolfGameOver | HostSpeech | Intermission;

export type Directive = AvalonDirective | WerewolfDirective;

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

export interface FeedItem {
  seq: number;
  directive: Directive;
  started_at: number;
  duration: number;
  speed: number;
  content_at: number;
  narrations: NarrationCue[];
  tracks: AudioTrack[];
}

export interface AvalonScene {
  game: "avalon";
  seats: AvalonSeatInfo[];
  dealt: boolean;
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

export interface WerewolfDeathLog {
  kind: "ww_death"; day: number; seat_no: number; role: string; reason: string;
}
export interface WerewolfVoteLog {
  kind: "ww_vote"; day: number; exiled: number | null; tied: boolean; votes: WerewolfExileVote[];
}
export type WerewolfTimelineEntry = WerewolfDeathLog | WerewolfVoteLog;

export interface WerewolfScene {
  game: "werewolf";
  board_name: string;
  seats: WerewolfSeatInfo[];
  dealt: boolean;
  day: number;
  phase: "opening" | "night" | "day" | "postgame";
  speaking: number | null;
  night_target: number | null;
  last_exiled: number | null;
  winner: string | null;
  win_reason: string | null;
  deaths: WerewolfDeathInfo[];
  feed: FeedItem[];
  timeline: WerewolfTimelineEntry[];
}

export type Scene = AvalonScene | WerewolfScene;

export interface StateMsg {
  type: "state";
  ts: number; // 服务端当前时刻（epoch 秒），客户端对钟用
  phase: "idle" | "playing" | "intermission";
  scene: Scene;
  overlays: OverlayItem[];
  prefetch: string[]; // 接下来几拍的音频 URL，提前预热进缓存
}

// 导播台专属（/api/state，带 token）
export interface LibraryItem {
  id: string;
  game: string;
  tags: string[];
}

export interface ConsoleState extends StateMsg {
  replay_id: string | null;
  queue: string[];
  library: LibraryItem[];
  knobs: { speed: number; intermission_seconds: number; loop_pool: boolean; feed_window: number };
}
