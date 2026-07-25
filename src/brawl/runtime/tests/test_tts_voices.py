"""TTS 只消费 replay 冻结的阿里云音色与严格 Sentence。"""

from __future__ import annotations

from pathlib import Path

import pytest

from brawl.runtime.tts import TTSError, TTSRenderer
from brawl.runtime.types import Delivery, Sentence


class _Backend:
    name = "fake"
    fingerprint = "fake:v1"

    async def synthesize(self, text: str, voice_id: str) -> bytes:  # pragma: no cover - 本文件不跑合成
        raise AssertionError("unexpected synthesis")


def _renderer(tmp_path: Path) -> TTSRenderer:
    return TTSRenderer(_Backend(), tmp_path)


def test_nonspoken_delivery_stays_in_log_but_is_skipped_by_tts(tmp_path: Path) -> None:
    utterances = _renderer(tmp_path)._utterances(
        [
            Sentence(
                seq=3,
                commit_id=1,
                turn_id="host.result",
                speaker="host",
                deliveries=[
                    Delivery(text="【符号账本】1号✔", channels=["broadcast"], spoken=False),
                    Delivery(text="口语播报结果", channels=["broadcast"]),
                ],
            )
        ],
        ["player"],
        "host",
    )
    assert len(utterances) == 1
    assert utterances[0].text == "口语播报结果"
    assert utterances[0].voice_id == "host"


def test_audience_only_is_silent_but_mixed_public_delivery_is_spoken(tmp_path: Path) -> None:
    utterances = _renderer(tmp_path)._utterances(
        [
            Sentence(
                seq=0,
                commit_id=0,
                turn_id="player.thought",
                speaker=1,
                deliveries=[Delivery(text="内心戏", channels=["audience"])],
            ),
            Sentence(
                seq=1,
                commit_id=1,
                turn_id="host.opening",
                speaker="host",
                deliveries=[Delivery(text="公开主持词", channels=["broadcast", "audience"])],
            ),
        ],
        ["player"],
        "host",
    )
    assert [(u.call_id, u.text) for u in utterances] == [("1.host", "公开主持词")]


def test_missing_seat_voice_fails_at_tts_projection_boundary(tmp_path: Path) -> None:
    sentence = Sentence(
        seq=0,
        commit_id=0,
        turn_id="player.speak",
        speaker=2,
        deliveries=[Delivery(text="发言", channels=["broadcast"])],
    )
    with pytest.raises(TTSError, match="缺少 2 号席位音色"):
        _renderer(tmp_path)._utterances([sentence], ["only-seat-1"], "host")


def test_speech_delivery_is_voiced_bare_without_attribution(tmp_path: Path) -> None:
    """kind="speech" 的 text 是裸发言：归属只渲染进 LLM inbox，TTS 念原文、不自报号码。"""
    utterances = _renderer(tmp_path)._utterances(
        [
            Sentence(
                seq=5,
                commit_id=2,
                turn_id="q1.a1.discuss.0",
                speaker=1,
                deliveries=[
                    Delivery(text="6号说的有道理", channels=["broadcast"], kind="speech"),
                    Delivery(text="内心戏", channels=["audience"]),
                ],
            )
        ],
        ["voice-1"],
        "host",
    )
    assert [u.text for u in utterances] == ["6号说的有道理"]
