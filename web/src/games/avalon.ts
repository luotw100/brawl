// 阿瓦隆舞台协议形状（与 avalon/protocol.py 对应）。

import type { HostSpeech } from "../protocol";
import type { FeedItem } from "../protocol";

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
  phase: "propose" | "discuss" | "lady" | "banter" | "evil_chat";
  seat_no: number;
  text: string;
  monologue: string;
  position?: "approve" | "reject" | "undecided" | null;
  quest: number | null;
  attempt: number | null;
  team: number[] | null;
  lady_target: number | null;
  lady_truth: string | null;
  audio_url: string | null;
}
export interface LadyCheck {
  kind: "lady_check";
  holder_no: number;
  target_no: number;
  truth: "good" | "evil";
}
export interface LadyChoose {
  kind: "lady_choose";
  holder_no: number;
  target_no: number;
  choose_monologue: string;
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

export type AvalonDirective =
  | GameOpen | Deal | Speech | LadyChoose | LadyCheck | VoteReveal | QuestReveal | Assassination | GameOver | HostSpeech;

export interface ProposalLog {
  kind: "proposal"; quest: number; attempt: number; leader_no: number;
  team: number[]; approved: boolean; approves: number[]; rejects: number[];
}
export interface QuestLog {
  kind: "quest"; quest: number; team: number[]; result: "success" | "fail";
  fails: number; fail_seats: number[];
}
export type TimelineEntry = ProposalLog | QuestLog;

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
