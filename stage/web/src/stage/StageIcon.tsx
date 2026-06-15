// ============================================================================
// AI 阿瓦隆 · 单线纹章风图标组件（方案 C）
// 放到 stage/web/src/stage/StageIcon.tsx
//
// 设计：统一 1.8px 圆头单线，stroke=currentColor —— 颜色由父元素 color 决定
//   （蓝方角色给蓝、红方角色给红）。crown / lady 是实心金，自带颜色。
// 角色 name 直接复用 protocol 里的 role 字符串
//   （merlin / percival / servant / assassin / morgana / mordred / oberon / minion），
//   外加 crown（队长）、lady（湖中仙女）、herald（报幕，可选）。
// ============================================================================
import type { CSSProperties } from "react";

export type IconName =
  | "crown" | "lady" | "herald"
  | "merlin" | "percival" | "servant"
  | "morgana" | "assassin" | "mordred" | "oberon" | "minion";

const STROKE = {
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.8,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
};

export function StageIcon({ name, size = 22, style }: { name: IconName; size?: number; style?: CSSProperties }) {
  const c = { width: size, height: size, viewBox: "0 0 24 24", style } as const;
  switch (name) {
    // —— 队长：实心金皇冠（浮在头像上方）——
    case "crown":
      return (
        <svg {...c} fill="currentColor" style={{ filter: "drop-shadow(0 2px 3px rgba(0,0,0,.6))", ...style }}>
          <path d="M2 8.2l4.6 3.1L12 4l5.4 7.3L22 8.2 20 19.5H4L2 8.2z" />
          <circle cx="12" cy="5" r="1.4" />
        </svg>
      );
    // —— 湖中仙女：水滴令牌 ——
    case "lady":
      return (
        <svg {...c} {...STROKE}>
          <path d="M12 3.4s6.6 6.6 6.6 11.1a6.6 6.6 0 11-13.2 0C5.4 10 12 3.4 12 3.4z" />
          <path d="M9.5 14.5a2.5 2.5 0 002.5 2.5" strokeWidth={1.3} />
        </svg>
      );
    // —— 报幕 / 主持（host directive）：号角（可选；不用就给 host 留 emoji）——
    case "herald":
      return (
        <svg {...c} {...STROKE}>
          <path d="M4 10v4h3l8 4V6L7 10H4z" />
          <path d="M18 9a4 4 0 010 6" />
        </svg>
      );
    // —— 梅林：全知之眼 ——
    case "merlin":
      return (
        <svg {...c} {...STROKE}>
          <path d="M1.5 12S5.5 5 12 5s10.5 7 10.5 7-4 7-10.5 7S1.5 12 1.5 12z" />
          <circle cx="12" cy="12" r="3" />
        </svg>
      );
    // —— 派西维尔：眼 + 星芒（看得见梅林与莫甘娜）——
    case "percival":
      return (
        <svg {...c} {...STROKE}>
          <path d="M2 12.5S5.5 6.5 12 6.5s10 6 10 6-3.5 6-10 6-10-6-10-6z" />
          <circle cx="12" cy="12.5" r="2.6" />
          <path d="M19 3l.9 2.1L22 6l-2.1.9L19 9l-.9-2.1L16 6l2.1-.9L19 3z" fill="currentColor" stroke="none" />
        </svg>
      );
    // —— 忠臣：盾 ——
    case "servant":
      return (
        <svg {...c} {...STROKE}>
          <path d="M12 2.5l7.5 2.8v6c0 4.7-3.2 8.4-7.5 10.2-4.3-1.8-7.5-5.5-7.5-10.2v-6L12 2.5z" />
        </svg>
      );
    // —— 莫甘娜：威尼斯面具（伪装成梅林）——
    case "morgana":
      return (
        <svg {...c} {...STROKE}>
          <path d="M2.5 8.5c3-1 6-1.2 9.5-1.2s6.5.2 9.5 1.2c.2 5-2.7 8.3-6 8.3-2 0-2.8-1.6-3.5-1.6s-1.5 1.6-3.5 1.6c-3.3 0-6.2-3.3-6-8.3z" />
          <circle cx="8" cy="10.5" r="1.1" fill="currentColor" stroke="none" />
          <circle cx="16" cy="10.5" r="1.1" fill="currentColor" stroke="none" />
        </svg>
      );
    // —— 刺客：匕首 ——
    case "assassin":
      return (
        <svg {...c} {...STROKE}>
          <path d="M12 2l1.6 11.5h-3.2L12 2z" />
          <path d="M7 14h10" />
          <path d="M12 16v6" />
        </svg>
      );
    // —— 莫德雷德：暗冠 / 角冠（梅林看不见他）——
    case "mordred":
      return (
        <svg {...c} {...STROKE}>
          <path d="M3 9l2.5-3.5L8 9l4-5 4 5 2.5-3.5L21 9l-1.8 9.5H4.8L3 9z" />
        </svg>
      );
    // —— 奥伯伦：孤月（坏人也不认识他）——
    case "oberon":
      return (
        <svg {...c} fill="currentColor" style={style}>
          <path d="M20.5 13.8A8 8 0 1110 4a6.2 6.2 0 0010.5 9.8z" />
        </svg>
      );
    // —— 爪牙：兜帽随从 ——
    case "minion":
      return (
        <svg {...c} {...STROKE}>
          <path d="M5 11c0-4 3-6.5 7-6.5s7 2.5 7 6.5c0 5-3 8.5-7 8.5s-7-3.5-7-8.5z" />
          <circle cx="9.5" cy="11" r="1" fill="currentColor" stroke="none" />
          <circle cx="14.5" cy="11" r="1" fill="currentColor" stroke="none" />
        </svg>
      );
  }
}

// role 字符串 → 图标名（与 protocol 的 role 完全同名，这里只是给个类型安全的转换点）
export function roleIconName(role: string): IconName {
  const known: IconName[] = ["merlin", "percival", "servant", "morgana", "assassin", "mordred", "oberon", "minion"];
  return (known as string[]).includes(role) ? (role as IconName) : "servant";
}
