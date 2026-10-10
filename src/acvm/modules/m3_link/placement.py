"""Platform link-placement policy (§4 M3 "플랫폼별 링크 배치 전략", §7 링크 클릭 column).

| Platform        | Link in post            | Placement                                                   |
|-----------------|-------------------------|-------------------------------------------------------------|
| Threads         | 본문 내 링크 (클릭 가능) | ``body`` (or ``first_comment``); disclosure in paragraph 1  |
| Instagram 릴스   | 캡션 링크 비활성         | ``hub`` — profile link-hub page + caption guidance           |
| YouTube Shorts  | 설명란 링크 클릭 제한    | ``hub`` — link hub + pinned guidance                         |
| Facebook 릴스    | 캡션 링크 가능           | ``caption_first_line`` — disclosure on the first line         |
| TikTok          | 캡션 링크 비활성         | ``hub`` — profile link hub                                    |
| Kakao           | no official publish API | ``hub`` — the manual one-click post points at the hub         |
"""

from __future__ import annotations

from typing import Final, Literal

from acvm.core.enums import Platform

LinkPlacement = Literal["body", "first_comment", "hub", "caption_first_line"]

PLACEMENT: Final[dict[Platform, LinkPlacement]] = {
    Platform.THREADS: "body",
    Platform.INSTAGRAM: "hub",
    Platform.YOUTUBE: "hub",
    Platform.FACEBOOK: "caption_first_line",
    Platform.TIKTOK: "hub",
    Platform.KAKAO: "hub",
}


def placement_for(platform: Platform, *, threads_first_comment: bool = False) -> LinkPlacement:
    """Where the affiliate link goes on ``platform``. Threads may opt into the first comment
    ("링크는 본문 또는 첫 댓글"); every other platform has exactly one compliant placement."""
    if platform is Platform.THREADS and threads_first_comment:
        return "first_comment"
    return PLACEMENT[platform]


def uses_hub(platform: Platform) -> bool:
    return PLACEMENT[platform] == "hub"
