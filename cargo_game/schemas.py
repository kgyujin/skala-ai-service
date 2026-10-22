"""엄격한 API와 모델 출력 경계. 금액과 수량의 암묵적 변환을 허용하지 않습니다."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Proposal(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    buyer: Literal['player', 'a', 'b']
    seller: Literal['player', 'a', 'b']
    good: Literal['fuel', 'food', 'medicine', 'parts']
    quantity: int = Field(ge=1, le=12)
    unit_price: int = Field(ge=1, le=100)


class Decision(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    decision: Literal['offer', 'reject', 'clarify']
    speech: str = Field(min_length=1, max_length=180)
    offer: Proposal | None
    memory_trade_id: str | None

    @model_validator(mode='after')
    def matching_offer(self):
        if (self.decision == 'offer') != (self.offer is not None):
            raise ValueError('offer is required only for offer decisions')
        return self


class Action(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    action_id: str = Field(pattern=r'^[a-zA-Z0-9_-]{8,96}$')
    expected_version: int = Field(ge=0)
    kind: Literal['negotiate', 'accept', 'end_round']
    merchant: Literal['a', 'b'] | None = None
    message: str = Field(default='', max_length=1000)
    proposal: Proposal | None = None
    offer_id: str | None = Field(default=None, max_length=64)

    @model_validator(mode='after')
    def valid_fields(self):
        if self.kind == 'negotiate':
            if not self.merchant or not (self.message.strip() or self.proposal):
                raise ValueError('merchant and message or proposal required')
            if self.offer_id is not None:
                raise ValueError('offer_id is not a negotiation input')
        elif self.kind == 'accept':
            if not self.offer_id or self.merchant or self.proposal or self.message:
                raise ValueError('accept requires only offer_id')
        elif self.merchant or self.proposal or self.offer_id or self.message:
            raise ValueError('end_round has no negotiation fields')
        return self
