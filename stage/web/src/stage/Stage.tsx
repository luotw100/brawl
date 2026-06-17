import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import type {
  FeedItem, Intermission, ProposalLog, QuestLog, Scene, SeatInfo,
} from "../protocol";
import { type Channel, elapsedOf, useChannel } from "../useChannel";
import { useSfx } from "./sfx";
import { useBgm } from "./bgm";
import { StageIcon, roleIconName } from "./StageIcon";
import "./stage.css";

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
const winReasonLabel = (winner: string, reason: string) => {
  const pair = WIN_REASON[reason];
  if (!pair) return reason;
  return winner === "good" ? pair[0] : pair[1];
};

const CIRC = "①②③④⑤⑥⑦⑧⑨⑩";
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
const avaClass = (base: string, seat: SeatInfo | undefined): string => (seat?.avatar_url ? `${base} has-img` : base);
const avaBg = (seat: SeatInfo | undefined): CSSProperties | undefined => (seat && !seat.avatar_url ? avatarStyle(seat.persona) : undefined);
function AvaFill({ seat }: { seat: SeatInfo | undefined }) {
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

  useSfx(ch, last);
  useBgm(state?.phase);

  if (!state) return <div className="viewport" />;
  const flashKind = last && ["vote_reveal", "quest_reveal", "assassination", "game_over"].includes(last.directive.kind)
    ? last.directive.kind : null;

  return (
    <div className="viewport">
      <div className="canvas" style={{ width: W, height: H, transform: `scale(${scale})` }}>
        <div className="bg" />
        {flashKind && <div key={last!.seq} className={`flash flash-${flashKind}`} />}
        {scene && <TopHud scene={scene} />}
        {scene && <Arena ch={ch} scene={scene} last={last} />}
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

function TopHud({ scene }: { scene: Scene }) {
  const good = scene.missions.filter((m) => m === "success").length;
  const evil = scene.missions.filter((m) => m === "fail").length;
  return (
    <div className="hud">
      <div className="hud-left">
        <div className="brand"><span className="crest">⚜</span> 赛博物种观察日记</div>
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
      <div className="hud-right" />
    </div>
  );
}

// 前情提要：把全场每个关键节点（每次组队、每次发车）按时序逐条循环播，给中途进来的
// 观众补课。5s 一条，叠帧卷轴：传送带停住让人看清→缓缓上移，旧条从顶卷出、新条从底升入。
const HOLD_MS = 7800;
const SLIDE_MS = 900;
function Carousel({ scene }: { scene: Scene }) {
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

function buildItems(scene: Scene): ReactNode[] {
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
          <span className="rc-when">第{cn(t.quest)}任务·第{cn(t.attempt)}次组队</span>
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

function Arena({ ch, scene, last }: { ch: Channel; scene: Scene; last: FeedItem | undefined }) {
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
  const seat = (s: SeatInfo) => (
    <Seat key={s.no} s={s} scene={scene} dealing={dealing} vote={votes[s.no]} card={cards[s.no]} />
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

function Seat({ s, scene, vote, card, dealing }: {
  s: SeatInfo; scene: Scene; vote?: boolean; card?: boolean; dealing: boolean;
}) {
  // 未发牌：身份面朝下（不染阵营色、不亮角色）。deal 那拍 dealt=true，角色牌逐张翻面（dealing 触发动画）。
  const dealt = scene.dealt;
  const cls = [
    "seat", dealt ? s.side : "facedown",
    scene.speaking === s.no ? "speaking" : "",
    scene.team.includes(s.no) ? "on-team" : "",
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
        {scene.lady_holder === s.no && <span className="badge lady"><StageIcon name="lady" size={16} /></span>}
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

function CenterStage({ ch, scene, last }: { ch: Channel; scene: Scene; last: FeedItem | undefined }) {
  const k = last?.directive.kind;
  const tall = k === "game_over" || k === "assassination";
  // 复盘横幅：胜负已分（scene.winner）且过了 game_over 大揭示那拍——复盘吐槽期间在发言区上方挂结果。
  const showResult = scene.winner && k !== "game_over";
  return (
    <div className="center-wrap">
      {showResult && (
        <div className={`result-banner ${scene.winner}`}>{scene.winner === "good" ? "蓝方胜利" : "红方胜利"}</div>
      )}
      <div className={`center-stage${tall ? " tall" : ""}`}>
        {last && <div className="stage-content" key={last.seq}><StageContent ch={ch} scene={scene} last={last} /></div>}
      </div>
    </div>
  );
}

function StageContent({ ch, scene, last }: { ch: Channel; scene: Scene; last: FeedItem }) {
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
      return (
        <div className="host-stage">
          <div className="host-name"><StageIcon name="herald" size={28} /> {d.name}</div>
          <div className="host-text"><LiveText ch={ch} item={last} text={d.text} /></div>
          <Audio ch={ch} item={last} url={d.audio_url} />
        </div>
      );
    case "speech": {
      const sp = seatOf(d.seat_no);
      return (
        <Spotlight ch={ch} item={last} seat={sp} no={d.seat_no}
          tag={PHASE_LABEL[d.phase]} text={d.text} audio={d.audio_url} monologue={d.monologue}
          team={d.team ?? undefined}
          ladyNo={d.lady_target} ladyEvil={d.lady_truth === "evil"} />
      );
    }
    case "vote_reveal":
      return <VoteView ch={ch} scene={scene} item={last} d={d} />;
    case "quest_reveal":
      return (
        <div className={`reveal-view quest-view ${d.result === "success" ? "ok" : "bad"}`}>
          <div className="vote-title">{d.result === "success" ? "任务成功" : "任务失败"}</div>
          <RevealRow stagger={(last.duration * 0.5) / Math.max(d.cards.length, 1)}>
            {d.cards.map((c, i) => (
              <RevealAvatar key={c.seat_no} seat={seatOf(c.seat_no)} no={c.seat_no}
                mark={c.success ? "yes" : "no"} label={c.success ? "成功" : "破坏"} idx={i} />
            ))}
          </RevealRow>
          <div className="feature-sub">第 {d.quest} 任务 · 破坏票 {d.fails}/{d.fails_required}</div>
        </div>
      );
    case "assassination":
      return (
        <div className={`reveal-view assassin-view ${d.hit ? "hit" : "miss"}`}>
          <div className="vote-title">刺杀</div>
          <div className="vs-row">
            <RevealAvatar seat={seatOf(d.assassin_no)} no={d.assassin_no} mark="no" label="刺客" idx={0} />
            <span className="dagger"><StageIcon name="assassin" size={52} /></span>
            <RevealAvatar seat={seatOf(d.target_no)} no={d.target_no}
              mark={d.hit ? "no" : "yes"} label={d.hit ? "命中" : "幸存"} idx={1} />
          </div>
          <div className="assassin-line">「{d.text}」</div>
          <div className={`verdict ${d.hit ? "bad" : "ok"}`}>
            {d.hit ? "正中梅林！红方翻盘！" : "刺空了！蓝方守住胜利！"}
          </div>
          <Audio ch={ch} item={last} url={d.audio_url} />
        </div>
      );
    case "game_over":
      return (
        <Feature cls={`feature-over ${d.winner}`}>
          <div className="feature-title big">{d.winner === "good" ? "蓝方胜利" : "红方胜利"}</div>
          <div className="feature-sub">{winReasonLabel(d.winner, d.win_reason)}</div>
          <div className="reveal-grid">
            {d.seats.map((s, i) => (
              <div key={s.no} className={`reveal ${s.side}`} style={{ animationDelay: `${i * 0.1}s` }}>
                <b>{circ(s.no)}</b> <StageIcon name={roleIconName(s.role)} size={18} /> {roleLabel(s.role)}
              </div>
            ))}
          </div>
        </Feature>
      );
    case "intermission":
      return null;
  }
}

function Feature({ cls, children }: { cls: string; children: ReactNode }) {
  return <div className={`feature ${cls}`}>{children}</div>;
}

// 逐人揭示用的大头像 + 大盖章：表决/发车/刺杀都把"谁出了什么"放中央放大，不再只靠座位旁的小印章。
function RevealAvatar({ seat, no, mark, label, idx }: {
  seat: SeatInfo | undefined; no: number; mark: "yes" | "no"; label: string; idx: number;
}) {
  return (
    <div className="rv" style={{ "--ri": idx } as CSSProperties}>
      <div className={avaClass("rv-ava", seat)} style={avaBg(seat)}><AvaFill seat={seat} /></div>
      <span className="rv-no">{circ(no)}</span>
      <span className={`rv-stamp ${mark}`}>{label}</span>
    </div>
  );
}

// 错峰节奏由本拍 duration 反推（与发牌/亮票同源）；pace=0 时 duration→0、stagger→0，瞬间倾倒。
function RevealRow({ stagger, children }: { stagger: number; children: ReactNode }) {
  return <div className="reveal-row" style={{ "--rv-stagger": `${stagger}s` } as CSSProperties}>{children}</div>;
}

function Spotlight({ ch, item, seat, no, tag, text, audio, monologue, team, ladyNo, ladyEvil }: {
  ch: Channel; item: FeedItem; seat: SeatInfo | undefined; no: number; tag: string;
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
        {/* 内心戏：头像右侧那块区域，纯排版（💭+斜体灰字）体现"脑子里的话"，不加框/底。 */}
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
      <div className="spot-text"><div className="spot-text-inner"><LiveText ch={ch} item={item} text={text} /></div></div>
      <Audio ch={ch} item={item} url={audio} />
    </div>
  );
}

function VoteView({ ch, scene, item, d }: {
  ch: Channel; scene: Scene; item: FeedItem;
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
function DealFlight({ ch, item, scene }: { ch: Channel; item: FeedItem; scene: Scene }) {
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
  const budget = Math.max(item.duration * 0.62, 0.001);
  const n = Math.min(Math.floor((elapsedOf(ch, item.started_at, now) / budget) * text.length), text.length);
  // 文字铺满发言框后，把滚动容器拉到底，让最新一行始终可见（顶上的旧字滚出去）。
  useEffect(() => {
    const box = ref.current?.closest(".spot-text");
    if (box) box.scrollTop = box.scrollHeight;
  });
  return <span ref={ref}>{text.slice(0, Math.max(n, 0))}{n < text.length && <span className="caret">▌</span>}</span>;
}

function Audio({ ch, item, url }: { ch: Channel; item: FeedItem; url: string | null | undefined }) {
  useEffect(() => {
    if (!url) return;
    const el = new window.Audio(url);
    el.currentTime = Math.max(elapsedOf(ch, item.started_at, Date.now()), 0);
    el.play().catch(() => {});
    return () => el.pause();
  }, [url, item.seq]);
  return null;
}

function Curtain({ title, sub }: { title: string; sub: string }) {
  return <div className="curtain"><div className="curtain-title">{title}</div><div className="curtain-sub">{sub}</div></div>;
}

function IntermissionCard({ d }: { d: Intermission }) {
  return (
    <div className="curtain dim">
      <div className="curtain-title">幕间休息</div>
      {d.last_winner && <div className="curtain-sub">上局：{d.last_winner === "good" ? "蓝方胜" : "红方胜"}（{d.last_reason ? winReasonLabel(d.last_winner, d.last_reason) : ""}）</div>}
      <div className="curtain-sub">{d.next_ready ? "下一局即将开始" : "下一局排片中…"}</div>
    </div>
  );
}
