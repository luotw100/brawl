"""合成演出层音效（占位素材，ffmpeg 本地生成，构造即 CC0、无署名）。

为什么自合成：构建环境无外网，下不到 Freesound/Pixabay 的素材。这些 .opus 是**可热插拔**
的占位——拿到更好的无版权素材后，同名覆盖 web/public/sfx/<name>.opus 即可，前后端代码不动
（cue 名是唯一契约，见 web/src/stage/sfx.ts 与 server/sfx.py）。

每条音效用纯 ffmpeg lavfi 生成：音符型走 aevalsrc（正弦叠加 + 指数衰减包络 + 软起音），
噪声型（洗牌/盖章/命中）走 anoisesrc + 带通/淡入淡出。统一 alimiter 压峰防爆、尾部 5ms
淡出去爆音，输出单声道 opus（与 TTS 同格式）。

用法：python -m tools.gen_sfx [输出目录]   默认写到 web/public/sfx/
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SR = 48000


def _term(f: float, ts: float, k: float, a: float) -> str:
    """一个在 ts 秒起音、指数衰减(k)的正弦音：幅度 a、频率 f。

    gte(t,ts) 把起音前压零；(1-exp(-90*dt)) 给个几毫秒软起音消除 t=ts 处的爆音。"""
    dt = f"(t-{ts})"
    return f"{a}*sin(2*PI*{f}*t)*exp(-{k}*{dt})*(1-exp(-90*{dt}))*gte(t,{ts})"


def _tone(*terms: str) -> str:
    return "+".join(terms)


# 音符频率（Hz）
G3, A3, C4, Cs4, E4, F4, G4 = 196.0, 220.0, 261.63, 277.18, 329.63, 349.23, 392.0
Bb3 = 233.08
A4, C5, E5, F5_, G5, A5, C6 = 440.0, 523.25, 659.25, 349.23, 783.99, 880.0, 1046.5

# 音符型：name -> (aevalsrc 表达式, 时长秒)
TONES: dict[str, tuple[str, float]] = {
    # 开场号角：G4→C5→E5 琶音后落到 C-E-G 大三和弦
    "open": (_tone(
        _term(G4, 0, 7, .34), _term(C5, .10, 7, .34), _term(E5, .20, 6, .32),
        _term(C5, .30, 2.2, .17), _term(E5, .30, 2.2, .17), _term(G5, .30, 2.2, .19),
    ), 1.0),
    # 胜（蓝）：C5-E5-G5-C6 上行琶音 + 明亮大三和弦长收
    "win-good": (_tone(
        _term(C5, 0, 8, .3), _term(E5, .12, 8, .3), _term(G5, .24, 8, .3), _term(C6, .36, 6, .3),
        _term(C5, .48, 2.0, .16), _term(E5, .48, 2.0, .16), _term(G5, .48, 2.0, .16), _term(C6, .48, 2.2, .14),
    ), 1.7),
    # 胜（红）：E4-C4-A3 下行 + 低沉小三和弦 + 低音鼓底
    "win-evil": (_tone(
        _term(E4, 0, 7, .3), _term(C4, .14, 7, .3), _term(A3, .28, 7, .3),
        _term(A3, .42, 1.8, .18), _term(C4, .42, 1.8, .18), _term(E4, .42, 1.8, .16),
        _term(110, .42, 2.2, .22),
    ), 1.7),
    # 思考叮：钟铃两分音（基频 + 1.5 倍泛音），轻而短
    "think": (_tone(_term(880, 0, 9, .2), _term(1320, 0, 12, .09)), 0.45),
    # 盖章：低频"咚"叠中频敲击，极短
    "stamp": (_tone(_term(150, 0, 28, .55), _term(90, 0, 30, .42), _term(300, 0, 60, .22)), 0.18),
    # 表决通过：E5→A5 上行 + C6 点缀
    "vote-pass": (_tone(_term(E5, 0, 7, .3), _term(A5, .12, 6, .32), _term(C6, .24, 7, .18)), 0.55),
    # 表决否决：A4→F4 下行 + 拍频"嗡"（349 与 353 打架）
    "vote-fail": (_tone(_term(A4, 0, 6, .3), _term(F4, .14, 5, .32), _term(353, .14, 5, .14)), 0.55),
    # 任务成功：C-E-G-C 大三和弦短促亮击
    "quest-success": (_tone(
        _term(C5, 0, 3.2, .22), _term(E5, 0, 3.2, .22), _term(G5, 0, 3.2, .22), _term(C6, 0, 3.6, .18),
    ), 0.7),
    # 任务失败：G3-Bb3-C#4 低沉不协和 + 低音
    "quest-fail": (_tone(
        _term(G3, 0, 3.2, .28), _term(Bb3, 0, 3.2, .24), _term(Cs4, 0, 3.4, .2), _term(98, 0, 3.6, .26),
    ), 0.7),
}


def _tone_cmd(expr: str, dur: float, out: Path) -> list[str]:
    return [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"aevalsrc='{expr}':d={dur}:s={SR}",
        "-af", f"alimiter=limit=0.9,afade=t=out:st={max(dur - 0.05, 0):.3f}:d=0.05",
        "-ac", "1", "-c:a", "libopus", "-b:a", "96k", str(out),
    ]


def _noise_cmd(src: str, af: str, out: Path) -> list[str]:
    return [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", src,
        "-af", f"{af},alimiter=limit=0.9",
        "-ac", "1", "-c:a", "libopus", "-b:a", "96k", str(out),
    ]


# 噪声型：name -> (lavfi 源, 滤镜链)
NOISES: dict[str, tuple[str, str]] = {
    # 洗牌 whoosh：粉噪带通 + 进出淡
    "deal": ("anoisesrc=d=0.45:c=pink:a=0.6",
             "highpass=f=1200,lowpass=f=6500,afade=t=in:st=0:d=0.05,afade=t=out:st=0.22:d=0.23,volume=2.2"),
    # 命中：白噪爆裂高通 + 快速衰减（暴击脆响）
    "assassin-hit": ("anoisesrc=d=0.4:c=white:a=0.9",
                     "highpass=f=900,afade=t=out:st=0.04:d=0.34,volume=1.0"),
    # 落空：下行"咻"——线性扫频 600→120Hz 的正弦，配衰减包络
    "assassin-miss": ("aevalsrc='0.32*sin(2*PI*(600*t+(120-600)/(2*0.5)*t*t))*exp(-3.2*t)':d=0.5:s=48000",
                      "afade=t=out:st=0.42:d=0.08"),
}


def generate(out_dir: Path) -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    made: list[str] = []
    for name, (expr, dur) in TONES.items():
        subprocess.run(_tone_cmd(expr, dur, out_dir / f"{name}.opus"), check=True)
        made.append(name)
    for name, (src, af) in NOISES.items():
        subprocess.run(_noise_cmd(src, af, out_dir / f"{name}.opus"), check=True)
        made.append(name)
    return made


def main(argv: list[str]) -> int:
    default = Path(__file__).resolve().parent.parent / "web" / "public" / "sfx"
    out_dir = Path(argv[0]) if argv else default
    made = generate(out_dir)
    print(f"✓ 生成 {len(made)} 条音效 → {out_dir}")
    for n in made:
        print(f"  {n}.opus")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
