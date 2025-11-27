from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from sqlalchemy import String, DateTime, Boolean, Float, Text
from sqlalchemy import ForeignKey, Integer, Index
from typing import Optional
from datetime import datetime, timezone
from sqlalchemy.orm import relationship
from uuid import uuid4

from .BaseModel import BaseModel


class UsageModel(BaseModel):
    __tablename__ = "usage"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default_factory=lambda: str(uuid4()))

    api_key_id: Mapped[str] = mapped_column(ForeignKey("api_keys.id", ondelete="CASCADE"), index=True, default=None, nullable=True)
    user_id: Mapped[Optional[str]] = mapped_column(String(36), default=None)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(timezone.utc), index=True)
    stream: Mapped[bool] = mapped_column(Boolean, default=False)

    route: Mapped[Optional[str]] = mapped_column(String(128), default=None)  # např. "openai_v1_responses"
    deployment: Mapped[Optional[str]] = mapped_column(String(128), default=None)
    status: Mapped[Optional[int]] = mapped_column(Integer, default=None)

    prompt_tokens: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    completion_tokens: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    total_tokens: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    stream_bytes: Mapped[Optional[int]] = mapped_column(Integer, default=None)

    # volitelná kalkulace ceny – pokud chceš
    cost_usd: Mapped[Optional[Float]] = mapped_column(Float, default=None)
    meta_json: Mapped[Optional[str]] = mapped_column(Text, default=None)

    api_key = relationship("ApiKeyModel", back_populates="usages")

Index("ix_usage_api_key_ts", UsageModel.api_key_id, UsageModel.ts)