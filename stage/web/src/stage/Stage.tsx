import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import type {
  AvalonScene, AvalonSeatInfo, FeedItem, Intermission, ProposalLog, QuestCard, QuestLog,
  StateMsg, WerewolfDeathInfo, WerewolfScene, WerewolfSeatInfo,
} from "../protocol";
import { type Channel, elapsedOf, useChannel } from "../useChannel";
import { useBgm } from "./bgm";
import { StageIcon, roleIconName } from "./StageIcon";
import "./stage.css";

declare global {
  interface Window {
    __brawlRecordFirstFramePainted?: (seq: number) => void;
  }
}

const W = 1920;
const H = 1080;

// 发牌节奏（全部按本拍 duration 反推，pace=0 时 duration→0 一切瞬时）：
// 起飞错峰占 0.8·dur，单张飞行时长占 DEAL_FLY_FRAC·dur。
const DEAL_FLY_FRAC = 0.28;
// 从中央牌叠已飞出的张数：纯由时钟反推，发牌台与飞牌共用同一公式，保证两边张数一致。
function dealtCount(elapsed: number, dur: number, n: number): number {
  const stagger = (dur * 0.8) / Math.max(n, 1);
  return Math.min(n, Math.max(0, Math.floor(elapsed / Math.max(stagger, 0.001))));
}

const ROLE: Record<string, string> = {
  merlin: "梅林", percival: "派西维尔", servant: "忠臣",
  assassin: "刺客", morgana: "莫甘娜", mordred: "莫德雷德",
  oberon: "奥伯伦", minion: "爪牙",
};
const PHASE_LABEL: Record<string, string> = {
  propose: "提队", discuss: "发言", lady: "湖中仙女", banter: "赛后复盘",
};
const WIN_REASON: Record<string, [string, string]> = {
  quests: ["蓝方完成 3 个成功任务", "红方破坏满 3 个任务"],
  rejections: ["连续 5 次否决组队", "连续 5 次否决组队"],
  assassination: ["刺客落空", "刺客命中梅林"],
};
const roleLabel = (r: string) => ROLE[r] ?? r;
const sideLabel = (side: string) => side === "evil" ? "红方" : "蓝方";
const winReasonLabel = (winner: string, reason: string) => {
  const pair = WIN_REASON[reason];
  if (!pair) return reason;
  return winner === "good" ? pair[0] : pair[1];
};

const CIRC = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫";
const circ = (n: number) => CIRC[n - 1] ?? `${n}`;
const seatList = (ns: number[]) => ns.map(circ).join("");
const CN = ["零", "一", "二", "三", "四", "五", "六", "七", "八", "九", "十"];
const cn = (n: number) => CN[n] ?? `${n}`;

function hueOf(key: string): number {
  let h = 0;
  for (const c of key) h = (h * 31 + c.charCodeAt(0)) % 360;
  return h;
}
function avatarStyle(persona: string): CSSProperties {
  const hue = hueOf(persona);
  return { background: `linear-gradient(150deg, hsl(${hue} 55% 52%), hsl(${(hue + 36) % 360} 50% 30%))` };
}

// 头像三态共用（座位/揭示/聚光）：有厂商 logo 就铺 logo（深底 contain），否则回退 hash 渐变 + 名字首字。
type AnySeat = AvalonSeatInfo | WerewolfSeatInfo;

const avaClass = (base: string, seat: AnySeat | undefined): string => (seat?.avatar_url ? `${base} has-img` : base);
const avaBg = (seat: AnySeat | undefined): CSSProperties | undefined => (seat && !seat.avatar_url ? avatarStyle(seat.persona) : undefined);
function AvaFill({ seat }: { seat: AnySeat | undefined }) {
  return seat?.avatar_url ? <img className="ava-img" src={seat.avatar_url} alt="" /> : <>{seat?.name.slice(0, 1) ?? "?"}</>;
}

export default function Stage() {
  const ch = useChannel();
  const [scale, setScale] = useState(1);
  useEffect(() => {
    const fit = () => setScale(Math.min(window.innerWidth / W, window.innerHeight / H));
    fit();
    window.addEventListener("resize", fit);
    return () => window.removeEventListener("resize", fit);
  }, []);

  const state = ch.state;
  const scene = state?.scene;
  const last = scene?.feed[scene.feed.length - 1];

  const warmed = useRef<Set<string>>(new Set());
  useEffect(() => {
    for (const url of state?.prefetch ?? []) {
      if (!warmed.current.has(url)) { warmed.current.add(url); fetch(url).catch(() => {}); }
    }
  }, [state?.prefetch]);

  useBgm(state?.phase);
  useEffect(() => {
    if (!window.__brawlRecordFirstFramePainted || state?.phase !== "playing" || !last) return;
    let cancelled = false;
    let secondRaf = 0;
    const seq = last.seq;
    const raf = window.requestAnimationFrame(() => {
      secondRaf = window.requestAnimationFrame(() => {
        if (!cancelled) window.__brawlRecordFirstFramePainted?.(seq);
      });
    });
    return () => {
      cancelled = true;
      window.cancelAnimationFrame(raf);
      window.cancelAnimationFrame(secondRaf);
    };
  }, [state?.phase, last?.seq]);

  if (!state) return <div className="viewport" />;
  // 刺杀的闪屏不在起拍放（那会先于台词剧透生死），改由 AssassinView 在揭晓那一刻自己炸。
  const flashKind = last && ["vote_reveal", "quest_reveal", "game_over", "ww_vote_reveal", "ww_death_resolved", "ww_game_over"].includes(last.directive.kind)
    ? last.directive.kind : null;
  if (scene?.game === "werewolf") {
    return <WerewolfStage ch={ch} state={state} scene={scene} last={last} scale={scale} connected={ch.connected} flashKind={flashKind} />;
  }
  const avalonScene = scene as AvalonScene | undefined;

  return (
    <div className="viewport">
      <div className="canvas" style={{ width: W, height: H, transform: `scale(${scale})` }}>
        <div className="bg" />
        {flashKind && <div key={last!.seq} className={`flash flash-${flashKind}`} />}
        {avalonScene && <TopHud scene={avalonScene} />}
        {avalonScene && <Arena ch={ch} scene={avalonScene} last={last} />}
        {last && <AudioLayer ch={ch} item={last} />}
        {state.phase === "idle" && <Curtain title="稍候开演" sub="导播正在排片…" />}
        {last?.directive.kind === "intermission" && <IntermissionCard d={last.directive} />}
        <div className="overlay-canvas">
          {state.overlays.map((o) => (
            <div key={o.id} className={`ov ov-${o.kind}`}
              style={{ left: `${o.x}%`, top: `${o.y}%`, width: o.width ? `${o.width}%` : undefined, ...o.style }}>
              {o.kind === "image" ? <img src={o.content} alt="" /> : <span>{o.content}</span>}
            </div>
          ))}
        </div>
        {!ch.connected && <div className="conn-dot" title="重连中" />}
      </div>
    </div>
  );
}

function TopHud({ scene }: { scene: AvalonScene }) {
  const good = scene.missions.filter((m) => m === "success").length;
  const evil = scene.missions.filter((m) => m === "fail").length;
  return (
    <div className="hud">
      <div className="hud-left">
        <div className="brand">赛博物种观察日记</div>
      </div>
      <div className="hud-center">
        <div className="mission-track">
          <span className="track-score good"><i>蓝</i>{good}</span>
          <div className="missions">
            {scene.missions.map((m, i) => (
              <div key={i} className={`dot ${m ?? (i + 1 === scene.quest_no ? "current" : "todo")}`}>
                {m === "success" ? "✓" : m === "fail" ? "✕" : ""}
              </div>
            ))}
          </div>
          <span className="track-score evil">{evil}<i>红</i></span>
        </div>
        <Carousel scene={scene} />
      </div>
      <div className="hud-right">
        <div className="show-badge">AI 阿瓦隆</div>
      </div>
    </div>
  );
}

// 前情提要：把全场每个关键节点（每次组队、每次发车）按时序逐条循环播，给中途进来的
// 观众补课。5s 一条，叠帧卷轴：传送带停住让人看清→缓缓上移，旧条从顶卷出、新条从底升入。
const HOLD_MS = 7800;
const SLIDE_MS = 900;
function Carousel({ scene }: { scene: AvalonScene }) {
  const items = buildItems(scene);
  const [idx, setIdx] = useState(0);
  const [sliding, setSliding] = useState(false);
  useEffect(() => {
    let hold: number, slide: number;
    const tick = () => {
      setSliding(true);
      slide = window.setTimeout(() => {
        setSliding(false);
        setIdx((i) => i + 1);
        hold = window.setTimeout(tick, HOLD_MS);
      }, SLIDE_MS);
    };
    hold = window.setTimeout(tick, HOLD_MS);
    return () => { window.clearTimeout(hold); window.clearTimeout(slide); };
  }, [items.length]);
  if (items.length === 0) return <div className="carousel" />;
  const cur = items[idx % items.length];
  const nxt = items[(idx + 1) % items.length];
  return (
    <div className="carousel">
      <span className="carousel-tag">前情提要</span>
      <div className="belt">
        <div className={`belt-track${sliding ? " slide" : ""}`}>
          <div className="cline">{cur}</div>
          <div className="cline">{nxt}</div>
        </div>
      </div>
    </div>
  );
}

function buildItems(scene: AvalonScene): ReactNode[] {
  const good = scene.missions.filter((m) => m === "success").length;
  const evil = scene.missions.filter((m) => m === "fail").length;
  const items: ReactNode[] = [
    <span><span className="rc-when">当前比分</span> <span className="ok">蓝 {good}</span> ： <span className="bad">红 {evil}</span></span>,
  ];
  scene.timeline.forEach((t) => {
    if (t.kind === "proposal") {
      // 投票不写"赞成/反对"，赞成染蓝、反对染红，靠颜色区分
      items.push(
        <span>
          <span className="rc-when">第{cn(t.quest)}次发车 第{cn(t.attempt)}次组队</span>
          {"　"}{circ(t.leader_no)} 提 <span className="rc-team">{seatList(t.team)}</span>
          {"　"}<span className="ok">{seatList(t.approves)}</span>
          {t.rejects.length > 0 && <span className="bad rc-gap">{seatList(t.rejects)}</span>}
          {"　"}<span className={t.approved ? "ok" : "bad"}>{t.approved ? "通过" : "否决"}</span>
        </span>,
      );
    } else {
      // 队员号码直接按各自亮的牌染色：成功蓝、破坏红，不再写"队/成功/破坏"
      items.push(
        <span>
          <span className="rc-when">第{cn(t.quest)}次发车</span>
          {"　"}{t.team.map((n) => (
            <span key={n} className={t.fail_seats.includes(n) ? "bad" : "ok"}>{circ(n)}</span>
          ))}
          {"　"}<span className={t.result === "success" ? "ok" : "bad"}>{t.result === "success" ? "成功" : "失败"}</span>
        </span>,
      );
    }
  });
  return items;
}

function Arena({ ch, scene, last }: { ch: Channel; scene: AvalonScene; last: FeedItem | undefined }) {
  const n = scene.seats.length;
  const half = Math.ceil(n / 2);
  const d = last?.directive;
  const dur = last?.duration ?? 0;
  const votes = d?.kind === "vote_reveal"
    ? Object.fromEntries(d.votes.map((v) => [v.seat_no, v.approve])) : {};
  const cards = d?.kind === "quest_reveal"
    ? Object.fromEntries(d.cards.map((c) => [c.seat_no, c.success])) : {};
  const dealing = d?.kind === "deal";
  const vars: CSSProperties = {};
  if (dealing && n) {
    // 身份牌的翻面落在「该座位的飞牌抵达手中」那一刻：起飞错峰 + 飞行时长。
    (vars as Record<string, string>)["--deal-stagger"] = `${(dur * 0.8) / n}s`;
    (vars as Record<string, string>)["--deal-fly"] = `${DEAL_FLY_FRAC * dur}s`;
  }
  if (d?.kind === "vote_reveal" && d.votes.length)
    (vars as Record<string, string>)["--vote-stagger"] = `${(dur * 0.62) / d.votes.length}s`;
  const seat = (s: AvalonSeatInfo) => (
    <Seat key={s.no} s={s} scene={scene} current={d} dealing={dealing} vote={votes[s.no]} card={cards[s.no]} />
  );
  return (
    <div className="arena" style={vars}>
      <div className="col left">{scene.seats.slice(0, half).map(seat)}</div>
      <div className="col right">{scene.seats.slice(half).map(seat)}</div>
      <CenterStage ch={ch} scene={scene} last={last} />
      {dealing && last && <DealFlight ch={ch} item={last} scene={scene} />}
    </div>
  );
}

function Seat({ s, scene, current, vote, card, dealing }: {
  s: AvalonSeatInfo; scene: AvalonScene; current: FeedItem["directive"] | undefined; vote?: boolean; card?: boolean; dealing: boolean;
}) {
  // 未发牌：身份面朝下（不染阵营色、不亮角色）。deal 那拍 dealt=true，角色牌逐张翻面（dealing 触发动画）。
  const dealt = scene.dealt;
  const ladyFocus = current?.kind === "lady_reveal"
    ? current.holder_no === s.no ? "lady-source" : current.target_no === s.no ? "lady-target" : ""
    : "";
  const showLadyBadge = scene.lady_holder === s.no || (current?.kind === "lady_reveal" && current.holder_no === s.no);
  const ladyBadge = current?.kind === "lady_reveal"
    ? current.holder_no === s.no ? " leaving" : current.target_no === s.no ? " receiving" : ""
    : "";
  const cls = [
    "seat", dealt ? s.side : "facedown",
    scene.speaking === s.no ? "speaking" : "",
    scene.team.includes(s.no) ? "on-team" : "",
    ladyFocus,
    dealing ? "dealing" : "",
  ].join(" ");
  return (
    <div className={cls} data-di={s.no - 1} style={{ "--di": s.no - 1 } as CSSProperties}>
      <div className="avatar-wrap">
        <div className={avaClass("avatar", s)} style={avaBg(s)}>
          <AvaFill seat={s} />
        </div>
        <span className="seat-no">{s.no}</span>
        {scene.leader_no === s.no && <span className="badge crown"><StageIcon name="crown" size={28} /></span>}
        {showLadyBadge && <span className={`badge lady${ladyBadge}`}><StageIcon name="lady" size={16} /></span>}
        {vote !== undefined && <span className={`badge stamp ${vote ? "yes" : "no"}`}>{vote ? "赞" : "反"}</span>}
        {card !== undefined && <span className={`badge stamp ${card ? "yes" : "no"}`}>{card ? "成" : "破"}</span>}
      </div>
      <div className="seat-info">
        <div className="seat-name">{s.name}</div>
        {dealt ? (
          <div className={`seat-role ${s.side}`}>
            <StageIcon name={roleIconName(s.role)} size={21} /> {roleLabel(s.role)}
          </div>
        ) : (
          <div className="seat-role facedown">🂠 待发牌</div>
        )}
        {s.model_display && <div className="seat-model">{s.model_display}</div>}
      </div>
    </div>
  );
}

function CenterStage({ ch, scene, last }: { ch: Channel; scene: AvalonScene; last: FeedItem | undefined }) {
  const k = last?.directive.kind;
  // 终局起（game_over 大揭示 + 整个复盘期）常驻结算画面：胜负各一行头像不再被复盘发言顶掉。
  // 不按 last.seq 重挂，故复盘逐句切换时头像不重新弹入；当前复盘发言作为底部字幕滚动。
  if (scene.winner) {
    const banter = last?.directive.kind === "speech" ? last : undefined;
    return (
      <div className="center-wrap">
        <div className="center-stage tall">
          <div className="stage-content"><OverView ch={ch} scene={scene} banter={banter} /></div>
        </div>
      </div>
    );
  }
  const tall = k === "assassination";
  return (
    <div className="center-wrap">
      <div className={`center-stage${tall ? " tall" : ""}`}>
        {last && <div className="stage-content" key={last.seq}><StageContent ch={ch} scene={scene} last={last} /></div>}
      </div>
    </div>
  );
}

function StageContent({ ch, scene, last }: { ch: Channel; scene: AvalonScene; last: FeedItem }) {
  const d = last.directive;
  const seatOf = (no: number) => scene.seats.find((s) => s.no === no);

  switch (d.kind) {
    case "game_open":
      return (
        <Feature cls="feature-open">
          <div className="feature-icon"><StageIcon name="herald" size={64} /></div>
          <div className="feature-title">新的一局</div>
          <div className="feature-sub">{d.players} 人 · 蓝方 {d.good} / 红方 {d.evil}</div>
        </Feature>
      );
    case "deal":
      return <DealDeck ch={ch} item={last} n={scene.seats.length} />;
    case "host":
      return null;
    case "speech": {
      const sp = seatOf(d.seat_no);
      return (
        <Spotlight ch={ch} item={last} seat={sp} no={d.seat_no}
          tag={PHASE_LABEL[d.phase]} text={d.text} audio={d.audio_url} monologue={d.monologue}
          team={d.team ?? undefined}
          ladyNo={d.lady_target} ladyEvil={d.lady_truth === "evil"} />
      );
    }
    case "lady_reveal":
      return <LadyView ch={ch} scene={scene} item={last} d={d} />;
    case "vote_reveal":
      return <VoteView ch={ch} scene={scene} item={last} d={d} />;
    case "quest_reveal":
      return <QuestView ch={ch} scene={scene} item={last} d={d} />;
    case "assassination":
      return <AssassinView ch={ch} scene={scene} item={last} d={d} />;
    case "game_over":
      return null;  // 终局起结算画面由 CenterStage 常驻渲染（scene.winner 已定），这里不再单独出
    case "intermission":
      return null;
  }
}

function Feature({ cls, children }: { cls: string; children: ReactNode }) {
  return <div className={`feature ${cls}`}>{children}</div>;
}

// 逐人揭示用的大头像 + 大盖章：表决/发车/刺杀都把"谁出了什么"放中央放大，不再只靠座位旁的小印章。
function RevealAvatar({ seat, no, mark, label, idx }: {
  seat: AnySeat | undefined; no: number; mark?: "yes" | "no" | "pending"; label: string; idx: number;
}) {
  // mark 省略 = 尚未揭晓（如刺杀靶子在刺客说完之前）：盖一枚中性"待揭"印章。
  return (
    <div className="rv" style={{ "--ri": idx } as CSSProperties}>
      <div className={avaClass("rv-ava", seat)} style={avaBg(seat)}><AvaFill seat={seat} /></div>
      <span className="rv-no">{circ(no)}</span>
      <span className={`rv-stamp ${mark ?? "pending"}`}>{label}</span>
    </div>
  );
}

function mainTextWindow(item: FeedItem): number {
  const mainEnds = item.tracks
    .filter((track) => track.kind === "main")
    .map((track) => track.start + track.duration);
  if (mainEnds.length > 0) return Math.max(Math.max(...mainEnds) - item.content_at, 0.001);
  return Math.max(item.duration - item.content_at, 0.001);
}

function revealAt(item: FeedItem, fraction: number): number {
  return item.content_at + mainTextWindow(item) * fraction;
}

function activeNarration(ch: Channel, item: FeedItem, now: number) {
  const elapsed = elapsedOf(ch, item.started_at, now);
  return item.narrations.find((n) => elapsed >= n.at && elapsed <= n.at + n.duration);
}

function NarrationLine({ ch, item, now }: { ch: Channel; item: FeedItem; now: number }) {
  const cue = activeNarration(ch, item, now);
  if (!cue) return null;
  return <div className="narration-line">{cue.name}：{cue.text}</div>;
}

function LadyView({ ch, scene, item, d }: {
  ch: Channel; scene: AvalonScene; item: FeedItem;
  d: Extract<FeedItem["directive"], { kind: "lady_reveal" }>;
}) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 60);
    return () => window.clearInterval(t);
  }, [item.seq]);
  const seatOf = (no: number) => scene.seats.find((s) => s.no === no);
  const revealed = elapsedOf(ch, item.started_at, now) >= revealAt(item, 0.54);
  const evil = d.truth === "evil";
  const truth = sideLabel(d.truth);
  return (
    <div className={`reveal-view lady-view ${revealed ? (evil ? "bad" : "ok") : ""}`}>
      <NarrationLine ch={ch} item={item} now={now} />
      <div className="vote-title">湖中仙女</div>
      <div className="lady-flow">
        <RevealAvatar seat={seatOf(d.holder_no)} no={d.holder_no} mark="yes" label="持有者" idx={0} />
        <span className={`lady-beam${revealed ? " revealed" : ""}`}><StageIcon name="lady" size={48} /></span>
        <RevealAvatar seat={seatOf(d.target_no)} no={d.target_no}
          mark={revealed ? (evil ? "no" : "yes") : undefined}
          label={revealed ? (evil ? "红方" : "蓝方") : "查验中"} idx={1} />
      </div>
      {d.choose_monologue && (
        <div className="lady-secret"><span className="think-mark">💭</span><span className="think-text">{d.choose_monologue}</span></div>
      )}
      <div className="lady-layers">
        <div className="lady-layer public">
          <span className="layer-tag">现场公开</span>
          <div className="lady-speech">「<LiveText ch={ch} item={item} text={d.text} />」</div>
        </div>
        <div className={`lady-layer god ${revealed ? (evil ? "bad" : "ok") : ""}`}>
          <span className="layer-tag god">观众视角</span>
          <div className="lady-result">{revealed ? `真实阵营：${truth}` : "查验中"}</div>
          <div className="lady-token-line">令牌传给 {circ(d.target_no)}号</div>
        </div>
      </div>
      {d.monologue && (
        <div className="lady-think"><span className="think-mark">💭</span><span className="think-text">{d.monologue}</span></div>
      )}
    </div>
  );
}

function QuestView({ ch, scene, item, d }: {
  ch: Channel; scene: AvalonScene; item: FeedItem;
  d: Extract<FeedItem["directive"], { kind: "quest_reveal" }>;
}) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 60);
    return () => window.clearInterval(t);
  }, [item.seq]);
  const seatOf = (no: number) => scene.seats.find((s) => s.no === no);
  const n = d.cards.length;
  const stagger = (item.duration * 0.58) / Math.max(n, 1);
  const elapsed = elapsedOf(ch, item.started_at, now);
  const opened = Math.min(n, Math.max(0, Math.floor(elapsed / Math.max(stagger, 0.001))));
  const shownFails = d.cards.slice(0, opened).filter((c) => !c.success).length;
  const settled = opened >= n;
  const failText = settled ? d.fails : shownFails;
  return (
    <div className={`reveal-view quest-view ${settled ? (d.result === "success" ? "ok" : "bad") : ""}`}>
      <NarrationLine ch={ch} item={item} now={now} />
      <div className="quest-kicker">第 {d.quest} 任务 · 队伍 {seatList(d.team)}</div>
      <div className="vote-title">{settled ? (d.result === "success" ? "任务成功" : "任务失败") : "任务开牌"}</div>
      <div className="quest-board">
        <div className="quest-public">
          <span className="layer-tag">现场公布</span>
          <span className="quest-count"><b>{failText}</b> / {d.fails_required} 破坏票</span>
        </div>
        <div className="quest-god">
          <span className="layer-tag god">观众视角</span>
          <span>{settled ? "出牌明细已翻开" : `已翻开 ${opened}/${n}`}</span>
        </div>
      </div>
      <div className="quest-card-grid">
        {d.cards.map((card, i) => {
          const revealed = i < opened;
          return (
            <QuestCardTile key={`${card.seat_no}-${revealed ? "open" : "closed"}`} seat={seatOf(card.seat_no)}
              card={card} revealed={revealed} idx={i} />
          );
        })}
      </div>
    </div>
  );
}

function QuestCardTile({ seat, card, revealed, idx }: {
  seat: AvalonSeatInfo | undefined; card: QuestCard; revealed: boolean; idx: number;
}) {
  const mark = revealed ? (card.success ? "yes" : "no") : undefined;
  const label = revealed ? (card.success ? "成功" : "破坏") : "待翻";
  return (
    <div className={`quest-card-tile ${revealed ? (card.success ? "success" : "fail") : "pending"}`}>
      <RevealAvatar seat={seat} no={card.seat_no} mark={mark} label={label} idx={idx} />
      {revealed && card.monologue && (
        <div className="quest-think"><span className="think-mark">💭</span><span className="think-text">{card.monologue}</span></div>
      )}
    </div>
  );
}

// 刺杀：先演刺客出刀 + 台词打字机（与音频同步），台词念完才翻牌定生死——命中/幸存印章、
// 判决文案、阵营色与红色闪屏全 gate 在"台词念完"之后（与 LiveText 同口径 duration*0.62）。
function AssassinView({ ch, scene, item, d }: {
  ch: Channel; scene: AvalonScene; item: FeedItem;
  d: Extract<FeedItem["directive"], { kind: "assassination" }>;
}) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 60);
    return () => window.clearInterval(t);
  }, [item.seq]);
  const seatOf = (no: number) => scene.seats.find((s) => s.no === no);
  const revealed = elapsedOf(ch, item.started_at, now) >= revealAt(item, 0.62);
  return (
    <div className={`reveal-view assassin-view ${revealed ? (d.hit ? "hit" : "miss") : ""}`}>
      <NarrationLine ch={ch} item={item} now={now} />
      {revealed && <div key={item.seq} className="flash flash-assassination local" />}
      <div className="vote-title">刺杀</div>
      <div className="vs-row">
        <RevealAvatar seat={seatOf(d.assassin_no)} no={d.assassin_no} mark="no" label="刺客" idx={0} />
        <span className="dagger"><StageIcon name="assassin" size={52} /></span>
        <RevealAvatar seat={seatOf(d.target_no)} no={d.target_no}
          mark={revealed ? (d.hit ? "no" : "yes") : undefined}
          label={revealed ? (d.hit ? "命中" : "幸存") : "梅林？"} idx={1} />
      </div>
      <div className="assassin-line">「<LiveText ch={ch} item={item} text={d.text} />」</div>
      {d.monologue && (
        <div className="assassin-think"><span className="think-mark">💭</span><span className="think-text">{d.monologue}</span></div>
      )}
      {revealed && (
        <div className={`verdict ${d.hit ? "bad" : "ok"}`}>
          {d.hit ? "正中梅林！红方翻盘！" : "刺空了！蓝方守住胜利！"}
        </div>
      )}
    </div>
  );
}

// 结算：胜方一行、败方一行，各行左侧标"胜利/失败"（取代居中的"某方胜利"大字）。终局起常驻，
// 数据取自折好的 scene（不随某一拍 directive），复盘期把当前发言作为底部字幕滚动。
function OverView({ ch, scene, banter }: { ch: Channel; scene: AvalonScene; banter?: FeedItem }) {
  const winner = scene.winner;
  const result = `${sideLabel(winner ?? "good")}胜利`;
  const reason = winReasonLabel(winner ?? "good", scene.win_reason ?? "");
  const rows: { tag: string; cls: string; seats: AvalonSeatInfo[] }[] = [
    { tag: "胜利", cls: "win", seats: scene.seats.filter((s) => s.side === winner) },
    { tag: "失败", cls: "lose", seats: scene.seats.filter((s) => s.side !== winner) },
  ];
  const speech = banter?.directive.kind === "speech" ? banter.directive : undefined;
  return (
    <div className={`over-view ${winner}`}>
      <div className="over-head">
        <span className={`over-winner ${winner ?? ""}`}>{result}</span>
        <span className="over-head-reason">{reason}</span>
      </div>
      <div className="over-rows">
        {rows.map((r) => (
          <div key={r.cls} className={`over-row ${r.cls}`}>
            <span className={`over-tag ${r.cls}`}>{r.tag}</span>
            <div className="over-seats">
              {r.seats.map((s, i) => (
                <div key={s.no} className={`over-seat ${s.side}`} style={{ animationDelay: `${i * 0.1}s` }}>
                  <div className={avaClass("over-ava", s)} style={avaBg(s)}><AvaFill seat={s} /></div>
                  <span className="over-no">{circ(s.no)}</span>
                  <div className="over-role"><StageIcon name={roleIconName(s.role)} size={16} /> {roleLabel(s.role)}</div>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
      {/* 下半区：复盘期播当前发言（打字机+TTS+内心戏），大揭示那拍还没发言时显赛果原因。 */}
      <div className="over-talk">
        {speech && banter ? (
          <>
            <div className="over-banter live-scroll" key={banter.seq}>
              <div className="over-banter-inner"><span className="ob-who">{circ(speech.seat_no)}号</span>「<LiveText ch={ch} item={banter} text={speech.text} />」</div>
            </div>
            {speech.monologue && (
              <div className="over-think" key={`m${banter.seq}`}>
                <span className="think-mark">💭</span><span className="think-text">{speech.monologue}</span>
              </div>
            )}
          </>
        ) : (
          <div className="over-reason">全员身份亮底</div>
        )}
      </div>
    </div>
  );
}

// 错峰节奏由本拍 duration 反推（与发牌/亮票同源）；pace=0 时 duration→0、stagger→0，瞬间倾倒。
function RevealRow({ stagger, children }: { stagger: number; children: ReactNode }) {
  return <div className="reveal-row" style={{ "--rv-stagger": `${stagger}s` } as CSSProperties}>{children}</div>;
}

function Spotlight({ ch, item, seat, no, tag, text, audio, monologue, team, ladyNo, ladyEvil }: {
  ch: Channel; item: FeedItem; seat: AvalonSeatInfo | undefined; no: number; tag: string;
  text: string; audio: string | null; monologue: string;
  team?: number[]; ladyNo: number | null; ladyEvil: boolean;
}) {
  return (
    <div className="spotlight">
      <div className="spot-head">
        <div className={avaClass("spot-avatar", seat)} style={avaBg(seat)}><AvaFill seat={seat} /></div>
        <div className="spot-id">
          <div className="spot-name">{circ(no)} 号</div>
          <div className="spot-tag">
            {tag}{seat ? <> · <StageIcon name={roleIconName(seat.role)} size={18} /> {roleLabel(seat.role)}</> : ""}
          </div>
        </div>
        {/* 内心戏：给观众看的隐藏信息，嵌在发言区里，不再另起一层底框。 */}
        {monologue && (
          <div className="spot-think" key={item.seq}>
            <span className="think-mark">💭</span>
            <span className="think-text">{monologue}</span>
          </div>
        )}
      </div>
      {team && team.length > 0 && (
        <div className="team-chips">提队：{team.map((t) => <span key={t} className="chip">{circ(t)}</span>)}</div>
      )}
      {ladyNo !== null && (
        <div className={`lady-line ${ladyEvil ? "bad" : "ok"}`}>
          <StageIcon name="lady" size={16} /> 查验 {circ(ladyNo)}号 —— 真身是{ladyEvil ? "红方" : "蓝方"}
        </div>
      )}
      <div className="spot-text live-scroll"><div className="spot-text-inner"><LiveText ch={ch} item={item} text={text} /></div></div>
    </div>
  );
}

function VoteView({ ch, scene, item, d }: {
  ch: Channel; scene: AvalonScene; item: FeedItem;
  d: Extract<FeedItem["directive"], { kind: "vote_reveal" }>;
}) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 60);
    return () => window.clearInterval(t);
  }, [item.seq]);
  const n = d.votes.length;
  const stagger = (item.duration * 0.62) / Math.max(n, 1);
  const elapsed = elapsedOf(ch, item.started_at, now);
  const landed = Math.min(n, Math.max(0, Math.floor(elapsed / Math.max(stagger, 0.001))));
  const shown = d.votes.slice(0, landed);
  const yes = shown.filter((v) => v.approve).length;
  const noCount = shown.length - yes;
  const settled = landed >= n;
  const seatOf = (no: number) => scene.seats.find((s) => s.no === no);
  return (
    <div className={`reveal-view vote-view ${settled ? (d.approved ? "ok" : "bad") : ""}`}>
      <div className="vote-title">{settled ? (d.approved ? "表决通过" : "表决被否") : "表决中…"}</div>
      {/* 逐个亮票：shown 随时间增长，新头像逐个落入（JS 控时，stagger 给 0 不重复延时）。 */}
      <RevealRow stagger={0}>
        {shown.map((v) => (
          <RevealAvatar key={v.seat_no} seat={seatOf(v.seat_no)} no={v.seat_no}
            mark={v.approve ? "yes" : "no"} label={v.approve ? "赞成" : "反对"} idx={0} />
        ))}
      </RevealRow>
      <div className="tally">
        <span className="t-yes">赞成 {yes}</span>
        <div className="tally-bar">
          <div className="ty" style={{ width: `${n ? (yes / n) * 100 : 0}%` }} />
          <div className="tn" style={{ width: `${n ? (noCount / n) * 100 : 0}%` }} />
        </div>
        <span className="t-no">反对 {noCount}</span>
      </div>
      {settled && !d.approved && <div className="vote-warn">连续否决 {d.rejections}/5 · 满 5 红方直接获胜</div>}
    </div>
  );
}

// 中央发牌台：牌叠按真实剩余张数缩短（n→…→0），数字随飞牌实时递减。
function DealDeck({ ch, item, n }: { ch: Channel; item: FeedItem; n: number }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 50);
    return () => window.clearInterval(t);
  }, [item.seq]);
  const remaining = n - dealtCount(elapsedOf(ch, item.started_at, now), item.duration, n);
  return (
    <Feature cls="feature-deal">
      <div className="deck">
        {remaining >= 3 && <span className="deck-card s2" />}
        {remaining >= 2 && <span className="deck-card s1" />}
        {remaining >= 1
          ? <span className="deck-card top"><span className="deck-count" key={remaining}>{remaining}</span></span>
          : <span className="deck-done">✓</span>}
      </div>
      <div className="feature-title">{remaining > 0 ? `发牌 · 剩 ${remaining} 张` : "发牌完毕"}</div>
    </Feature>
  );
}

// 飞牌层：每张牌从中央牌叠沿真实弧线飞到对应座位的头像（手中），位置纯由时钟逐帧反推。
// 起飞错峰与发牌台同源（dealtCount），抵达即触发该座位身份牌翻面（CSS 延时 = 错峰 + 飞行）。
function DealFlight({ ch, item, scene }: { ch: Channel; item: FeedItem; scene: AvalonScene }) {
  const n = scene.seats.length;
  const [now, setNow] = useState(() => Date.now());
  const [geo, setGeo] = useState<{ ox: number; oy: number; targets: Map<number, { x: number; y: number }> } | null>(null);
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 33);
    return () => window.clearInterval(t);
  }, [item.seq]);
  // 量出牌叠中心与各头像中心，换算到 1920×1080 画布坐标系（getBoundingClientRect 拿到的是缩放后屏幕坐标，除以 scale 还原）。
  useLayoutEffect(() => {
    const canvas = document.querySelector(".canvas") as HTMLElement | null;
    const deck = document.querySelector(".deck") as HTMLElement | null;
    if (!canvas || !deck) { setGeo(null); return; }
    const cr = canvas.getBoundingClientRect();
    const s = cr.width / W || 1;
    const toCanvas = (el: Element) => {
      const r = el.getBoundingClientRect();
      return { x: (r.left + r.width / 2 - cr.left) / s, y: (r.top + r.height / 2 - cr.top) / s };
    };
    const o = toCanvas(deck);
    const targets = new Map<number, { x: number; y: number }>();
    document.querySelectorAll(".seat[data-di]").forEach((seat) => {
      const ava = seat.querySelector(".avatar-wrap");
      if (ava) targets.set(Number((seat as HTMLElement).dataset.di), toCanvas(ava));
    });
    setGeo({ ox: o.x, oy: o.y, targets });
  }, [item.seq, n]);

  if (!geo) return null;
  const stagger = (item.duration * 0.8) / Math.max(n, 1);
  const flyDur = Math.max(DEAL_FLY_FRAC * item.duration, 0.0001);
  const elapsed = elapsedOf(ch, item.started_at, now);
  const cards: ReactNode[] = [];
  for (let di = 0; di < n; di++) {
    const tgt = geo.targets.get(di);
    if (!tgt) continue;
    const p = (elapsed - di * stagger) / flyDur;
    if (p <= 0 || p >= 1) continue; // 仍在牌叠 / 已入手（交给座位翻面）
    const e = 1 - Math.pow(1 - p, 3); // easeOutCubic：抛出后减速落入手中
    const x = geo.ox + (tgt.x - geo.ox) * e;
    const y = geo.oy + (tgt.y - geo.oy) * e - 90 * Math.sin(Math.PI * p); // sin 抬一个抛物线弧顶
    const scale = 1 - 0.45 * p;
    const rot = (di % 2 ? 1 : -1) * 16 * (1 - p);
    const op = Math.min(1, p * 6) * Math.min(1, (1 - p) * 6); // 起飞/落手两端快速淡入淡出
    cards.push(
      <span key={di} className="fly-card"
        style={{ left: x, top: y, opacity: op, transform: `translate(-50%,-50%) scale(${scale}) rotate(${rot}deg)` }}>
        <span className="fly-crest">⚜</span>
      </span>,
    );
  }
  return <div className="deal-flight">{cards}</div>;
}

function LiveText({ ch, item, text }: { ch: Channel; item: FeedItem; text: string }) {
  const [now, setNow] = useState(() => Date.now());
  const ref = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 50);
    return () => window.clearInterval(t);
  }, [item.seq]);
  const budget = mainTextWindow(item) * 0.62;
  const elapsed = elapsedOf(ch, item.started_at, now) - item.content_at;
  const n = Math.min(Math.floor((elapsed / budget) * text.length), text.length);
  // 文字铺满后把最近的滚动容器（.live-scroll：发言区/复盘字幕共用契约）拉到底，最新一行始终可见。
  useEffect(() => {
    const box = ref.current?.closest(".live-scroll");
    if (box) box.scrollTop = box.scrollHeight;
  });
  return <span ref={ref}>{text.slice(0, Math.max(n, 0))}{n < text.length && <span className="caret">▌</span>}</span>;
}

function AudioLayer({ ch, item }: { ch: Channel; item: FeedItem }) {
  return (
    <>
      {item.tracks.map((track, i) => (
        <Audio key={`${item.seq}-${i}-${track.kind}-${track.start}`} ch={ch} item={item}
          url={track.audio_url} at={track.start} volume={track.volume} />
      ))}
    </>
  );
}

function Audio({ ch, item, url, at = item.content_at, volume = 1.0 }: {
  ch: Channel; item: FeedItem; url: string | null | undefined; at?: number; volume?: number;
}) {
  useEffect(() => {
    if (!url) return;
    let timer = 0;
    let el: HTMLAudioElement | null = null;
    const play = (offset: number) => {
      el = new window.Audio(url);
      // 时间轴倍速：拍被压缩了 speed 倍，音频也按 speed 提速才放得进、对得齐（中途加入时音频位置同乘 speed）。
      el.playbackRate = item.speed;
      el.volume = volume;
      el.currentTime = Math.max(offset * item.speed, 0);
      el.play().catch(() => {});
    };
    const elapsed = elapsedOf(ch, item.started_at, Date.now()) - at;
    if (elapsed >= 0) {
      play(elapsed);
    } else {
      timer = window.setTimeout(() => play(0), Math.max(-elapsed, 0) * 1000);
    }
    return () => {
      if (timer) window.clearTimeout(timer);
      el?.pause();
    };
  }, [url, item.seq, item.speed, at, volume]);
  return null;
}

const WW_ROLE: Record<string, string> = {
  werewolf: "狼人", villager: "平民", seer: "预言家", witch: "女巫", hunter: "猎人", idiot: "白痴",
};
const WW_REASON: Record<string, string> = {
  wolf_kill: "狼刀", poison: "毒杀", exile: "放逐", hunter_shot: "猎枪",
};
const WW_WIN_REASON: Record<string, [string, string]> = {
  all_wolves_dead: ["狼人全灭", "狼人全灭"],
  all_gods_dead: ["神职全灭", "神职全灭"],
  all_villagers_dead: ["平民全灭", "平民全灭"],
};
const wwRole = (r: string) => WW_ROLE[r] ?? r;
const wwReason = (r: string) => WW_REASON[r] ?? r;
const wwWinReason = (winner: string, reason: string) => {
  const pair = WW_WIN_REASON[reason];
  if (!pair) return reason;
  return winner === "good" ? pair[0] : pair[1];
};

function WerewolfStage({ ch, state, scene, last, scale, connected, flashKind }: {
  ch: Channel; state: StateMsg; scene: WerewolfScene; last: FeedItem | undefined;
  scale: number; connected: boolean; flashKind: string | null;
}) {
  return (
    <div className="viewport">
      <div className="canvas ww-canvas" style={{ width: W, height: H, transform: `scale(${scale})` }}>
        <div className="bg ww-bg" />
        {flashKind && <div key={last!.seq} className={`flash flash-${flashKind}`} />}
        <WerewolfHud scene={scene} />
        <WerewolfArena ch={ch} scene={scene} last={last} />
        {last && <AudioLayer ch={ch} item={last} />}
        {state.phase === "idle" && <Curtain title="稍候开演" sub="导播正在排片…" />}
        {last?.directive.kind === "intermission" && <IntermissionCard d={last.directive} />}
        <div className="overlay-canvas">
          {state.overlays.map((o) => (
            <div key={o.id} className={`ov ov-${o.kind}`}
              style={{ left: `${o.x}%`, top: `${o.y}%`, width: o.width ? `${o.width}%` : undefined, ...o.style }}>
              {o.kind === "image" ? <img src={o.content} alt="" /> : <span>{o.content}</span>}
            </div>
          ))}
        </div>
        {!connected && <div className="conn-dot" title="重连中" />}
      </div>
    </div>
  );
}

function WerewolfHud({ scene }: { scene: WerewolfScene }) {
  const aliveWolves = scene.seats.filter((s) => s.side === "werewolf" && s.alive).length;
  const aliveGood = scene.seats.filter((s) => s.side === "good" && s.alive).length;
  return (
    <div className="hud ww-hud">
      <div className="hud-left">
        <div className="brand">赛博物种观察日记</div>
      </div>
      <div className="hud-center ww-hud-center">
        <div className="ww-day">第 {scene.day || 1} 天 · {scene.phase === "night" ? "黑夜" : scene.phase === "postgame" ? "复盘" : "白天"}</div>
        <div className="ww-score">
          <span className="track-score good"><i>好人</i>{aliveGood}</span>
          <span className="ww-board">{scene.board_name || "狼人杀"}</span>
          <span className="track-score evil">{aliveWolves}<i>狼人</i></span>
        </div>
        <WerewolfTicker scene={scene} />
      </div>
      <div className="hud-right">
        <div className="show-badge">AI 狼人杀</div>
      </div>
    </div>
  );
}

function WerewolfTicker({ scene }: { scene: WerewolfScene }) {
  const items = werewolfItems(scene);
  const [idx, setIdx] = useState(0);
  const [sliding, setSliding] = useState(false);
  useEffect(() => {
    let hold: number, slide: number;
    const tick = () => {
      setSliding(true);
      slide = window.setTimeout(() => {
        setSliding(false);
        setIdx((i) => i + 1);
        hold = window.setTimeout(tick, HOLD_MS);
      }, SLIDE_MS);
    };
    hold = window.setTimeout(tick, HOLD_MS);
    return () => { window.clearTimeout(hold); window.clearTimeout(slide); };
  }, [items.length]);
  return (
    <div className="carousel ww-carousel">
      <span className="carousel-tag">场上信息</span>
      <div className="belt">
        <div className={`belt-track${sliding ? " slide" : ""}`}>
          <div className="cline">{items[idx % items.length]}</div>
          <div className="cline">{items[(idx + 1) % items.length]}</div>
        </div>
      </div>
    </div>
  );
}

function werewolfItems(scene: WerewolfScene): ReactNode[] {
  const alive = scene.seats.filter((s) => s.alive).map((s) => s.no);
  const items: ReactNode[] = [
    <span><span className="rc-when">存活</span> {seatList(alive)}</span>,
  ];
  scene.timeline.forEach((t) => {
    if (t.kind === "ww_death") {
      items.push(<span><span className="rc-when">第{cn(t.day)}天</span> {circ(t.seat_no)} 翻出 {wwRole(t.role)} · {wwReason(t.reason)}</span>);
    } else {
      items.push(
        <span>
          <span className="rc-when">第{cn(t.day)}天投票</span>
          {"　"}{t.tied || t.exiled === null ? <span className="bad">平票</span> : <span className="bad">放逐 {circ(t.exiled)}</span>}
        </span>,
      );
    }
  });
  return items;
}

function WerewolfArena({ ch, scene, last }: { ch: Channel; scene: WerewolfScene; last: FeedItem | undefined }) {
  const half = Math.ceil(scene.seats.length / 2);
  const d = last?.directive;
  const votes = d?.kind === "ww_vote_reveal"
    ? Object.fromEntries(d.votes.map((v) => [v.seat_no, v.target_no])) : {};
  const marked = new Set<number>();
  if (d?.kind === "ww_death_resolved") d.deaths.forEach((x) => marked.add(x.seat_no));
  if (d?.kind === "ww_role_reveal") marked.add(d.seat_no);
  if (d?.kind === "ww_idiot_reveal") marked.add(d.seat_no);
  if (d?.kind === "ww_hunter_shot" && d.target_no !== null) marked.add(d.target_no);
  const seat = (s: WerewolfSeatInfo) => (
    <WerewolfSeat key={s.no} s={s} scene={scene} voteTarget={votes[s.no]} marked={marked.has(s.no)} />
  );
  return (
    <div className="arena ww-arena">
      <div className="col left">{scene.seats.slice(0, half).map(seat)}</div>
      <div className="col right">{scene.seats.slice(half).map(seat)}</div>
      <WerewolfCenter ch={ch} scene={scene} last={last} />
    </div>
  );
}

function WerewolfSeat({ s, scene, voteTarget, marked }: {
  s: WerewolfSeatInfo; scene: WerewolfScene; voteTarget?: number; marked: boolean;
}) {
  const cls = [
    "seat ww-seat", s.side, s.alive ? "alive" : "dead",
    scene.speaking === s.no ? "speaking" : "",
    scene.night_target === s.no || scene.last_exiled === s.no || marked ? "marked" : "",
  ].join(" ");
  return (
    <div className={cls}>
      <div className="avatar-wrap">
        <div className={avaClass("avatar", s)} style={avaBg(s)}><AvaFill seat={s} /></div>
        <span className="seat-no">{s.no}</span>
        {!s.alive && <span className="badge stamp no">死</span>}
        {!s.can_vote && <span className="badge stamp yes">白</span>}
        {voteTarget !== undefined && <span className="badge stamp no">{circ(voteTarget)}</span>}
      </div>
      <div className="seat-info">
        <div className="seat-name">{s.name}</div>
        <div className={`seat-role ${s.side === "werewolf" ? "evil" : "good"}`}>{wwRole(s.role)}</div>
        {s.model_display && <div className="seat-model">{s.model_display}</div>}
      </div>
    </div>
  );
}

function WerewolfCenter({ ch, scene, last }: { ch: Channel; scene: WerewolfScene; last: FeedItem | undefined }) {
  if (scene.winner) {
    const banter = last?.directive.kind === "ww_speech" ? last : undefined;
    return (
      <div className="center-wrap">
        <div className="center-stage tall">
          <div className="stage-content"><WerewolfOverView ch={ch} scene={scene} banter={banter} /></div>
        </div>
      </div>
    );
  }
  return (
    <div className="center-wrap">
      <div className="center-stage tall ww-center">
        {last && <div className="stage-content" key={last.seq}><WerewolfContent ch={ch} scene={scene} last={last} /></div>}
      </div>
    </div>
  );
}

function WerewolfContent({ ch, scene, last }: { ch: Channel; scene: WerewolfScene; last: FeedItem }) {
  const d = last.directive;
  const seatOf = (no: number) => scene.seats.find((s) => s.no === no);
  switch (d.kind) {
    case "ww_game_open":
      return (
        <Feature cls="feature-open ww-feature">
          <div className="feature-icon"><StageIcon name="herald" size={64} /></div>
          <div className="feature-title">{d.board_name}</div>
          <div className="feature-sub">{d.players} 人 · 狼 {d.wolves} / 神 {d.gods} / 民 {d.villagers}</div>
        </Feature>
      );
    case "ww_deal":
      return (
        <Feature cls="feature-deal ww-feature">
          <div className="deck"><span className="deck-card top"><span className="deck-count">{d.seats.length}</span></span></div>
          <div className="feature-title">身份发放完毕</div>
        </Feature>
      );
    case "host":
      return null;
    case "ww_night_start":
      return <WerewolfNotice title={`第 ${d.day} 天 · 天黑`} sub="狼人、预言家、女巫依次行动" />;
    case "ww_wolf_kill":
      return (
        <div className="ww-card">
          <div className="vote-title">狼人夜议</div>
          <RevealRow stagger={0.16}>
            {d.votes.map((v, i) => (
              <RevealAvatar key={v.seat_no} seat={seatOf(v.seat_no)} no={v.seat_no} mark="no" label={`刀${circ(v.target_no)}`} idx={i} />
            ))}
          </RevealRow>
          <div className="feature-sub">最终刀口：{circ(d.target_no)}号</div>
        </div>
      );
    case "ww_seer_check":
      return (
        <WerewolfAction ch={ch} item={last} seat={seatOf(d.seat_no)} no={d.seat_no}
          title="预言家查验" text={`${circ(d.target_no)}号是${d.result === "werewolf" ? "狼人" : "好人"}`}
          monologue={d.monologue} audio={d.audio_url} danger={d.result === "werewolf"} />
      );
    case "ww_witch_action":
      return (
        <WerewolfAction ch={ch} item={last} seat={seatOf(d.seat_no)} no={d.seat_no}
          title="女巫行动" text={d.save ? "使用解药" : d.poison_target_no !== null ? `毒杀${circ(d.poison_target_no)}号` : "未使用药剂"}
          monologue={d.monologue} audio={d.audio_url} danger={d.poison_target_no !== null} />
      );
    case "ww_death_resolved":
      return (
        <div className={`reveal-view ww-card ${d.deaths.length ? "bad" : "ok"}`}>
          <div className="vote-title">{d.deaths.length ? "昨夜死讯" : "平安夜"}</div>
          {d.deaths.length > 0 && (
            <RevealRow stagger={0.18}>
              {d.deaths.map((x, i) => <RevealAvatar key={x.seat_no} seat={seatOf(x.seat_no)} no={x.seat_no} mark="no" label={wwReason(x.reason)} idx={i} />)}
            </RevealRow>
          )}
        </div>
      );
    case "ww_role_reveal":
      return <WerewolfReveal seat={seatOf(d.seat_no)} no={d.seat_no} title="翻牌" label={`${wwRole(d.role)} · ${wwReason(d.reason)}`} />;
    case "ww_idiot_reveal":
      return <WerewolfReveal seat={seatOf(d.seat_no)} no={d.seat_no} title="白痴翻牌" label="免死，失去投票权" ok />;
    case "ww_speech":
      return (
        <WerewolfTalk ch={ch} item={last} seat={seatOf(d.seat_no)} no={d.seat_no}
          tag={_WW_PHASE[d.phase] ?? d.phase} text={d.text} monologue={d.monologue} audio={d.audio_url} />
      );
    case "ww_vote_reveal":
      return <WerewolfVoteView scene={scene} d={d} />;
    case "ww_hunter_shot":
      return (
        <WerewolfAction ch={ch} item={last} seat={seatOf(d.hunter_no)} no={d.hunter_no}
          title="猎人开枪" text={d.target_no === null ? "不开枪" : `带走${circ(d.target_no)}号`}
          monologue={d.monologue} audio={d.audio_url} danger={d.target_no !== null} />
      );
    case "ww_game_over":
      return null;
    case "intermission":
      return null;
    default:
      return null;
  }
}

const _WW_PHASE: Record<string, string> = {
  discussion: "白天发言", last_words: "遗言", hunter: "猎人", banter: "复盘",
};

function WerewolfNotice({ title, sub }: { title: string; sub: string }) {
  return (
    <Feature cls="feature-open ww-feature">
      <div className="feature-title">{title}</div>
      <div className="feature-sub">{sub}</div>
    </Feature>
  );
}

function WerewolfReveal({ seat, no, title, label, ok = false }: {
  seat: WerewolfSeatInfo | undefined; no: number; title: string; label: string; ok?: boolean;
}) {
  return (
    <div className={`reveal-view ww-card ${ok ? "ok" : "bad"}`}>
      <div className="vote-title">{title}</div>
      <RevealRow stagger={0}>
        <RevealAvatar seat={seat} no={no} mark={ok ? "yes" : "no"} label={label} idx={0} />
      </RevealRow>
    </div>
  );
}

function WerewolfAction({ ch, item, seat, no, title, text, monologue, audio, danger }: {
  ch: Channel; item: FeedItem; seat: WerewolfSeatInfo | undefined; no: number; title: string;
  text: string; monologue: string; audio: string | null; danger: boolean;
}) {
  return (
    <div className={`spotlight ww-talk ${danger ? "bad" : "ok"}`}>
      <div className="spot-head">
        <div className={avaClass("spot-avatar", seat)} style={avaBg(seat)}><AvaFill seat={seat} /></div>
        <div className="spot-id">
          <div className="spot-name">{circ(no)} 号</div>
          <div className="spot-tag">{title}{seat ? <> · {wwRole(seat.role)}</> : ""}</div>
        </div>
        {monologue && <div className="spot-think"><span className="think-mark">💭</span><span className="think-text">{monologue}</span></div>}
      </div>
      <div className="spot-text live-scroll"><div className="spot-text-inner"><LiveText ch={ch} item={item} text={text} /></div></div>
    </div>
  );
}

function WerewolfTalk({ ch, item, seat, no, tag, text, monologue, audio }: {
  ch: Channel; item: FeedItem; seat: WerewolfSeatInfo | undefined; no: number; tag: string;
  text: string; monologue: string; audio: string | null;
}) {
  return (
    <div className="spotlight ww-talk">
      <div className="spot-head">
        <div className={avaClass("spot-avatar", seat)} style={avaBg(seat)}><AvaFill seat={seat} /></div>
        <div className="spot-id">
          <div className="spot-name">{circ(no)} 号</div>
          <div className="spot-tag">{tag}{seat ? <> · {wwRole(seat.role)}</> : ""}</div>
        </div>
        {monologue && <div className="spot-think"><span className="think-mark">💭</span><span className="think-text">{monologue}</span></div>}
      </div>
      <div className="spot-text live-scroll"><div className="spot-text-inner"><LiveText ch={ch} item={item} text={text} /></div></div>
    </div>
  );
}

function WerewolfVoteView({ scene, d }: {
  scene: WerewolfScene; d: Extract<FeedItem["directive"], { kind: "ww_vote_reveal" }>;
}) {
  const seatOf = (no: number) => scene.seats.find((s) => s.no === no);
  const counts = new Map<number, number>();
  d.votes.forEach((v) => counts.set(v.target_no, (counts.get(v.target_no) ?? 0) + 1));
  const ordered = [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0] - b[0]);
  return (
    <div className={`reveal-view vote-view ww-card ${d.exiled !== null ? "bad" : ""}`}>
      <div className="vote-title">{d.tied ? "平票无人出局" : `放逐 ${circ(d.exiled ?? 0)}号`}</div>
      <RevealRow stagger={0.12}>
        {d.votes.map((v, i) => (
          <RevealAvatar key={v.seat_no} seat={seatOf(v.seat_no)} no={v.seat_no}
            mark={v.target_no === d.exiled ? "no" : "pending"} label={`投${circ(v.target_no)}`} idx={i} />
        ))}
      </RevealRow>
      <div className="ww-vote-counts">
        {ordered.map(([no, count]) => <span key={no}>{circ(no)}号 {count} 票</span>)}
      </div>
    </div>
  );
}

function WerewolfOverView({ ch, scene, banter }: { ch: Channel; scene: WerewolfScene; banter?: FeedItem }) {
  const winner = scene.winner;
  const rows: { tag: string; cls: string; seats: WerewolfSeatInfo[] }[] = [
    { tag: "胜利", cls: "win", seats: scene.seats.filter((s) => s.side === winner) },
    { tag: "失败", cls: "lose", seats: scene.seats.filter((s) => s.side !== winner) },
  ];
  const speech = banter?.directive.kind === "ww_speech" ? banter.directive : undefined;
  return (
    <div className={`over-view ww-over ${winner}`}>
      <div className="over-rows">
        {rows.map((r) => (
          <div key={r.cls} className={`over-row ${r.cls}`}>
            <span className={`over-tag ${r.cls}`}>{r.tag}</span>
            <div className="over-seats">
              {r.seats.map((s, i) => (
                <div key={s.no} className={`over-seat ${s.side === "werewolf" ? "evil" : "good"}`} style={{ animationDelay: `${i * 0.08}s` }}>
                  <div className={avaClass("over-ava", s)} style={avaBg(s)}><AvaFill seat={s} /></div>
                  <span className="over-no">{circ(s.no)}</span>
                  <div className="over-role">{wwRole(s.role)}</div>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
      <div className="over-talk">
        {speech && banter ? (
          <>
            <div className="over-banter live-scroll" key={banter.seq}>
              <div className="over-banter-inner"><span className="ob-who">{circ(speech.seat_no)}号</span>「<LiveText ch={ch} item={banter} text={speech.text} />」</div>
            </div>
            {speech.monologue && <div className="over-think"><span className="think-mark">💭</span><span className="think-text">{speech.monologue}</span></div>}
          </>
        ) : (
          <div className="over-reason">{winner ? `${winner === "good" ? "好人" : "狼人"}胜利 · ${wwWinReason(winner, scene.win_reason ?? "")}` : ""}</div>
        )}
      </div>
    </div>
  );
}

function Curtain({ title, sub }: { title: string; sub: string }) {
  return <div className="curtain"><div className="curtain-title">{title}</div><div className="curtain-sub">{sub}</div></div>;
}

function IntermissionCard({ d }: { d: Intermission }) {
  const reason = d.last_winner === "werewolf" ? wwWinReason(d.last_winner, d.last_reason ?? "") : winReasonLabel(d.last_winner ?? "good", d.last_reason ?? "");
  return (
    <div className="curtain dim">
      <div className="curtain-title">幕间休息</div>
      {d.last_winner && <div className="curtain-sub">上局：{d.last_winner === "good" ? "好人胜" : "狼人胜"}（{d.last_reason ? reason : ""}）</div>}
      <div className="curtain-sub">{d.next_ready ? "下一局即将开始" : "下一局排片中…"}</div>
    </div>
  );
}
