"""Legacy protocol 4-7 language heuristics.

These checks are intentionally quarantined from the live player protocol.
They only preserve the exact action shape and validation semantics of already
recorded replays.  Natural-language meaning is not a deterministic game rule,
so protocol 8 and later must not call this module.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from brawl.games.avalon.state import Role, Side
from brawl.runtime.parsing import JsonParseError

_SUCCESS_EVIDENCE = re.compile(
    r"(?:任务(?:成功|通过)|成功(?:任务|队伍|车|核心)|"
    r"(?:零|0|没|无|没有)\s*(?:张)?(?:破坏|坏票)|"
    r"(?:破坏|坏票)\s*(?:为|是)?\s*(?:零|0))"
)
_ABSOLUTE_GOOD_IDENTITY = re.compile(
    r"(?:全(?:员|队)?(?:是|为)?蓝|都是蓝|铁蓝|(?:身份)?(?:干净|清白)|"
    r"确定(?:是|为)蓝|排除(?:了)?红方|不可能是红)"
)
_IDENTITY_LINK = re.compile(r"(?:证明|验证|说明|代表|等于|视为|当(?:作)?)")
_NEGATION_NEAR_LINK = re.compile(r"(?:不|并不|不能|无法|不足以|未能|没能|不可|不应|别)")
_IDENTITY_CLAIM = re.compile(
    r"(?:自称(?:自己)?(?:是|为)?|声称自己(?:是|为)?|"
    r"表态自己(?:是|为)?|说(?:自己|我)(?:是|为)?)"
    r".{0,4}"
    r"(?:蓝方|好人|忠臣|梅林|派西维尔)"
)
_IDENTITY_SUPPORT_LINK = re.compile(
    r"(?:所以|因此|既然|基于|考虑到|没有理由.{0,8}(?:质疑|反对))"
    r"[\s\S]{0,30}(?:信任|相信|赞成|通过|可靠|安全)"
)
_CLAIM_DISCOUNT = re.compile(
    r"(?:零证据|没有信息量|不能作为|不足以|不构成|并不代表|不能证明|"
    r"不(?:信任|相信|采信|认可)|不能.{0,8}(?:信|当作|视为)|"
    r"只是.{0,8}(?:声明|说法)|可能.{0,8}(?:撒谎|说谎|假报)|待验证|保留)"
)
_LADY_MARKER = re.compile(r"(?:湖中仙女|仙女|查验|验人)")
_LADY_COLOR = re.compile(r"(?:蓝方|红方|好人|坏人)")
_LADY_TRUST = re.compile(
    r"(?:我(?:选择)?(?:相信|信任|采信|认可).{0,10}(?:结果|说法|结论|信息|他|她)|"
    r"(?:这个|该)(?:结果|查验|说法).{0,8}(?:可信|可靠)|"
    r"查实|实锤|铁蓝|铁红|"
    r"(?:因此|所以).{0,20}(?:赞成|通过|上车|反对|排除)|"
    r"(?:相对|更)(?:稳妥|安全|可靠))"
)
_PASS_SPEECH = re.compile(r"^(?:过|没有补充|暂无补充|无补充)[。.!！]?$")
_LEGACY_STRONG_APPROVE = re.compile(
    r"(?:我(?:会|将)?投(?:赞成|同意)(?:票)?|"
    r"我(?:明确)?(?:赞成|同意)(?:这|该|本)?(?:支)?(?:队伍|车|提案)?|"
    r"我不反对|"
    r"^(?:\s*(?:10|[1-9])号[:：]\s*)?(?:明确)?(?:赞成|同意)"
    r"(?=[。！!，,；;]|$))"
)
_LEGACY_STRONG_REJECT = re.compile(
    r"(?:我(?:会|将)?投反对(?:票)?|"
    r"我(?:明确)?反对(?:这|该|本)?(?:支)?(?:队伍|车|提案)?|"
    r"我不赞成|"
    r"^(?:\s*(?:10|[1-9])号[:：]\s*)?(?:明确)?反对"
    r"(?=[。！!，,；;]|$))"
)
_STRONG_APPROVE = re.compile(
    r"(?:我(?:会|将)?投(?:赞成|同意)(?:票)?|"
    r"我(?:明确)?(?:赞成|同意)"
    r"(?:(?:这|该|本)(?:支)?(?:队伍|车|提案)|通过)?"
    r"(?=[。！!，,；;]|$)|"
    r"我不反对(?:(?:这|该|本)(?:支)?(?:队伍|车|提案))?"
    r"(?=[。！!，,；;]|$)|"
    r"^(?:赞成|同意)(?:通过)?(?=[。！!，,；;]|$))"
)
_STRONG_REJECT = re.compile(
    r"(?:我(?:会|将)?投反对(?:票)?|"
    r"我(?:明确)?反对"
    r"(?:(?:这|该|本)(?:支)?(?:队伍|车|提案))?"
    r"(?=[。！!，,；;]|$)|"
    r"我不赞成(?:(?:这|该|本)(?:支)?(?:队伍|车|提案))?"
    r"(?=[。！!，,；;]|$)|"
    r"^反对(?=[。！!，,；;]|$))"
)
_REPEATED_REASON_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "首轮缺少证据，先过或否决观察",
        re.compile(
            r"(?:先|尽快).{0,8}(?:走|过|开始|做).{0,8}(?:一轮|任务)"
            r"|任务结果.{0,16}(?:信息|判断)"
            r"|(?:第一轮|首轮|开局)[\s\S]{0,36}"
            r"(?:信息.{0,8}(?:少|不足|有限)|"
            r"(?:没有|缺乏|不足).{0,8}(?:信任|可信)|"
            r"(?:先|再|多).{0,8}(?:看看|观察)|"
            r"尽快.{0,12}(?:获取|获得).{0,8}(?:信息|情报)|"
            r"(?:即使|就算).{0,8}破坏.{0,12}(?:缩小|排除).{0,8}范围)"
            r"|尽快.{0,12}(?:出|做|开始)?任务.{0,16}"
            r"(?:获取|获得|提供).{0,8}(?:信息|情报)"
        ),
    ),
    (
        "在队所以支持自己的队伍",
        re.compile(
            r"(?:我(?:本人)?在(?:这|该)?(?:支)?(?:队伍|车)(?:里|上)?|"
            r"我是(?:这|该)?(?:支)?(?:队伍|车)(?:的)?成员|队伍里有我)"
            r"[\s\S]{0,36}(?:支持|成功牌|赞成|通过)"
        ),
    ),
    (
        "仅按座位前后或连续号判断队伍缺乏测试",
        re.compile(
            r"(?:位置靠前|前排|后排|连续号|前三个号|座位号)"
            r"[\s\S]{0,36}(?:组合|阵容|多样性|测试|检验|参与)"
        ),
    ),
)


def _speech_fingerprint(text: str) -> str:
    without_intro = re.sub(
        r"^\s*(?:大家好[，,]?)?(?:我是)?"
        r"(?:10|[1-9]|十|[一二三四五六七八九])号[，,:：]?\s*",
        "",
        text,
    )
    return re.sub(r"[\W_]+", "", without_intro)


def _mentioned_seats(text: str) -> set[int]:
    mentioned = {
        int(match.group(1))
        for match in re.finditer(r"(?<!\d)(10|[1-9])(?!\d)", text)
    }
    chinese = {
        "一": 1,
        "二": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10,
    }
    mentioned.update(
        chinese[match.group(1)]
        for match in re.finditer(
            r"(?<![一二三四五六七八九十])"
            r"(十|[一二三四五六七八九])\s*号",
            text,
        )
    )
    return mentioned


def declared_stance(seat: int, speech: str, agent_protocol: int) -> bool | None:
    if agent_protocol < 7:
        approve_pattern = _LEGACY_STRONG_APPROVE
        reject_pattern = _LEGACY_STRONG_REJECT
        normalized = speech
    else:
        approve_pattern = _STRONG_APPROVE
        reject_pattern = _STRONG_REJECT
        normalized = re.sub(
            rf"^\s*{seat}号[:：]?\s*",
            "",
            speech,
            count=1,
        )
    declarations = [
        *((match.start(), True) for match in approve_pattern.finditer(normalized)),
        *((match.start(), False) for match in reject_pattern.finditer(normalized)),
    ]
    if not declarations:
        return None
    return max(declarations, key=lambda item: item[0])[1]


def _success_claims_absolute_identity(speech: str) -> bool:
    success = list(_SUCCESS_EVIDENCE.finditer(speech))
    identities = list(_ABSOLUTE_GOOD_IDENTITY.finditer(speech))
    for evidence in success:
        for identity in identities:
            if abs(evidence.start() - identity.start()) > 90:
                continue
            start = min(evidence.start(), identity.start())
            end = max(evidence.end(), identity.end())
            segment = speech[start:end]
            links = list(_IDENTITY_LINK.finditer(segment))
            if not links:
                return True
            for link in links:
                prefix = segment[max(0, link.start() - 12):link.start()]
                if not _NEGATION_NEAR_LINK.search(prefix):
                    return True
    return False


def _identity_claim_used_as_support(speech: str) -> bool:
    for claim in _IDENTITY_CLAIM.finditer(speech):
        segment = speech[claim.start():claim.end() + 90]
        if (
            _IDENTITY_SUPPORT_LINK.search(segment)
            and not _CLAIM_DISCOUNT.search(segment)
        ):
            return True
    return False


def _lady_claim_target(speech: str) -> int | None:
    for marker in _LADY_MARKER.finditer(speech):
        window = speech[marker.start():marker.end() + 55]
        color = _LADY_COLOR.search(window)
        if color is None:
            continue
        candidates = [
            (match.start(), int(match.group(1)))
            for match in re.finditer(r"(?<!\d)(10|[1-9])\s*号(?!\d)", window)
        ]
        chinese = {
            "一": 1,
            "二": 2,
            "三": 3,
            "四": 4,
            "五": 5,
            "六": 6,
            "七": 7,
            "八": 8,
            "九": 9,
            "十": 10,
        }
        candidates.extend(
            (match.start(), chinese[match.group(1)])
            for match in re.finditer(
                r"(?<![一二三四五六七八九十])"
                r"(十|[一二三四五六七八九])\s*号",
                window,
            )
        )
        if candidates:
            return min(
                candidates,
                key=lambda item: abs(item[0] - color.start()),
            )[1]
    return None


def _lady_claim_used_as_fact(seat: int, speech: str, state: Any) -> bool:
    if not state.lady_checks:
        return False
    target = _lady_claim_target(speech)
    if target is None or not _LADY_TRUST.search(speech):
        return False
    if _CLAIM_DISCOUNT.search(speech):
        return False
    if target == seat or target in state.lady_checks.get(seat, {}):
        return False
    actor = state._seat(seat)
    return not (actor.role is Role.MERLIN and target in actor.intel)


def _validate_good_epistemics(seat: int, speech: str, state: Any) -> None:
    if state._seat(seat).role.side is Side.EVIL:
        return
    if _success_claims_absolute_identity(speech):
        raise JsonParseError(
            "任务成功或零破坏可以调整概率，但不能推出全蓝、铁蓝或排除红方；"
            "请保留这个不确定性"
        )
    if _identity_claim_used_as_support(speech):
        raise JsonParseError(
            "玩家可以零成本自称蓝方或特殊角色，身份自称本身不能作为信任、"
            "赞成或通过的依据；请改用可核对的组队、票型和任务记录"
        )
    if _lady_claim_used_as_fact(seat, speech, state):
        raise JsonParseError(
            "别人的湖中仙女公布只是可以撒谎的玩家声明，不能直接作为信任、"
            "上车或投票依据；请评估其行为动机或使用独立公开证据"
        )


def validate_proposal_speech(
    seat: int,
    speech: str,
    team: list[int],
    state: Any,
) -> None:
    missing = sorted(set(team) - _mentioned_seats(speech))
    if missing:
        raise JsonParseError(
            f"组队发言必须逐一点出实际提交的队伍 {team}；尚未提到：{missing}"
        )
    _validate_good_epistemics(seat, speech, state)


def validate_discussion_speech(seat: int, speech: str, state: Any) -> None:
    _validate_good_epistemics(seat, speech, state)

    current = _speech_fingerprint(speech)
    if _PASS_SPEECH.fullmatch(speech.strip()):
        return
    for label, pattern in _REPEATED_REASON_PATTERNS:
        if not pattern.search(speech):
            continue
        for other, previous in state.discussion_speeches.items():
            if pattern.search(previous):
                raise JsonParseError(
                    f"{other}号 已经说过“{label}”这个理由；"
                    "有新的决策依据就只说新增部分，否则只说“过。”"
                )

    if len(current) < 6:
        return
    for other, previous in state.discussion_speeches.items():
        if _PASS_SPEECH.fullmatch(previous.strip()):
            continue
        earlier = _speech_fingerprint(previous)
        if len(earlier) < 6:
            continue
        similarity = SequenceMatcher(None, earlier, current).ratio()
        threshold = 0.82 if min(len(earlier), len(current)) < 40 else 0.76
        if similarity >= threshold:
            raise JsonParseError(
                f"这段发言与 {other}号 本轮已说内容高度重复（{similarity:.0%}）；"
                "有新的决策依据就只说新增判断、未决风险或具体反驳，"
                "否则只说“过。”"
            )
