"""M1 Product Intelligence (§4 M1): candidate collection within the Coupang call budget, POS scoring,
hard exclusion of regulated goods, and ``product.selected`` events for M3/M4."""

from acvm.modules.m1_product.pos import MODEL_VERSION, PosModel, PosWeights, RawSignals
from acvm.modules.m1_product.service import ProductIntelligence, ScoredProduct
from acvm.modules.m1_product.vdemo import ScoutVDemo, VDemoAssessment, VDemoResult, heuristic_vdemo

__all__ = [
    "MODEL_VERSION",
    "PosModel",
    "PosWeights",
    "ProductIntelligence",
    "RawSignals",
    "ScoredProduct",
    "ScoutVDemo",
    "VDemoAssessment",
    "VDemoResult",
    "heuristic_vdemo",
]
