"""Replay directives -> deterministic stage play plan.

The compiler is intentionally game-agnostic. Game-specific timing, host
placement, and sound cues live in YAML plan files in each game package (``<game>/plan.yaml``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from brawl.stage.protocol import AudioTrack, Directive, HostSpeech, NarrationCue
from brawl.stage.sfx import SFX_VOLUME

_DEFAULT_FALLBACK_CPS = 1 / 0.06
_DEFAULT_AUDIO_MARGIN = 0.5
_DEFAULT_NARRATION_MARGIN = 0.2


class PlanError(ValueError):
    """Raised when the stage plan DSL cannot cover a directive."""


class StagePlanSpec(BaseModel):
    version: int
    audio: dict[str, float] = Field(default_factory=dict)
    host: dict[str, Any] = Field(default_factory=dict)
    directives: dict[str, dict[str, Any]]

    @property
    def fallback_cps(self) -> float:
        return self.audio.get("fallback_cps", _DEFAULT_FALLBACK_CPS)

    @property
    def audio_margin(self) -> float:
        return self.audio.get("audio_margin", _DEFAULT_AUDIO_MARGIN)

    @property
    def narration_margin(self) -> float:
        return self.audio.get("narration_margin", _DEFAULT_NARRATION_MARGIN)


@dataclass
class StageBeat:
    directive: Directive
    duration: float = 0.0
    content_at: float = 0.0
    narrations: list[NarrationCue] = field(default_factory=list)
    tracks: list[AudioTrack] = field(default_factory=list)


def load_plan(path: Path) -> StagePlanSpec:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return StagePlanSpec.model_validate(data)


def compile_stage_beats(
    directives: list[Directive],
    spec: StagePlanSpec,
    durations: dict[str, float],
    *,
    speed: float,
) -> list[StageBeat]:
    compiler = _Compiler(spec, durations, speed=speed)
    return compiler.compile(directives)


class _Compiler:
    def __init__(self, spec: StagePlanSpec, durations: dict[str, float], *, speed: float) -> None:
        self.spec = spec
        self.durations = durations
        self.speed = speed

    def compile(self, directives: list[Directive]) -> list[StageBeat]:
        beats: list[StageBeat] = []
        pending: list[HostSpeech] = []

        for directive in directives:
            if isinstance(directive, HostSpeech):
                placement = self._host_placement(directive)
                if placement == "skip":
                    continue
                if placement == "lead_in_next":
                    pending.append(directive)
                    continue
                if placement == "append_previous" and beats:
                    self._append_host(beats[-1], directive)
                    beats[-1].duration = self._duration(beats[-1])
                    continue
                if placement == "attach_next" or not beats:
                    pending.append(directive)
                    continue
                raise PlanError(f"unsupported host placement {placement!r} for beat {directive.beat!r}")

            beat = StageBeat(directive=directive)
            at = 0.0
            for host in pending:
                at = self._add_host_track(beat, host, at)
            beat.content_at = at
            pending = []
            self._add_main_track(beat)
            self._add_cue_tracks(beat)
            beat.duration = self._duration(beat)
            beats.append(beat)

        if pending and beats:
            for host in pending:
                self._append_host(beats[-1], host)
                beats[-1].duration = self._duration(beats[-1])
        return beats

    def _host_placement(self, host: HostSpeech) -> str:
        placements = self.spec.host.get("placements", {})
        return placements.get(host.beat, self.spec.host.get("default", "append_previous"))

    def _append_host(self, beat: StageBeat, host: HostSpeech) -> None:
        beat.tracks = [track for track in beat.tracks if track.kind != "sfx"]
        at = 0.0
        if self._directive_spec(beat.directive).get("post_narration_after_main", False):
            at = beat.content_at + self._content_duration(beat.directive)
        for track in beat.tracks:
            if track.kind != "sfx":
                at = max(at, track.start + track.duration)
        self._add_host_track(beat, host, at)
        self._add_cue_tracks(beat)

    def _add_host_track(self, beat: StageBeat, host: HostSpeech, at: float) -> float:
        duration = self._narration_duration(host)
        cue = NarrationCue(
            name=host.name,
            beat=host.beat,
            text=host.text,
            audio_url=host.audio_url,
            at=at,
            duration=duration,
        )
        beat.narrations.append(cue)
        if host.audio_url:
            beat.tracks.append(AudioTrack(
                kind="narration",
                audio_url=host.audio_url,
                start=at,
                duration=duration,
                text=host.text,
                name=host.name,
                beat=host.beat,
            ))
        return at + duration

    def _add_main_track(self, beat: StageBeat) -> None:
        audio_url = getattr(beat.directive, "audio_url", None)
        if not audio_url:
            return
        beat.tracks.append(AudioTrack(
            kind="main",
            audio_url=audio_url,
            start=beat.content_at,
            duration=self._audio_duration(audio_url, self.spec.audio_margin),
            text=self._first_text(beat.directive),
        ))

    def _add_cue_tracks(self, beat: StageBeat) -> None:
        duration = self._cue_duration(beat)
        for cue in self._cue_specs(beat.directive):
            for name, at in self._expand_cue(beat.directive, cue, duration):
                beat.tracks.append(AudioTrack(
                    kind="sfx",
                    audio_url=f"/sfx/{name}.opus",
                    start=at,
                    duration=0.0,
                    name=name,
                    volume=SFX_VOLUME,
                ))

    def _cue_duration(self, beat: StageBeat) -> float:
        if self._directive_spec(beat.directive).get("post_narration_after_main", False):
            return beat.content_at + self._content_duration(beat.directive)
        return self._duration_without_cues(beat)

    def _duration(self, beat: StageBeat) -> float:
        content = self._duration_without_cues(beat)
        for track in beat.tracks:
            if track.kind != "sfx":
                content = max(content, track.start + track.duration)
        return content

    def _duration_without_cues(self, beat: StageBeat) -> float:
        narration_end = max((n.at + n.duration for n in beat.narrations), default=0.0)
        if beat.narrations and self._directive_spec(beat.directive).get("reveal_paced_by_narration", False):
            return narration_end
        return beat.content_at + self._content_duration(beat.directive)

    def _content_duration(self, directive: Directive) -> float:
        visual = self._visual_spec_seconds(directive)
        audio = self._main_audio_seconds(directive)
        if audio is not None:
            visual = max(visual, audio + self.spec.audio_margin)
        return visual / self.speed

    def _narration_duration(self, host: HostSpeech) -> float:
        audio = self._raw_audio_duration(host.audio_url or "")
        if audio is not None:
            return (audio + self.spec.narration_margin) / self.speed
        content = self._text_seconds(host.text)
        if content > 0:
            content += self.spec.narration_margin
        return content / self.speed

    def _audio_duration(self, audio_url: str, margin: float) -> float:
        audio = self._raw_audio_duration(audio_url)
        return ((audio + margin) if audio is not None else 0.0) / self.speed

    def _raw_audio_duration(self, audio_url: str) -> float | None:
        return self.durations.get(audio_url)

    def _visual_spec_seconds(self, directive: Directive) -> float:
        spec = self._directive_spec(directive).get("content", {})
        total = float(spec.get("fixed", 0.0))
        if per_item := spec.get("per_item"):
            total += len(self._field(directive, per_item["field"], [])) * float(per_item["seconds"])
        return total

    def _main_audio_seconds(self, directive: Directive) -> float | None:
        audio = self._raw_audio_duration(getattr(directive, "audio_url", None) or "")
        if audio is not None:
            return audio
        text = "".join(str(self._field(directive, field_name, "")) for field_name in self._char_fields(directive))
        if not text:
            return None
        return self._text_seconds(text)

    def _text_seconds(self, text: str) -> float:
        return len(text) / self.spec.fallback_cps

    def _char_fields(self, directive: Directive) -> list[str]:
        return list(self._directive_spec(directive).get("content", {}).get("chars", []))

    def _directive_spec(self, directive: Directive) -> dict[str, Any]:
        try:
            return self.spec.directives[directive.kind]
        except KeyError:
            raise PlanError(f"stage plan has no directive rule for {directive.kind!r}") from None

    def _cue_specs(self, directive: Directive) -> list[dict[str, Any]]:
        return list(self._directive_spec(directive).get("cues", []))

    def _expand_cue(self, directive: Directive, cue: dict[str, Any], duration: float) -> list[tuple[str, float]]:
        if (when := cue.get("when")) and not self._condition(directive, when):
            return []
        if each := cue.get("each"):
            items = self._field(directive, each["field"], [])
            return [
                (self._resolve_name(directive, cue), self._resolve_at(directive, cue["at"], duration, index=i))
                for i, _ in enumerate(items)
            ]
        return [(self._resolve_name(directive, cue), self._resolve_at(directive, cue.get("at", 0.0), duration))]

    def _condition(self, directive: Directive, spec: dict[str, Any]) -> bool:
        if "truthy" in spec:
            return bool(self._field(directive, spec["truthy"], None))
        return True

    def _resolve_name(self, directive: Directive, cue: dict[str, Any]) -> str:
        name = cue["name"]
        if isinstance(name, str):
            return name
        if "bool_field" in name:
            true_name = name.get("true", name.get(True))
            false_name = name.get("false", name.get(False))
            return true_name if bool(self._field(directive, name["bool_field"], False)) else false_name
        if "field" in name:
            return name["cases"][str(self._field(directive, name["field"], ""))]
        if "none_field" in name:
            return name["none"] if self._field(directive, name["none_field"], None) is None else name["present"]
        if "empty_field" in name:
            return name["empty"] if not self._field(directive, name["empty_field"], []) else name["nonempty"]
        raise PlanError(f"unsupported cue name expression: {name!r}")

    def _resolve_at(self, directive: Directive, spec: Any, duration: float, *, index: int = 0) -> float:
        if isinstance(spec, int | float):
            return float(spec)
        if "fraction" in spec:
            return duration * float(spec["fraction"])
        if "stagger_index" in spec:
            cfg = spec["stagger_index"]
            n = len(self._field(directive, cfg["field"], []))
            return index * duration * float(cfg["fraction"]) / max(n, 1)
        if "stagger_end" in spec:
            cfg = spec["stagger_end"]
            n = len(self._field(directive, cfg["field"], []))
            return n * duration * float(cfg["fraction"]) / max(n, 1)
        raise PlanError(f"unsupported cue at expression: {spec!r}")

    def _first_text(self, directive: Directive) -> str:
        for field_name in ("text", "speech"):
            value = getattr(directive, field_name, None)
            if isinstance(value, str):
                return value
        return ""

    def _field(self, obj: Any, name: str, default: Any) -> Any:
        return getattr(obj, name, default)
