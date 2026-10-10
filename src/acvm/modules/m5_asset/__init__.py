"""M5 Asset Engine (§4 M5): visual sourcing by priority and the rights ledger."""

from acvm.modules.m5_asset.download import fetch_product_image, host_allowed
from acvm.modules.m5_asset.service import COUPANG_IMAGE_LICENSE, AssetService

__all__ = ["COUPANG_IMAGE_LICENSE", "AssetService", "fetch_product_image", "host_allowed"]
