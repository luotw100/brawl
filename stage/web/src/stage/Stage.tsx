import React, { useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import type {
  FeedItem, Intermission, ProposalLog, QuestLog, Scene, SeatInfo,
} from "../protocol";
import { type Channel, elapsedOf, useChannel } from "../useChannel";
import { StageIcon, roleIconName } from "./StageIcon";
import "./stage.css";

const W = 1920;
const H = 1080;

const ROLE: Record<string, string> = {
  merlin: "梅林", percival: "派西维尔", servant: "忠臣",
  assassin: "刺客", morgana: "莫甘娜", mordred: "莫德雷德",
  oberon: "奥伯伦", minion: "爪牙",
};
const PHASE_LABEL: Record<string, string> = {
  propose: "提队", discuss: "发言", lady: "湖中仙女", banter: "赛后复盘",
};
const roleLabel = (r: string) => ROLE[r] ?? r;

const CIRC = "①②③④⑤⑥⑦⑧⑨⑩";
const circ = (n: number) => CIRC[n - 1] ?? `${n}`;
const ROMAN = ["I", "II", "III", "IV", "V"];

function hueOf(key: string): number {
  let h = 0;
  for (const c of key) h = (h * 31 + c.charCodeAt(0)) % 360;
  return h;
}
function avatarStyle(persona: string): CSSProperties {
  const hue = hueOf(persona);
  return { background: `linear-gradient(150deg, hsl(${hue} 55% 52%), hsl(${(hue + 36) % 360} 50% 30%))` };
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
        {state.paused && <div className="paused-tag">⏸ 暂停中</div>}
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
        <div className="brand"><span className="crest">⚜</span> AI 阿瓦隆 <span className="crest">⚜</span></div>
      </div>
      <div className="hud-center">
        <div className="mission-track">
          <span className="track-score good">{good}</span>
          <div className="missions">
            {scene.missions.map((m, i) => (
              <div key={i} className={`dot ${m ?? (i + 1 === scene.quest_no ? "current" : "todo")}`}>
                {m === "success" ? "✓" : m === "fail" ? "✕" : ROMAN[i]}
              </div>
            ))}
          </div>
          <span className="track-score evil">{evil}</span>
        </div>
        <div className="hud-sub">第 {scene.quest_no} 任务 · 第 {scene.attempt} 次组队</div>
      </div>
      <div className="hud-right"><Carousel scene={scene} /></div>
    </div>
  );
}

function Carousel({ scene }: { scene: Scene }) {
  const items = buildItems(scene);
  if (items.length === 0) return <div className="carousel" />;
  const doubled = [...items, ...items];
  return (
    <div className="carousel">
      <div className="ticker-row">
        {doubled.map((node, i) => <React.Fragment key={i}>{node}</React.Fragment>)}
      </div>
    </div>
  );
}

function buildItems(scene: Scene): ReactNode[] {
  const good = scene.missions.filter((m) => m === "success").length;
  const evil = scene.missions.filter((m) => m === "fail").length;
  const items: ReactNode[] = [
    <span>比分 <span className="ok">{good}</span> : <span className="bad">{evil}</span></span>,
  ];
  scene.timeline.forEach((t, i) => {
    if (t.kind === "proposal") {
      const leaderNo = circ(t.leader_no);
      const teamNos = t.team.map((n) => circ(n)).join(" ");
      const approveNos = t.approves.map((n) => circ(n)).join(" ");
      const rejectNos = t.rejects.map((n) => circ(n)).join(" ");
      items.push(
        <span>
          第{t.quest}轮·{t.attempt}组 {leaderNo}组队 {teamNos}
          {" "}<span className={t.approved ? "ok" : "bad"}>{t.approved ? "通过" : "否决"}</span>
          {" "}赞{t.approves.length} 反{t.rejects.length}
        </span>,
      );
    } else {
      const teamNos = t.team.map((n) => circ(n)).join(" ");
      items.push(
        <span>
          第{t.quest}轮发车 {teamNos}
          {" "}<span className={t.result === "success" ? "ok" : "bad"}>
            {t.result === "success" ? "成功" : `失败·破${t.fails}票`}
          </span>
        </span>,
      );
    }
  });
  const withSep: ReactNode[] = [];
  items.forEach((node, i) => {
    withSep.push(node);
    if (i < items.length - 1) withSep.push(<span className="sep">◆</span>);
  });
  return withSep;
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
  if (dealing && n) (vars as Record<string, string>)["--deal-stagger"] = `${(dur * 0.8) / n}s`;
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
    </div>
  );
}

function Seat({ s, scene, vote, card, dealing }: {
  s: SeatInfo; scene: Scene; vote?: boolean; card?: boolean; dealing: boolean;
}) {
  const cls = [
    "seat", s.side,
    scene.speaking === s.no ? "speaking" : "",
    scene.team.includes(s.no) ? "on-team" : "",
    dealing ? "dealing" : "",
  ].join(" ");
  return (
    <div className={cls} style={{ "--di": s.no - 1 } as CSSProperties}>
      <div className="avatar-wrap">
        <div className="avatar" style={avatarStyle(s.persona)}>
          <span className="ava-init">{s.name.slice(0, 1)}</span>
        </div>
        {scene.leader_no === s.no && <span className="badge crown"><StageIcon name="crown" size={28} /></span>}
        {scene.lady_holder === s.no && <span className="badge lady"><StageIcon name="lady" size={16} /></span>}
        {vote !== undefined && <span className={`badge stamp ${vote ? "yes" : "no"}`}>{vote ? "赞" : "反"}</span>}
        {card !== undefined && <span className={`badge stamp ${card ? "yes" : "no"}`}>{card ? "成" : "破"}</span>}
      </div>
      <div className="seat-info">
        <div className="seat-name">{s.no} {s.name}</div>
        <div className={`seat-role ${s.side}`}>
          <StageIcon name={roleIconName(s.role)} size={21} /> {roleLabel(s.role)}
        </div>
      </div>
    </div>
  );
}

function CenterStage({ ch, scene, last }: { ch: Channel; scene: Scene; last: FeedItem | undefined }) {
  const tall = last?.directive.kind === "game_over";
  return (
    <div className={`center-stage${tall ? " tall" : ""}`}>
      {last && <div className="stage-content" key={last.seq}><StageContent ch={ch} scene={scene} last={last} /></div>}
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
      return (
        <Feature cls="feature-deal">
          <div className="feature-icon"><StageIcon name="crown" size={64} /></div>
          <div className="feature-title">发牌 · 身份揭晓</div>
          <div className="feature-sub">
            首任队长 {circ(d.leader_no)}号{d.lady_holder ? ` · 仙女 ${circ(d.lady_holder)}号` : ""}
          </div>
        </Feature>
      );
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
          tag={PHASE_LABEL[d.phase]} text={d.text} monologue={d.monologue} audio={d.audio_url}
          team={d.team ?? undefined}
          ladyNo={d.lady_target} ladyEvil={d.lady_truth === "evil"} />
      );
    }
    case "vote_reveal":
      return <VoteView ch={ch} item={last} d={d} />;
    case "quest_reveal":
      return (
        <Feature cls={`feature-quest ${d.result === "success" ? "ok" : "bad"}`}>
          <div className="feature-icon">
            {d.result === "success" ? <StageIcon name="servant" size={64} /> : <StageIcon name="assassin" size={64} />}
          </div>
          <div className="feature-title">{d.result === "success" ? "任务成功" : "任务失败"}</div>
          <div className="feature-sub">第 {d.quest} 任务 · 破坏票 {d.fails}/{d.fails_required}</div>
        </Feature>
      );
    case "assassination":
      return (
        <Feature cls={`feature-assassin ${d.hit ? "hit" : "miss"}`}>
          <div className="feature-icon"><StageIcon name="assassin" size={64} /></div>
          <div className="feature-title">{circ(d.assassin_no)}号 刺向 {circ(d.target_no)}号</div>
          <div className="assassin-line">「{d.text}」</div>
          <div className={`verdict ${d.hit ? "bad" : "ok"}`}>
            {d.hit ? "正中梅林！红方翻盘！" : "刺空了！蓝方守住胜利！"}
          </div>
          <Audio ch={ch} item={last} url={d.audio_url} />
        </Feature>
      );
    case "game_over":
      return (
        <Feature cls={`feature-over ${d.winner}`}>
          <div className="feature-title big">{d.winner === "good" ? "蓝方胜利" : "红方胜利"}</div>
          <div className="feature-sub">{d.win_reason}</div>
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

function Spotlight({ ch, item, seat, no, tag, text, monologue, audio, team, ladyNo, ladyEvil }: {
  ch: Channel; item: FeedItem; seat: SeatInfo | undefined; no: number; tag: string;
  text: string; monologue: string; audio: string | null;
  team?: number[]; ladyNo: number | null; ladyEvil: boolean;
}) {
  return (
    <div className="spotlight">
      <div className="spot-head">
        <div className="spot-avatar" style={seat ? avatarStyle(seat.persona) : undefined}>{seat?.name.slice(0, 1) ?? "?"}</div>
        <div className="spot-id">
          <div className="spot-name">{circ(no)} 号</div>
          <div className="spot-tag">
            {tag}{seat ? <> · <StageIcon name={roleIconName(seat.role)} size={18} /> {roleLabel(seat.role)}</> : ""}
          </div>
        </div>
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
      {monologue && <div className="spot-mono">💭 {monologue}</div>}
      <Audio ch={ch} item={item} url={audio} />
    </div>
  );
}

function VoteView({ ch, item, d }: {
  ch: Channel; item: FeedItem;
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
  return (
    <div className={`vote-view ${settled ? (d.approved ? "ok" : "bad") : ""}`}>
      <div className="vote-title">{settled ? (d.approved ? "表决通过" : "表决被否") : "表决中…"}</div>
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

function LiveText({ ch, item, text }: { ch: Channel; item: FeedItem; text: string }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 50);
    return () => window.clearInterval(t);
  }, [item.seq]);
  const budget = Math.max(item.duration * 0.62, 0.001);
  const n = Math.min(Math.floor((elapsedOf(ch, item.started_at, now) / budget) * text.length), text.length);
  return <>{text.slice(0, Math.max(n, 0))}{n < text.length && <span className="caret">▌</span>}</>;
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
      {d.last_winner && <div className="curtain-sub">上局：{d.last_winner === "good" ? "蓝方胜" : "红方胜"}（{d.last_reason}）</div>}
      <div className="curtain-sub">{d.next_ready ? "下一局即将开始" : "下一局排片中…"}</div>
    </div>
  );
}
