"""Deterministic Coupang Partners sandbox (``settings.coupang.sandbox = True``, the default until G0).

``SandboxCoupangClient`` subclasses the real client and replaces only ``_transport``: requests still go
through the identical budget → cache → 403-breaker pipeline (§4 M1, §9.5 #2), and responses are built
in the official envelope/field shapes from a fixed catalogue, so M1/M3 exercise production code paths.

Sandbox-only enrichments (the real API has none of these): ``rating``/``reviewCount`` for richer POS
inputs, and ``soldOut``/``discontinued`` flags that drive the M3 health checker. Discontinued items are
delisted from search/best results exactly as on Coupang; sold-out items stay listed.

Deeplinks are pure functions of (url, subId, partner tag):
``https://link.coupang.com/a/<code>`` and
``https://link.coupang.com/re/AFFSDP?lptag=<partner_id|AF0000000>&subid=<subId>&pageKey=<productId>``.
Reports are pure functions of (kind, subId, date).
"""

from __future__ import annotations

import hashlib
import random
import re
import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import parse_qsl, urlparse

from acvm.config import Settings
from acvm.core.clock import Clock, to_kst
from acvm.core.enums import LinkHealth
from acvm.core.ids import short_code
from acvm.infra.coupang import (
    API_PREFIX,
    BEST_MAX_LIMIT,
    COUPANG_HOSTS,
    REPORT_PAGE_SIZE,
    SEARCH_MAX_LIMIT,
    CoupangClient,
    CoupangPartnersClient,
    KvProvider,
    RawResponse,
)
from acvm.infra.coupang_catalog import CATEGORY_NAMES, resolve_category
from acvm.ports import QuotaStore, SecretStore

if TYPE_CHECKING:
    from acvm.context import AppContext

SANDBOX_PARTNER_ID: Final = "AF0000000"


@dataclass(frozen=True, slots=True)
class SandboxItem:
    product_id: int
    name: str
    category_id: str
    price: int
    rating: float
    review_count: int
    is_rocket: bool = True
    is_free_shipping: bool = True
    sold_out: bool = False
    discontinued: bool = False


def _i(pid: int, name: str, cat: str, price: int, rating: float, reviews: int, rocket: bool = True,
       free: bool = True, *, sold_out: bool = False, discontinued: bool = False) -> SandboxItem:
    return SandboxItem(pid, name, cat, price, rating, reviews, rocket, free, sold_out, discontinued)


# Product IDs encode the category for readability: 7 · <category> · <sequence>.
CATALOG: Final[tuple[SandboxItem, ...]] = (
    # ── 1013 주방용품 ──
    _i(7101300001, "과자 밀봉 클립 20개입 대용량 세트", "1013", 8_900, 4.7, 3_812),
    _i(7101300002, "진공 밀폐용기 6종 세트 투명 김치통", "1013", 29_900, 4.6, 2_140),
    _i(7101300003, "실리콘 주방 집게 2P 내열 조리도구", "1013", 9_800, 4.5, 1_650, False, True),
    _i(7101300004, "스테인리스 채칼 슬라이서 5in1 야채 다지기", "1013", 15_900, 4.4, 980),
    _i(7101300005, "접이식 실리콘 설거지통 대형", "1013", 18_900, 4.6, 760),
    _i(7101300006, "자석 주방 칼꽂이 벽걸이 마그네틱 랙", "1013", 21_900, 4.5, 540, False, False),
    _i(7101300007, "싱크대 배수구 거름망 스테인리스 2개", "1013", 7_900, 4.4, 4_210),
    _i(7101300008, "원터치 음식물 쓰레기통 밀폐 5L", "1013", 24_900, 4.3, 1_320),
    _i(7101300009, "냉장고 정리 트레이 슬라이딩 수납 4개", "1013", 19_800, 4.6, 2_875),
    _i(7101300010, "전동 우유 거품기 휴대용 핸드블렌더", "1013", 12_900, 4.2, 640, False, True),
    _i(7101300011, "무선 전기 주전자 1.7L 스테인리스", "1013", 39_900, 4.5, 5_120),
    _i(7101300012, "코팅 프라이팬 28cm 인덕션 겸용", "1013", 34_900, 4.6, 8_930),
    _i(7101300013, "과자밀봉 클립 20개입 대용량세트", "1013", 9_500, 4.5, 1_210, False, True),  # re-listing
    _i(7101300014, "와인잔 크리스탈 4P 세트", "1013", 27_900, 4.5, 430),
    _i(7101300015, "다용도 계량컵 스푼 세트 8종", "1013", 6_900, 4.4, 1_980, sold_out=True),
    _i(7101300016, "수동 커피 그라인더 세라믹 날", "1013", 22_900, 4.1, 310, discontinued=True),
    _i(7101300017, "대용량 프리미엄 주방 가전 오븐 60L", "1013", 389_000, 4.3, 220, False, True),
    # ── 1018 자동차용품 ──
    _i(7101800001, "차량용 송풍구 스마트폰 거치대 자석형", "1018", 14_900, 4.5, 6_230),
    _i(7101800002, "차량용 틈새 수납함 콘솔 사이드 포켓 2P", "1018", 12_800, 4.4, 3_410),
    _i(7101800003, "차량용 무선 충전 거치대 15W 고속", "1018", 32_900, 4.3, 2_780),
    _i(7101800004, "트렁크 정리함 접이식 대용량", "1018", 19_900, 4.6, 4_120),
    _i(7101800005, "차량용 햇빛가리개 앞유리 우산형", "1018", 16_900, 4.4, 5_340),
    _i(7101800006, "차량용 미니 청소기 무선 핸디", "1018", 39_900, 4.2, 1_980),
    _i(7101800007, "헤드레스트 후크 걸이 4P", "1018", 6_900, 4.5, 7_810, False, True),
    _i(7101800008, "차량용 컵홀더 확장 거치대", "1018", 13_900, 4.3, 1_240),
    _i(7101800009, "유리 발수코팅제 스프레이 500ml", "1018", 11_900, 4.4, 2_650),
    _i(7101800010, "차량용 방향제 송풍구 클립형", "1018", 9_900, 4.1, 3_420, False, True),
    _i(7101800011, "블랙박스 메모리카드 64GB", "1018", 18_900, 4.6, 8_920),
    _i(7101800012, "타이어 공기압 측정기 디지털", "1018", 15_800, 4.5, 1_130),
    _i(7101800013, "차량용 시트 틈새 쿠션 메모리폼", "1018", 17_900, 4.3, 870, sold_out=True),
    _i(7101800014, "차량용 공기청정기 USB 미니", "1018", 29_900, 3.9, 410, discontinued=True),
    # ── 1014 생활용품 (정리수납) ──
    _i(7101400001, "압축 리빙박스 대형 3개 세트", "1014", 25_900, 4.6, 5_430),
    _i(7101400002, "옷걸이 정리 슬림 논슬립 50개", "1014", 17_900, 4.7, 9_120),
    _i(7101400003, "서랍 칸막이 정리함 조절식 8P", "1014", 13_900, 4.5, 2_310),
    _i(7101400004, "이불 압축팩 진공 10매", "1014", 15_900, 4.4, 3_870),
    _i(7101400005, "틈새 수납장 슬림 4단 바퀴형", "1014", 32_900, 4.5, 1_780),
    _i(7101400006, "매직 행거 옷걸이 연결고리 20개", "1014", 8_900, 4.6, 6_450, False, True),
    _i(7101400007, "문걸이 후크 수납 걸이 5구", "1014", 7_900, 4.3, 2_040, False, True),
    _i(7101400008, "무타공 벽부착 욕실 코너 선반", "1014", 12_900, 4.4, 3_360),
    _i(7101400009, "신발 정리대 2단 슬라이드 10P", "1014", 19_900, 4.5, 2_780),
    _i(7101400010, "멀티탭 정리함 전선 케이블 박스", "1014", 11_900, 4.6, 4_150),
    _i(7101400011, "투명 아크릴 화장품 정리함 회전형", "1014", 21_900, 4.5, 1_650),
    _i(7101400012, "다용도 바구니 수납함 접이식 3P", "1014", 14_900, 4.4, 980),
    _i(7101400013, "KF94 보건용 마스크 대형 50매", "1014", 14_900, 4.7, 12_400),  # 의약외품 → regulated
    _i(7101400014, "욕실 수건 선반 스테인리스 2단", "1014", 23_900, 4.2, 620, sold_out=True),
    _i(7101400015, "빨래 건조대 접이식 대형 윙형", "1014", 34_900, 4.3, 1_440, discontinued=True),
    # ── 1024 헬스/건강식품 (regulated category: never selectable) ──
    _i(7102400001, "종합비타민 미네랄 120정 4개월분", "1024", 24_900, 4.6, 15_200),
    _i(7102400002, "프로바이오틱스 유산균 60캡슐", "1024", 19_900, 4.5, 8_430),
    _i(7102400003, "rTG 오메가3 90캡슐", "1024", 29_900, 4.6, 6_120),
    _i(7102400004, "다이어트 보조제 가르시니아 60정", "1024", 32_900, 4.0, 2_210),
)


def _norm(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", unicodedata.normalize("NFKC", text).lower())


def _seed(*parts: object) -> random.Random:
    digest = hashlib.blake2b("|".join(map(str, parts)).encode(), digest_size=8).digest()
    return random.Random(int.from_bytes(digest, "big"))


def _envelope(data: Any, r_code: str = "0", message: str = "") -> RawResponse:
    return RawResponse(200, {"rCode": r_code, "rMessage": message, "data": data})


_PRODUCT_PATH = re.compile(r"/vp/products/(\d+)")


class SandboxCoupangClient(CoupangPartnersClient):
    """Fixture-backed client with the real client's interface and operating policy."""

    def __init__(self, settings: Settings, secrets: SecretStore, quota: QuotaStore, clock: Clock,
                 kv_provider: KvProvider, *, catalog: Sequence[SandboxItem] = CATALOG) -> None:
        super().__init__(settings, secrets, quota, clock, kv_provider)
        self._items: dict[int, SandboxItem] = {it.product_id: it for it in catalog}
        self._overrides: dict[int, LinkHealth] = {}
        self._issued_sub_ids: set[str] = set()
        self.transport_calls: Counter[str] = Counter()  # requests that reached the (fake) wire, per endpoint

    @property
    def partner_tag(self) -> str:
        return self.cfg.partner_id or SANDBOX_PARTNER_ID

    def items(self, category_id: str | None = None) -> list[SandboxItem]:
        return [it for it in self._items.values() if category_id is None or it.category_id == category_id]

    def set_availability(self, product_id: int, health: LinkHealth) -> None:
        """Operator drill / test hook: force a product's stock state for the health checker."""
        self._overrides[int(product_id)] = health

    async def availability(self, product_id: int, name: str) -> LinkHealth:
        """Stock state straight from the fixture (no search call: the sandbox has a stock signal the
        real API lacks, so it does not need to spend search budget to approximate one)."""
        pid = int(product_id)
        if pid in self._overrides:
            return self._overrides[pid]
        item = self._items.get(pid)
        if item is None:
            return LinkHealth.UNKNOWN
        if item.discontinued:
            return LinkHealth.DISCONTINUED
        return LinkHealth.SOLD_OUT if item.sold_out else LinkHealth.OK

    # ── fake wire ──

    async def _transport(self, method: str, path: str, query: str, body: dict[str, Any] | None) -> RawResponse:
        params = dict(parse_qsl(query, keep_blank_values=True))
        route = path.removeprefix(API_PREFIX)
        if route == "/products/search":
            self.transport_calls["search"] += 1
            return self._search(params)
        if route.startswith("/products/bestcategories/"):
            self.transport_calls["bestcategories"] += 1
            return self._best(route.rsplit("/", 1)[-1], params)
        if route == "/products/goldbox":
            self.transport_calls["goldbox"] += 1
            return self._goldbox(params)
        if route == "/deeplink" and method == "POST":
            self.transport_calls["deeplink"] += 1
            return self._deeplink(body or {})
        if route.startswith("/reports/"):
            self.transport_calls["reports"] += 1
            return self._reports(route.rsplit("/", 1)[-1], params)
        return RawResponse(404, {"rCode": "404", "rMessage": f"unknown sandbox route {route}"})

    def _listed(self, items: Sequence[SandboxItem]) -> list[SandboxItem]:
        return [it for it in items if not it.discontinued and
                self._overrides.get(it.product_id) is not LinkHealth.DISCONTINUED]

    def _item_json(self, it: SandboxItem, rank: int, keyword: str, sub_id: str) -> dict[str, Any]:
        tag = f"lptag={self.partner_tag}" + (f"&subid={sub_id}" if sub_id else "")
        sold_out = it.sold_out or self._overrides.get(it.product_id) is LinkHealth.SOLD_OUT
        return {
            "productId": it.product_id,
            "productName": it.name,
            "productPrice": it.price,
            # served by our own API (acvm.api.public /sandbox/product-image) so sandbox pages show real thumbnails
            "productImage": f"{self.settings.link_hub_base_url.rstrip('/')}/sandbox/product-image/{it.product_id}.jpg",
            "productUrl": f"https://link.coupang.com/re/AFFSDP?{tag}&pageKey={it.product_id}",
            "isRocket": it.is_rocket,
            "isFreeShipping": it.is_free_shipping,
            "categoryName": CATEGORY_NAMES.get(it.category_id, ""),
            "rank": rank,
            "keyword": keyword,
            # sandbox-only enrichments
            "rating": it.rating,
            "reviewCount": it.review_count,
            "soldOut": sold_out,
            "discontinued": False,
        }

    @staticmethod
    def _popularity(it: SandboxItem) -> tuple[float, int]:
        return (-(it.review_count * it.rating), it.product_id)

    def _search(self, params: dict[str, str]) -> RawResponse:
        keyword = params.get("keyword", "")
        limit = max(1, min(int(params.get("limit", "10") or 10), SEARCH_MAX_LIMIT))
        tokens = [_norm(tok) for tok in keyword.split() if len(_norm(tok)) >= 2]
        listed = self._listed(list(self._items.values()))
        scored: list[tuple[int, SandboxItem]] = []
        for it in listed:
            name = _norm(it.name)
            hits = sum(1 for tok in tokens if tok in name)
            if hits:
                scored.append((hits, it))
        if not scored:
            cat = resolve_category(keyword)
            scored = [(1, it) for it in listed if cat is not None and it.category_id == cat]
        scored.sort(key=lambda x: (-x[0], *self._popularity(x[1])))
        sub_id = params.get("subId", "")
        data = [self._item_json(it, i + 1, keyword, sub_id) for i, (_, it) in enumerate(scored[:limit])]
        landing = f"https://link.coupang.com/re/AFFSEARCH?lptag={self.partner_tag}&keyword={keyword}"
        return _envelope({"landingUrl": landing, "productData": data})

    def _best(self, category_id: str, params: dict[str, str]) -> RawResponse:
        if category_id not in CATEGORY_NAMES:
            return _envelope(None, "400", f"invalid categoryId {category_id}")
        limit = max(1, min(int(params.get("limit", "20") or 20), BEST_MAX_LIMIT))
        items = sorted(self._listed(self.items(category_id)), key=self._popularity)[:limit]
        sub_id = params.get("subId", "")
        return _envelope([self._item_json(it, i + 1, "", sub_id) for i, it in enumerate(items)])

    def _goldbox(self, params: dict[str, str]) -> RawResponse:
        day = to_kst(self._clock.now()).date().isoformat()  # deals rotate daily, deterministically
        picks = [it for it in self._listed(list(self._items.values()))
                 if _seed("goldbox", day, it.product_id).random() < 0.25]
        picks.sort(key=self._popularity)
        sub_id = params.get("subId", "")
        return _envelope([self._item_json(it, i + 1, "", sub_id) for i, it in enumerate(picks[:20])])

    def _deeplink(self, body: dict[str, Any]) -> RawResponse:
        sub_id = str(body.get("subId", ""))
        urls = body.get("coupangUrls") or []
        out: list[dict[str, str]] = []
        for url in urls:
            u = urlparse(str(url))
            m = _PRODUCT_PATH.search(u.path)
            page_key = m.group(1) if m else dict(parse_qsl(u.query)).get("pageKey", "")
            if u.hostname not in COUPANG_HOSTS or not page_key:
                return _envelope(None, "400", f"unsupported coupangUrl {url}")
            code = short_code(f"{url}|{sub_id}|{self.partner_tag}", length=10)
            out.append({
                "originalUrl": str(url),
                "shortenUrl": f"https://link.coupang.com/a/{code}",
                "landingUrl": (f"https://link.coupang.com/re/AFFSDP?lptag={self.partner_tag}"
                               f"&subid={sub_id}&pageKey={page_key}"),
            })
        if sub_id:
            self._issued_sub_ids.add(sub_id)
        return _envelope(out)

    def _reports(self, kind: str, params: dict[str, str]) -> RawResponse:
        try:
            start = datetime.strptime(params["startDate"], "%Y%m%d").date()
            end = datetime.strptime(params["endDate"], "%Y%m%d").date()
        except (KeyError, ValueError):
            return _envelope(None, "400", "startDate/endDate must be yyyyMMdd")
        sub_ids = [params["subId"]] if params.get("subId") else sorted(self._issued_sub_ids)
        rows: list[dict[str, Any]] = []
        day = start
        while day <= end:
            for sid in sub_ids:
                rows.extend(self._report_rows(kind, sid, day))
            day += timedelta(days=1)
        page = int(params.get("page", "0") or 0)
        return _envelope(rows[page * REPORT_PAGE_SIZE : (page + 1) * REPORT_PAGE_SIZE])

    def _report_rows(self, kind: str, sub_id: str, day: date) -> list[dict[str, Any]]:
        """Deterministic funnel per (subId, day): clicks → orders (≈3 % CVR) → ≈10 % cancelled."""
        ds = day.strftime("%Y%m%d")
        clicks = _seed("clicks", sub_id, ds).randint(0, 40)
        rng = _seed("orders", sub_id, ds)
        orders = [(rng.randint(9_900, 59_900), rng.random() < 0.10) for _ in range(clicks) if rng.random() < 0.03]
        base = {"date": ds, "trackingCode": self.partner_tag, "subId": sub_id}
        if kind == "clicks":
            return [{**base, "addtag": "400", "ctag": "sandbox", "click": clicks}] if clicks else []
        if kind == "orders":
            return [{**base, "orderId": f"{ds}{sub_id[-4:]}{i:02d}", "quantity": 1, "gmv": gmv,
                     "commissionRate": 3.0, "commission": round(gmv * 0.03)} for i, (gmv, _) in enumerate(orders)]
        if kind == "cancels":
            return [{**base, "orderDate": ds, "orderId": f"{ds}{sub_id[-4:]}{i:02d}", "quantity": 1, "gmv": gmv,
                     "commissionRate": 3.0, "commission": round(gmv * 0.03)}
                    for i, (gmv, cancelled) in enumerate(orders) if cancelled]
        commission = sum(round(g * 0.03) for g, c in orders if not c)
        return [{**base, "commission": commission, "click": clicks, "order": len(orders),
                 "cancel": sum(1 for _, c in orders if c), "gmv": sum(g for g, _ in orders)}] if clicks else []


def build_coupang_client(ctx: AppContext) -> CoupangPartnersClient:
    """Sandbox or real client per ``settings.coupang.sandbox`` (prod refuses sandbox at boot)."""
    cls = SandboxCoupangClient if ctx.settings.coupang.sandbox else CoupangPartnersClient
    return cls(ctx.settings, ctx.secrets, ctx.quota, ctx.clock, ctx.uow)


def coupang_client_for(ctx: AppContext) -> CoupangClient:
    """The process-wide client in ``ctx.extras["coupang"]``, built on first use."""
    client: CoupangClient | None = ctx.extras.get("coupang")
    if client is None:
        client = build_coupang_client(ctx)
        ctx.extras["coupang"] = client
    return client
