"""Link-hub page rendering (§4 M3 링크 허브, §8.1 D-04).

"상품 카드 목록 페이지(자체 도메인)를 자동 생성. 페이지 최상단에 대가성 문구 고정." Rule D-04 BLOCKs a hub
whose top-fixed disclosure is missing, so the template guarantees by construction that the *first*
element inside ``<body>`` — with no text before it — is ``<div id="acvm-disclosure">`` carrying the
standard disclosure (§8.1 표준 대가성 문구), made sticky so it stays visible while scrolling.

Every card links to our ``/r/{hub_code}`` redirect (click logging + pause control) rather than straight
to Coupang, and shows the price with the date it was last checked ("YYYY-MM-DD 기준"). All product text
is untrusted marketplace data and is HTML-escaped (Jinja2 autoescape).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from jinja2 import Environment, StrictUndefined

HUB_TEMPLATE: Final = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex, nofollow">
<title>{{ handle }} · 추천 상품</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#f6f7f9;color:#111;line-height:1.45;
font-family:-apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Noto Sans KR","Malgun Gothic",sans-serif}
#acvm-disclosure{position:sticky;top:0;z-index:10;margin:0;padding:10px 14px;background:#fff8e1;
border-bottom:1px solid #f0d78c;font-size:13px;font-weight:600;color:#3d3000}
main{max-width:560px;margin:0 auto;padding:12px}
h1{font-size:18px;margin:8px 2px 12px}
.card{display:flex;gap:12px;background:#fff;border-radius:12px;padding:12px;margin:0 0 10px;
box-shadow:0 1px 2px rgba(0,0,0,.06)}
.card img{width:96px;height:96px;object-fit:cover;border-radius:8px;flex:none;background:#eee}
.info{flex:1;min-width:0}
.name{font-size:15px;font-weight:600;margin:0 0 6px;overflow:hidden;display:-webkit-box;
-webkit-line-clamp:2;-webkit-box-orient:vertical}
.price{font-size:16px;font-weight:700;margin:0}
.asof{font-size:12px;color:#666;margin:2px 0 8px}
.btn{display:block;text-align:center;background:#e52528;color:#fff;text-decoration:none;padding:9px 0;
border-radius:8px;font-weight:700}
.empty{color:#666;text-align:center;padding:32px 0}
footer{font-size:11px;color:#888;text-align:center;padding:8px 16px 24px}
</style>
</head>
<body><div id="acvm-disclosure" role="note">{{ disclosure }}</div>
<main>
<h1>{{ handle }} 추천 상품</h1>
{% for card in cards -%}
<article class="card">
<img src="{{ card.image_url }}" alt="{{ card.name }}" loading="lazy" width="96" height="96">
<div class="info">
<p class="name">{{ card.name }}</p>
<p class="price">{{ card.price_text }}</p>
<p class="asof">{{ card.as_of }} 기준 · 가격은 변동될 수 있습니다</p>
<a class="btn" href="{{ card.href }}" rel="sponsored nofollow noopener">쿠팡에서 보기</a>
</div>
</article>
{% else -%}
<p class="empty">지금 소개 중인 상품이 없습니다.</p>
{% endfor -%}
</main>
<footer>최종 가격·재고·배송 정보는 쿠팡 상품 페이지에서 확인하세요.</footer>
</body>
</html>
"""

_ENV: Final = Environment(autoescape=True, undefined=StrictUndefined, keep_trailing_newline=True)
_TEMPLATE: Final = _ENV.from_string(HUB_TEMPLATE)


@dataclass(frozen=True, slots=True)
class HubCard:
    name: str
    image_url: str
    price_text: str  # "12,900원"
    as_of: str  # "YYYY-MM-DD" (KST) of the last price check
    href: str  # {link_hub_base_url}/r/{hub_code}


def render_hub(*, handle: str, disclosure: str, cards: Sequence[HubCard]) -> str:
    if not disclosure.strip():
        raise ValueError("hub page requires the standard disclosure (§8.1 D-04)")
    return _TEMPLATE.render(handle=handle, disclosure=disclosure, cards=list(cards))
