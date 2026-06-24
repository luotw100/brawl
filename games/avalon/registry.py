"""加载可调内容：LLM Provider 池、人设库、流派库。全部来自 avalon/data/*.yaml。

人设、流派、模型三轴彻底解耦：Persona 只有"怎么说话"（嗓音），Strategy 只有"怎么赢"
（打法哲学），ModelSpec 只有接入信息（智商），运行时由 settings 的三套座位映射独立配起来。
当前 random 后端不调模型，但三轴已就位——LLM 后端落地时只补 LLMPlayer/prompts，不动配置面。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

DATA_DIR = Path(__file__).parent / "data"


class ModelSpec(BaseModel):
    """一个逻辑模型的 OpenAI 兼容接入规格。"""

    name: str
    base_url: str
    model_id: str
    api_key_env: str
    temperature: float = 0.9
    max_tokens: int | None = None  # None = 不设上限，由 timeout 兜底（开 thinking 后推理长，别截断）
    timeout: float = 180  # 单次调用客户端超时（秒）；开 thinking 后推理长，给足。超时算"不符预期"，由 player 层退避重试
    extra_body: dict[str, Any] = Field(default_factory=dict)  # 透传给 API 的额外参数，如开/关思考
    avatar: str | None = None  # 厂商 logo key，指向 data/avatars/<avatar>.png；录制时按座位烧进 replay（见 Registry.avatar_path）。缺省=该模型不带头像，渲染层走 hash 色回退
    display: str | None = None  # 对外展示的模型名（如 "DeepSeek v4 Pro"）；录制时把"逻辑名→display"映射烧进 meta.model_labels 供直播显在座位下。不带厂商名——头像已表明厂商


class Persona(BaseModel):
    """一个人设：只管"怎么说话"，不绑定模型。name 是直播渲染层的网名，绝不进 prompt。"""

    id: str
    name: str
    tone: str = ""  # 腔调：语气/节奏/态度的二人称描述，进 prompt。玩家各写各的（当前全员同一句
    #                 中性，将来戏子层要分化时逐个改）；主持人不发言、无 tone（hosts.yaml 不写）。
    voice_id: str = ""  # 阿里云 TTS 音色 id（brawl voice-forge 铸造），离线配音用，不进 prompt
    voice_design: str = ""  # 铸该音色用的 VD 嗓音描述（备份导线），不进 prompt


class Strategy(BaseModel):
    """一个流派：只管"怎么赢"（风险偏好与主动性），不绑定人设与模型。"""

    id: str
    name: str
    system: str


@dataclass
class Registry:
    """data/ 下可调内容的内存视图。玩家人设(personas/cast.yaml)与主持人人设(hosts/hosts.yaml)
    是两套独立集合——数据源头就分开，玩家池里绝不会混进主持人，核心无需过滤。"""

    models: dict[str, ModelSpec]
    personas: dict[str, Persona]  # 玩家人设（cast.yaml）
    hosts: dict[str, Persona]  # 主持人人设（hosts.yaml）；只用 name/voice_id，不发言、无 tone
    strategies: dict[str, Strategy]
    avatars_dir: Path

    def avatar_path(self, model_name: str | None) -> Path | None:
        """该模型厂商 logo 的源文件（data/avatars/<avatar>.png）；模型不在池 / 没配 avatar /
        文件缺失都返回 None——录制层据此决定烧不烧入（缺则回放渲染走 hash 色回退）。"""
        spec = self.models.get(model_name) if model_name is not None else None
        if spec is None or not spec.avatar:
            return None
        path = self.avatars_dir / f"{spec.avatar}.png"
        return path if path.is_file() else None

    def model_names(self) -> list[str]:
        return list(self.models)

    def persona_ids(self) -> list[str]:
        return list(self.personas)

    def host_ids(self) -> list[str]:
        return list(self.hosts)

    def strategy_ids(self) -> list[str]:
        return list(self.strategies)


def load_registry(data_dir: Path = DATA_DIR) -> Registry:
    models_raw = yaml.safe_load((data_dir / "models.yaml").read_text(encoding="utf-8"))
    cast_raw = yaml.safe_load((data_dir / "cast.yaml").read_text(encoding="utf-8"))
    hosts_raw = yaml.safe_load((data_dir / "hosts.yaml").read_text(encoding="utf-8"))
    strategies_raw = yaml.safe_load((data_dir / "strategies.yaml").read_text(encoding="utf-8"))

    models = {m["name"]: ModelSpec(**m) for m in models_raw["models"]}
    personas = {p["id"]: Persona(**p) for p in cast_raw["personas"]}
    hosts = {h["id"]: Persona(**h) for h in hosts_raw["hosts"]}
    strategies = {s["id"]: Strategy(**s) for s in strategies_raw["strategies"]}
    return Registry(models=models, personas=personas, hosts=hosts, strategies=strategies, avatars_dir=data_dir / "avatars")
