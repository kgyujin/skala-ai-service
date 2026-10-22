"""카드 행동의 입력 경계. 모델이나 브라우저가 효과값을 직접 지정할 수 없습니다."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class CardAction(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    action_id: str = Field(pattern=r'^[a-zA-Z0-9_-]{8,96}$')
    expected_version: int = Field(ge=0)
    kind: Literal['select_deal', 'prepare_trade', 'play_card', 'cancel_preparation', 'negotiate', 'accept', 'end_round', 'choose_reward']
    deal_id: str | None = Field(default=None, pattern=r'^[ab]:(buy|sell):(fuel|food|medicine|parts):[1-4]$')
    merchant: Literal['a', 'b'] | None = None
    direction: Literal['buy', 'sell'] | None = None
    good: Literal['fuel', 'food', 'medicine', 'parts'] | None = None
    quantity: int | None = Field(default=None, ge=1, le=4)
    unit_price: int | None = Field(default=None, ge=1, le=100)
    card_id: str | None = Field(default=None, max_length=64)
    discard_ids: list[str] = Field(default_factory=list, max_length=2)
    offer_id: str | None = Field(default=None, max_length=64)
    message: str = Field(default='', max_length=200)
    reward: Literal['bulk', 'breakthrough', 'swift', 'extra', 'history', 'analysis', 'standard'] | None = None

    @model_validator(mode='after')
    def action_fields(self):
        allowed = {'select_deal': {'deal_id'}, 'prepare_trade': {'merchant', 'direction', 'good', 'quantity', 'unit_price'},
                   'play_card': {'card_id', 'discard_ids'}, 'cancel_preparation': set(),
                   'negotiate': {'message'}, 'accept': {'offer_id'}, 'end_round': set(),
                   'choose_reward': {'reward'}}[self.kind]
        required = {'select_deal': {'deal_id'}, 'prepare_trade': {'merchant', 'direction', 'good', 'quantity'},
                    'play_card': {'card_id'}, 'accept': {'offer_id'}}.get(self.kind, set())
        payload = self.model_dump(exclude={'action_id', 'expected_version', 'kind'})
        if any(payload[key] is None for key in required):
            raise ValueError('행동에 필요한 입력이 없습니다.')
        if any(payload[key] not in (None, '', []) for key in payload.keys() - allowed):
            raise ValueError('이 행동에 사용할 수 없는 입력입니다.')
        return self
