"""Common immutable record configuration."""

from pydantic import BaseModel, ConfigDict


class FrozenRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
