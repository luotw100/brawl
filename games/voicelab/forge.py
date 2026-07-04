"""`brawl voice-forge`:音色铸造——把一段嗓音描述(VD prompt)铸成可复用音色句柄。

跨玩法通用,完全不认识 cast.yaml:输入嗓音描述,输出 voice_id 和一组试听 wav。

cloud:阿里云百炼 Qwen3-TTS 声音设计(qwen-voice-design 创建 → qwen3-tts-vd 合成)。
self-hosted:局域网 Qwen3-TTS API(VoiceDesign 铸音色 → Base prompt → voice_id)。
"""

from __future__ import annotations

import base64
import datetime
import json
import os
import tempfile
from pathlib import Path
from typing import ClassVar, Literal
from urllib.parse import urljoin

import httpx
from dotenv import dotenv_values
from pydantic import model_validator
from pydantic_settings import (
    BaseSettings,
    CliPositionalArg,
    CliSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)
from rich.console import Console

# 北京地域。创建音色与合成是两个 endpoint;target_model 必须与合成 model 一致。
_API_BASE = "https://dashscope.aliyuncs.com"
_CREATE_URL = f"{_API_BASE}/api/v1/services/audio/tts/customization"
_SYNTH_URL = f"{_API_BASE}/api/v1/services/aigc/multimodal-generation/generation"
_VD_MODEL = "qwen3-tts-vd-2026-01-26"

# 语气样本:同一句话配两种对立指令,一耳朵听出这副嗓子对 instructions 的响应度。
_SAMPLE_TEXT = "好家伙，这就开始投我了？行，那我也没什么好说的了。"
_SAMPLES = {
    "急了": "用非常愤怒急切的语气，语速很快，几乎是吼出来的",
    "阴阳": "用阴阳怪气、慢悠悠的嘲讽语气，拖长音",
}


class SettingsError(Exception):
    """面向用户的错误(参数/凭证/API 失败):cli 入口只打印消息、不打 traceback。"""


class ForgeSettings(BaseSettings):
    """`brawl voice-forge` 的参数面(settings 即 CLI,flag 自动派生)。"""

    model_config = SettingsConfigDict(
        env_prefix="BRAWL_VOICE_FORGE_",
        cli_kebab_case=True,
        cli_implicit_flags=True,
        extra="forbid",
    )

    _cli_args: ClassVar[list[str] | None] = None

    voice_prompt: CliPositionalArg[str]  # 嗓音描述(VD prompt),≤2048 字符
    backend: Literal["qwen3-tts-cloud", "qwen3-tts-self-hosted"] = "qwen3-tts-cloud"
    name: str = "voice"  # 一两个单词的短描述,拼上日期做云端 preferred_name(便于辨认/清理)
    preview_text: str = "我跳梅林？哥们，我要是梅林这局早就赢了好吧。"  # 用 cast 将来真会说的话来听
    emotion_samples: bool = True  # 铸完追加合成两个语气样本(急了/阴阳),验收 instructions 响应度
    out_dir: Path | None = None  # 试听产物目录;None = /tmp 下自动建临时目录
    self_hosted_url: str = "http://127.0.0.1:8010"

    @model_validator(mode="after")
    def _check(self) -> ForgeSettings:
        if not self.voice_prompt.strip():
            raise ValueError("voice_prompt 不能为空")
        if len(self.voice_prompt) > 2048:
            raise ValueError(f"voice_prompt 超长:{len(self.voice_prompt)} > 2048 字符")
        if not self.name.isascii() or not self.name.replace("_", "").isalnum():
            raise ValueError(f"name 仅限字母数字和下划线(进云端 preferred_name),收到 {self.name!r}")
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            CliSettingsSource(
                settings_cls,
                cli_parse_args=cls._cli_args if cls._cli_args is not None else [],
                cli_prog_name="brawl voice-forge",
            ),
            env_settings,
        )

    @classmethod
    def load(cls, cli_args: list[str]) -> ForgeSettings:
        cls._cli_args = cli_args
        try:
            return cls()
        finally:
            cls._cli_args = None


def _api_key() -> str:
    """边界校验:开工前确认凭证就绪。真实环境变量优先,其次 games/.env(对齐各玩法惯例)。"""
    key = os.environ.get("DASHSCOPE_API_KEY") or dotenv_values(Path(__file__).resolve().parents[1] / ".env").get(
        "DASHSCOPE_API_KEY"
    )
    if not key:
        raise SettingsError("games/.env 缺 DASHSCOPE_API_KEY(阿里云百炼 API key)")
    return key


def _post(client: httpx.Client, url: str, payload: dict) -> dict:
    resp = client.post(url, json=payload)
    body = resp.json()
    if resp.status_code != 200:
        raise SettingsError(f"API 调用失败({resp.status_code}):{body.get('code')} {body.get('message')}")
    return body


def _create_voice(client: httpx.Client, s: ForgeSettings) -> tuple[str, bytes]:
    """创建音色:返回 (voice_id, 预览 wav 字节)。"""
    today = datetime.date.today().strftime("%Y%m%d")
    body = _post(
        client,
        _CREATE_URL,
        {
            "model": "qwen-voice-design",
            "input": {
                "action": "create",
                "target_model": _VD_MODEL,
                # 云端 preferred_name 实测只接受字母数字下划线(连字符 400)
                "preferred_name": f"{s.name}_{today}",
                "voice_prompt": s.voice_prompt,
                "preview_text": s.preview_text,
            },
            "parameters": {"sample_rate": 24000, "response_format": "wav"},
        },
    )
    out = body["output"]
    return out["voice"], base64.b64decode(out["preview_audio"]["data"])


def _synthesize(client: httpx.Client, voice_id: str, text: str, instructions: str) -> bytes:
    """用 voice_id + 语气指令合成一句,当场下载(返回的 OSS URL 有有效期)。"""
    body = _post(
        client,
        _SYNTH_URL,
        {"model": _VD_MODEL, "input": {"text": text, "voice": voice_id, "instructions": instructions}},
    )
    return client.get(body["output"]["audio"]["url"]).content


def _forge_cloud(s: ForgeSettings, console: Console) -> None:
    out_dir = s.out_dir or Path(tempfile.mkdtemp(prefix=f"voice-forge-{s.name}-"))
    out_dir.mkdir(parents=True, exist_ok=True)

    with httpx.Client(headers={"Authorization": f"Bearer {_api_key()}"}, timeout=60) as client:
        voice_id, preview = _create_voice(client, s)
        (out_dir / "preview.wav").write_bytes(preview)
        console.print(f"音色已创建:[bold]{voice_id}[/bold]")
        console.print(f"预览:{out_dir / 'preview.wav'}")

        if s.emotion_samples:
            for label, instructions in _SAMPLES.items():
                path = out_dir / f"sample_{label}.wav"
                path.write_bytes(_synthesize(client, voice_id, _SAMPLE_TEXT, instructions))
                console.print(f"语气样本[{label}]:{path}")

    # 留一份导线:听完隔了几天也能对上"这堆 wav 是哪段描述铸的"。
    (out_dir / "forge.json").write_text(
        json.dumps({"voice_id": voice_id, "voice_prompt": s.voice_prompt, "preview_text": s.preview_text},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    console.print("\n听满意后,把下面片段拷进玩法的 cast.yaml 对应 persona:\n")
    console.print(f'    voice_id: {voice_id}\n    voice_design: "{s.voice_prompt}"')


def _create_self_hosted_voice(client: httpx.Client, s: ForgeSettings) -> dict:
    resp = client.post(
        f"{s.self_hosted_url.rstrip('/')}/v1/voices",
        json={
            "name": s.name,
            "voice_prompt": s.voice_prompt,
            "preview_text": s.preview_text,
            "language": "Chinese",
            "overwrite": False,
        },
    )
    if resp.status_code != 200:
        raise SettingsError(f"自托管音色创建失败({resp.status_code}):{_response_text(resp)}")
    return resp.json()


def _self_hosted_synthesize(client: httpx.Client, s: ForgeSettings, voice_id: str, text: str) -> bytes:
    resp = client.post(
        f"{s.self_hosted_url.rstrip('/')}/v1/audio/speech",
        json={"voice": voice_id, "text": text, "language": "Chinese", "format": "wav"},
    )
    if resp.status_code != 200:
        raise SettingsError(f"自托管合成失败({resp.status_code}):{_response_text(resp)}")
    return resp.content


def _forge_self_hosted(s: ForgeSettings, console: Console) -> None:
    out_dir = s.out_dir or Path(tempfile.mkdtemp(prefix=f"voice-forge-{s.name}-"))
    out_dir.mkdir(parents=True, exist_ok=True)

    with httpx.Client(timeout=300) as client:
        voice = _create_self_hosted_voice(client, s)
        voice_id = voice["voice_id"]
        preview_url = urljoin(s.self_hosted_url.rstrip("/") + "/", voice["preview_audio_url"])
        preview = client.get(preview_url)
        if preview.status_code != 200:
            raise SettingsError(f"自托管预览下载失败({preview.status_code}):{_response_text(preview)}")
        (out_dir / "preview.wav").write_bytes(preview.content)
        console.print(f"自托管音色已创建:[bold]{voice_id}[/bold]")
        console.print(f"预览:{out_dir / 'preview.wav'}")

        if s.emotion_samples:
            sample = out_dir / "sample.wav"
            sample.write_bytes(_self_hosted_synthesize(client, s, voice_id, _SAMPLE_TEXT))
            console.print(f"合成样本:{sample}")

    (out_dir / "forge.json").write_text(
        json.dumps(
            {
                "backend": s.backend,
                "voice_id": voice_id,
                "voice_prompt": s.voice_prompt,
                "preview_text": s.preview_text,
                "self_hosted_url": s.self_hosted_url,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    console.print("\n听满意后,把下面片段拷进玩法的 cast.yaml 对应 persona:\n")
    console.print(f'    self_hosted_voice_id: {voice_id}\n    voice_design: "{s.voice_prompt}"')


def _response_text(resp: httpx.Response) -> str:
    try:
        return str(resp.json())
    except ValueError:
        return resp.text[:500]


def forge(s: ForgeSettings, console: Console) -> None:
    if s.backend == "qwen3-tts-cloud":
        _forge_cloud(s, console)
    elif s.backend == "qwen3-tts-self-hosted":
        _forge_self_hosted(s, console)


def main(argv: list[str]) -> None:
    forge(ForgeSettings.load(argv), Console())


__all__ = ["ForgeSettings", "SettingsError", "main"]
