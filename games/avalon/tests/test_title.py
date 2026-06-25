"""avalon-title：replay → LLM 发布文案。纯函数与边界测试，不碰网络。"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from avalon.registry import Registry
from avalon.title import PublishCopy, SettingsError, TitleSettings, build_brief, format_copy, resolve_and_validate


def _write_replay(tmp_path):
    d = tmp_path / "game1"
    d.mkdir()
    meta = {
        "setup": {
            "players": 5,
            "names": ["一号", "二号", "三号", "四号", "五号"],
            "roles": ["merlin", "servant", "assassin", "morgana", "servant"],
            "lady_holder": None,
        }
    }
    events = [
        {"type": "propose", "quest": 1, "attempt": 1, "leader_no": 1, "team": [1, 2], "speech": "我先带二号试水。"},
        {"type": "vote_result", "quest": 1, "attempt": 1, "approves": 5, "rejections": 0, "approved": True},
        {"type": "quest_result", "quest": 1, "team": [1, 2], "fails": 0, "result": "success", "succeeded": 1, "failed": 0},
        {"type": "assassination", "assassin_no": 3, "target_no": 1, "hit": True, "speech": "我认定一号就是梅林。"},
        {"type": "game_over", "winner": "evil", "win_reason": "刺客命中梅林"},
    ]
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    (d / "events.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), encoding="utf-8")
    return d, meta["setup"], events


def test_title_settings_requires_replay_files(tmp_path):
    d = tmp_path / "bad"
    d.mkdir()
    with pytest.raises(ValidationError):
        TitleSettings.load([str(d)])


def test_build_brief_mentions_key_turns(tmp_path):
    _, setup, events = _write_replay(tmp_path)

    brief = build_brief(events, setup)

    assert "人数：5 人" in brief
    assert "梅林" in brief and "刺客" in brief
    assert "任务1 成功" in brief
    assert "刺杀：三号 刺杀 一号，结果命中" in brief
    assert "终局：红方获胜" in brief


def test_format_copy_writes_title_intro_tags():
    copy = PublishCopy(title="刺客最后一刀命中梅林", intro="蓝方任务刚刚起势，刺客终局精准锁定梅林完成翻盘。", tags=["阿瓦隆", "桌游", "AI对局"])

    assert format_copy(copy) == "标题：刺客最后一刀命中梅林\n简介：蓝方任务刚刚起势，刺客终局精准锁定梅林完成翻盘。\n标签：#阿瓦隆 #桌游 #AI对局\n"


def test_resolve_title_model_missing_key_raises(registry: Registry, tmp_path, monkeypatch: pytest.MonkeyPatch):
    d, _, _ = _write_replay(tmp_path)
    settings = TitleSettings.load([str(d), "--model", "oc-deepseek-v4-flash"])
    spec = registry.models[settings.model]
    monkeypatch.delenv(spec.api_key_env, raising=False)

    with pytest.raises(SettingsError):
        resolve_and_validate(settings, registry)
