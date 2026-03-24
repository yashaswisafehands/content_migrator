"""
Data models for migration payloads.
Defines structured data classes for API requests.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


@dataclass
class LanguageData:
    """Data structure for language creation payload."""

    language_name: str
    autonym_script: str
    country: str
    country_code: str
    region: str
    latitude: float
    longitude: float
    learning_platform: Optional[bool] = True
    created_by: Optional[str] = "System"
    icon: Optional[str] = None
    image_prefix: Optional[str] = None
    video_prefix: Optional[str] = None
    categories: Optional[list[str]] = field(
        default_factory=list
    )  # List of category IDs

    def to_dict(self) -> Dict[str, Any]:
        return {
            "language_name": self.language_name,
            "autonym_script": self.autonym_script,
            "learning_platform": self.learning_platform,
            "country_code": self.country_code,
            "country": self.country,
            "region": self.region,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "created_by": self.created_by,
            "icon": self.icon,
            "categories": self.categories,
        }


@dataclass
class ModuleData:
    """Data structure for module creation payload."""

    title: str
    description: Optional[str] = None
    icon: Optional[str] = None
    created_by: Optional[str] = "System"

    videos: List[str] = field(default_factory=list)
    action_cards: List[str] = field(default_factory=list)
    practical_procedures: List[str] = field(default_factory=list)
    key_learning_points: List[str] = field(default_factory=list)
    drugs: List[str] = field(default_factory=list)
    language_id: Optional[str] = None
    region: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "description": self.description,
            "icon": self.icon,
            "created_by": self.created_by,
            "videos": self.videos,
            "action_cards": self.action_cards,
            "practical_procedures": self.practical_procedures,
            "key_learning_points": self.key_learning_points,
            "drugs": self.drugs,
            "language_id": self.language_id,
            "region": self.region,
        }


class ResourcePostRequestData(BaseModel):
    """Data structure for resource creation/update payload."""

    title: str
    description: Optional[str] = None
    icon: Optional[str] = None
    content: Optional[str] = None
    language_id: Optional[str] = None
    region: Optional[str] = None
    content_type: Optional[str] = None
    created_by: Optional[str] = "System"
    level: Optional[str] = None
    questions: Optional[List[Dict[str, Any]]] = None
    derived_from_id: Optional[str] = None
