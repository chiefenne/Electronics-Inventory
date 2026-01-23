# models.py
from __future__ import annotations

from typing import Optional, List
from pydantic import BaseModel, Field


class PartCreate(BaseModel):
    category: str = Field(min_length=1, max_length=24)
    subcategory: Optional[str] = Field(default="", max_length=64)
    description: str = Field(min_length=1, max_length=200)
    package: Optional[str] = Field(default="", max_length=32)
    container_id: Optional[str] = Field(default="", max_length=32)
    quantity: int = Field(default=0, ge=0, le=1_000_000)
    notes: Optional[str] = Field(default="", max_length=1000)


class PartUpdateCell(BaseModel):
    field: str
    value: str


# --- AI Auto-Fill models (optional feature) ---
class PartRequest(BaseModel):
    query: str


class PartData(BaseModel):
    description: str
    package: str
    category: str
    subcategory: str
    notes: str
    datasheet_url: Optional[str] = None


class ImageResult(BaseModel):
    title: str
    thumbnail: str
    url: str
    source: str


class ImageDownloadRequest(BaseModel):
    url: str
    type: str = "part"  # "part" or "pinout"
    part_description: str  # Used to generate filename
