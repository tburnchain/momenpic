"""What the gate judges: a content item (gate stage) or one publication of it (preflight stage),
with every text field addressable so rules can patch them and the engine can write patches back."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from acvm.domain.models import AffiliateLink, Asset, ChannelAccount, Content, Finding, Product, Publication
from acvm.policy.facts import ProductFacts

Stage = Literal["gate", "preflight"]


@dataclass
class TextField:
    name: str
    text: str
    disclosure_first: bool  # D-01 applies to this field
    in_render: bool  # the text is burned into / spoken in the rendered video


@dataclass
class GateSubject:
    stage: Stage
    content: Content
    product: Product
    facts: ProductFacts
    accounts: dict[uuid.UUID, ChannelAccount]
    now: datetime
    fields: dict[str, TextField] = field(default_factory=dict)
    publication: Publication | None = None
    link: AffiliateLink | None = None
    assets: dict[uuid.UUID, Asset] = field(default_factory=dict)
    flags: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def for_content(cls, content: Content, product: Product, facts: ProductFacts,
                    accounts: dict[uuid.UUID, ChannelAccount], assets: dict[uuid.UUID, Asset],
                    now: datetime) -> GateSubject:
        s = cls("gate", content, product, facts, accounts, now, assets=assets)
        sc = content.script
        rendered = content.render is not None
        if sc.body:
            s.fields["script.body"] = TextField("script.body", sc.body, True, False)
        if sc.caption:
            s.fields["script.caption"] = TextField("script.caption", sc.caption, True, False)
        if sc.title:
            s.fields["script.title"] = TextField("script.title", sc.title, False, False)
        for i, h in enumerate(sc.hooks):
            s.fields[f"hook[{i}]"] = TextField(f"hook[{i}]", h, False, rendered and i == sc.chosen_hook)
        for i, b in enumerate(sc.beats):
            s.fields[f"beat[{i}].narration"] = TextField(f"beat[{i}].narration", b.narration, False, rendered)
            if b.on_screen_text:
                s.fields[f"beat[{i}].on_screen"] = TextField(f"beat[{i}].on_screen", b.on_screen_text, False, rendered)
        return s

    @classmethod
    def for_publication(cls, content: Content, product: Product, facts: ProductFacts, account: ChannelAccount,
                        publication: Publication, link: AffiliateLink | None, now: datetime) -> GateSubject:
        s = cls("preflight", content, product, facts, {account.id: account}, now, publication=publication, link=link)
        s.fields["publication.caption"] = TextField("publication.caption", publication.caption, True, False)
        return s

    def texts(self) -> list[TextField]:
        return list(self.fields.values())

    def apply(self) -> tuple[Content, Publication | None]:
        """Write patched field texts back into the domain objects."""
        sc = self.content.script
        beats = list(sc.beats)
        hooks = list(sc.hooks)
        body, caption, title = sc.body, sc.caption, sc.title
        for f in self.fields.values():
            if f.name == "script.body":
                body = f.text
            elif f.name == "script.caption":
                caption = f.text
            elif f.name == "script.title":
                title = f.text
            elif f.name.startswith("hook["):
                hooks[int(f.name[5:-1])] = f.text
            elif f.name.startswith("beat["):
                idx = int(f.name[5 : f.name.index("]")])
                key = "narration" if f.name.endswith("narration") else "on_screen_text"
                beats[idx] = beats[idx].model_copy(update={key: f.text})
        script = sc.model_copy(update={"beats": beats, "hooks": hooks, "body": body, "caption": caption,
                                       "title": title})
        content = self.content.model_copy(update={"script": script})
        pub = self.publication
        if pub is not None and "publication.caption" in self.fields:
            pub = pub.model_copy(update={"caption": self.fields["publication.caption"].text,
                                         "ai_label": bool(self.flags.get("ai_label", pub.ai_label))})
        return content, pub


@dataclass
class RuleResult:
    findings: list[Finding] = field(default_factory=list)
    patched: bool = False
    actions: set[str] = field(default_factory=set)
