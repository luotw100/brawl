#!/usr/bin/env python3
"""离线验证「戏子」管线：拿今天 10 局的理性发言当『军师意图』代理，用花腔人设 restyle，
对比 原发言 / 戏子(无回顾) / 戏子(给最近2句台面)。零仓库改动，只读 AvalonGames + 直连 API。

跑法: python3 restyle_exp.py            # 默认挑几个有冲突的精彩瞬间
      PERFORMER=kimi-k2.6 python3 ...   # 换戏子模型
"""
import os, json, sys, re, textwrap, urllib.request

GAMES = os.path.expanduser("~/Downloads/AvalonGames")
ENVF  = os.path.expanduser("~/code/brawl/games/.env")
PERFORMER = os.environ.get("PERFORMER", "deepseek-v4-pro")
ENDPOINT = "https://opencode.ai/zen/go/v1/chat/completions"

# 旧花腔人设（git a3f8d53），实验把理性发言 restyle 回这些腔调
VOICES = {
 "fox":"你说话慢条斯理，话总说七分留三分，爱用反问把问题抛回去，让人猜不透你站哪边。",
 "janitor":"你见谁怼谁，别人说什么你先挑刺，语气冲但句句对事不对人，被驳倒了也要嘴硬两句。",
 "diva":"你情绪外放、反应夸张，说话全是大白话不掉书袋，语速偏快、爱接话爱起哄，自带直播间气氛。",
 "shy":"你说话小声、磕磕绊绊，开口前总要先铺垫两句客气话，但该说的事实和怀疑一件不漏。",
 "shade":"你从不直接骂人，全程阴阳怪气、夹枪带棒，夸人的话从你嘴里说出来都像在损人。",
 "honest":"你说话实在巴交、有啥说啥，不绕弯也不修饰，偶尔实在得让全场哭笑不得。",
 "boss":"你一身江湖气，说话直来直去爱拍板，开口就带节奏，不服就当场对线。",
 "prof":"你咬文嚼字，发言爱分点列条，把推理摆得一板一眼，时不时引经据典。",
 "bard":"你出口成章，爱用比喻、顺口溜甚至打油诗点评局势，把桌上的尔虞我诈说成故事。",
 "deadpan":"你语速平、不带情绪，一副事不关己的腔调，但每句话都在不动声色地精准补刀。",
}

def load_key():
    for line in open(ENVF):
        if line.startswith("OPENCODE_API_KEY="):
            return line.split("=",1)[1].strip()
    sys.exit("no OPENCODE_API_KEY")
KEY = load_key()

import time
LAST_MS = 0
def call(model, system, user, timeout=300):
    global LAST_MS
    body = json.dumps({"model":model,"temperature":0.8,
        "messages":[{"role":"system","content":system},{"role":"user","content":user}]}).encode()
    req = urllib.request.Request(ENDPOINT, body,
        {"Authorization":f"Bearer {KEY}","Content-Type":"application/json",
         "User-Agent":"curl/8.5.0","Accept":"application/json"})
    t0=time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    LAST_MS = int((time.time()-t0)*1000)
    return d["choices"][0]["message"]["content"].strip()

STATELESS_ONLY = os.environ.get("STATELESS_ONLY","") == "1"

def speech_of(raw):
    m = re.search(r"发言：(.+?)(?:\n队伍：|\n刺杀：|\Z)", raw or "", re.S)
    return m.group(1).strip() if m else None

PERF_SYS = ("你是桌游《阿瓦隆》直播里某位玩家的『嘴替』。给你这位玩家本轮想公开表达的内容，"
    "它的策略信息已经定死——所有点名的座位号、票面立场、指控、承诺、结论你都不许增、删、改，"
    "只许换一种腔调把同样的意思重讲一遍。不要加新的指控或新的座位号。只输出重述后的发言本身，一行，别带「发言：」标签。")

def restyle(voice, intent, recent=None):
    u = f"你的人设腔调：{voice}\n\n本轮要表达的内容（策略不可改）：\n{intent}"
    if recent:
        u += "\n\n刚才台面上最近的发言（你可以现挂、接梗、回怼，但仍不许改自己上面的策略内容）：\n" + recent
    u += "\n\n用你的腔调把『要表达的内容』重讲一遍："
    return call(PERFORMER, PERF_SYS, u)

def public_speeches(game):
    """按发生序的 (seat, type, speech)，用于取 target 之前的最近台面原话。"""
    seq=[]
    for line in open(f"{GAMES}/{game}/events.jsonl"):
        o=json.loads(line)
        if o.get("type") in ("propose","discuss") and o.get("speech"):
            seq.append((o.get("seat_no") or o.get("leader_no"), o["type"], o["speech"]))
    return seq

# 手挑几个有看点的瞬间：(game, phase, quest, seat)；覆盖 bard/prof/boss/janitor/shade 等花腔
PICKS = [
 ("06161215","propose",1,5),
 ("06161215","discuss",2,2),
 ("06161219","propose",2,1),
 ("06161219","discuss",1,4),
 ("06161222","discuss",1,3),
]

def main():
    print(f"# 戏子 = {PERFORMER}\n")
    for game, phase, quest, seat in PICKS:
        cast = json.load(open(f"{GAMES}/{game}/meta.json"))["setup"]["cast"]
        persona = cast[seat-1]; voice = VOICES[persona]
        # 找到该 call 的原始发言
        raw=None
        for line in open(f"{GAMES}/{game}/llm_calls.jsonl"):
            o=json.loads(line)
            if o["phase"]==phase and o["quest"]==quest and o["seat_no"]==seat and not o["phase"].endswith(".extract"):
                raw=o["output"]; break
        intent = speech_of(raw)
        if not intent:
            print(f"[skip {game} q{quest} {seat}号 {phase}: 没抓到发言]\n"); continue
        # 最近两句台面（该发言之前）
        seq = public_speeches(game)
        recent_lines=[f"{s}号：{sp}" for s,_,sp in seq if sp!=intent][-2:]
        recent = "\n".join(recent_lines)
        print("="*90)
        print(f"{game} · 第{quest}任务 · {seat}号 · 人设={persona}({phase})")
        print("-"*90)
        print("【原·理性发言】\n " + textwrap.fill(intent, 86, subsequent_indent="  "))
        try:
            a = restyle(voice, intent)
            print(f"\n【戏子·无回顾·{persona}腔·{LAST_MS}ms】\n " + textwrap.fill(a, 86, subsequent_indent="  "))
        except Exception as e:
            print(f"\n【戏子·无回顾】调用失败：{e}")
        if not STATELESS_ONLY:
            try:
                b = restyle(voice, intent, recent)
                print(f"\n【戏子·给最近2句·{persona}腔·{LAST_MS}ms】\n " + textwrap.fill(b, 86, subsequent_indent="  "))
                print(f"  (回顾喂入：{recent[:60]}…)" if recent else "  (无前文)")
            except Exception as e:
                print(f"\n【戏子·给最近2句】调用失败：{e}")
        print()

if __name__=="__main__":
    main()
