"""M2 레퍼런스 인텔리전스 (Content DNA), design §4 M2.

Own-channel tail content is the primary source (자기 채널 성과 데이터가 1순위); external references are
metadata + operator notes only (§8.2: no media, transcripts or subtitles of others are ever stored).
"""

from acvm.modules.m2_dna.service import (
    TAIL_PERCENTILE,
    ContentDNAService,
    external_dna_id,
    infer_hook_type,
    own_dna_id,
)

__all__ = ["TAIL_PERCENTILE", "ContentDNAService", "external_dna_id", "infer_hook_type", "own_dna_id"]
