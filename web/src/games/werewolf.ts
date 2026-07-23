// 狼人杀舞台协议形状（与 werewolf/protocol.py 对应）。

import type { FeedItem, HostSpeech } from "../protocol";

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
  | WerewolfSpeech | WerewolfVoteReveal | WerewolfHunterShot | WerewolfGameOver | HostSpeech;

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
