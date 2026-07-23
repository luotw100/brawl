"""Sentence Runtime 数据边界必须拒绝 Pydantic 的宽松转换。"""

import pytest
from pydantic import ValidationError

from brawl.runtime.types import Delivery, Expectation, Sentence


@pytest.mark.parametrize(
    "channels",
    [[], [True], ["1"], [0], [-1], [1, 1], ["broadcast", "broadcast"]],
)
def test_delivery_rejects_empty_coerced_or_duplicate_channels(channels) -> None:
    with pytest.raises(ValidationError):
        Delivery(text="x", channels=channels)


def test_delivery_rejects_coerced_bool_and_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        Delivery(text="x", channels=[1], spoken="false")
    with pytest.raises(ValidationError):
        Delivery(text="x", channels=[1], phase="vote")


@pytest.mark.parametrize("seats", [[], [True], ["1"], [0], [-1], [1, 1]])
def test_expectation_rejects_empty_coerced_or_duplicate_seats(seats) -> None:
    with pytest.raises(ValidationError):
        Expectation(seats=seats, turn_id="turn")


def test_sentence_rejects_invalid_speaker_and_empty_turn() -> None:
    with pytest.raises(ValidationError):
        Sentence(seq=-1, turn_id="turn", speaker=True)
    with pytest.raises(ValidationError):
        Sentence(seq=-1, turn_id="", speaker=1)
