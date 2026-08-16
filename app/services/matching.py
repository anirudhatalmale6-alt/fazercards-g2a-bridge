"""Match supplier SKUs to store products, and keep the result unique.

Scoring is deliberately conservative.  A wrong mapping is far more expensive
than an unmapped product: an unmapped product is a missed sale, a wrong mapping
sells a customer a 100 HKD card when they paid for a 350 HKD one and ends in a
chargeback plus a marketplace penalty.

So the engine applies **hard gates** first (things that make a pair impossible),
then scores what survives, then only auto-accepts near-certain matches.  Anything
in between lands in the review queue with its runners-up attached.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.logging_conf import get_logger
from app.models import MappingStatus, ProductMapping, StoreProduct, SupplierProduct
from app.services.text import TitleParts, parse_title, parse_title_with

logger = get_logger(__name__)

# Weights for the soft signals; they sum to 1.0.
W_TOKEN_OVERLAP = 0.55
W_SEQUENCE = 0.30
W_REGION = 0.10
W_PLATFORM = 0.05


@dataclass(frozen=True)
class Candidate:
    store_product_id: int
    product_ext_id: str
    name: str
    score: float
    reasons: tuple[str, ...] = ()

    def as_json(self) -> dict:
        return {
            "store_product_id": self.store_product_id,
            "product_ext_id": self.product_ext_id,
            "name": self.name,
            "score": round(self.score, 4),
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class MatchOutcome:
    supplier_product_id: int
    status: MappingStatus
    best: Candidate | None
    candidates: tuple[Candidate, ...]
    method: str


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _denominations_conflict(
    left: tuple[float, str | None] | None, right: tuple[float, str | None] | None
) -> bool:
    """True when two face values cannot be the same product.

    A missing face value on one side is not a conflict -- supplier titles often
    omit the currency the store spells out.  Two *stated* values that differ is.
    """
    if left is None or right is None:
        return False
    amount_l, currency_l = left
    amount_r, currency_r = right
    if abs(amount_l - amount_r) > 0.001:
        return True
    if currency_l and currency_r and currency_l != currency_r:
        return True
    return False


def score_pair(supplier: TitleParts, store: TitleParts) -> tuple[float, tuple[str, ...]]:
    """Return a 0..1 similarity and the reasons behind it.

    A score of 0 means a hard gate rejected the pair outright.
    """
    reasons: list[str] = []

    if _denominations_conflict(supplier.denomination, store.denomination):
        return 0.0, ("denomination mismatch",)

    if (
        supplier.region
        and store.region
        and supplier.region != store.region
        and "GLOBAL" not in (supplier.region, store.region)
    ):
        # Region-locked keys are not interchangeable; this is a gate, not a penalty.
        return 0.0, (f"region mismatch {supplier.region} vs {store.region}",)

    if supplier.platform and store.platform and supplier.platform != store.platform:
        return 0.0, (f"platform mismatch {supplier.platform} vs {store.platform}",)

    token_overlap = _jaccard(supplier.tokens, store.tokens)
    sequence = SequenceMatcher(None, supplier.core, store.core).ratio()

    if supplier.region and store.region and supplier.region == store.region:
        region_score = 1.0
        reasons.append(f"region {supplier.region}")
    elif not supplier.region or not store.region:
        region_score = 0.5  # unknown, not wrong
    else:
        region_score = 0.6  # one side says GLOBAL

    if supplier.platform and store.platform and supplier.platform == store.platform:
        platform_score = 1.0
        reasons.append(f"platform {supplier.platform}")
    elif not supplier.platform or not store.platform:
        platform_score = 0.5
    else:
        platform_score = 0.0

    if (
        supplier.denomination
        and store.denomination
        and abs(supplier.denomination[0] - store.denomination[0]) <= 0.001
    ):
        reasons.append(f"denomination {supplier.denomination[0]:g}")

    score = (
        W_TOKEN_OVERLAP * token_overlap
        + W_SEQUENCE * sequence
        + W_REGION * region_score
        + W_PLATFORM * platform_score
    )
    reasons.append(f"tokens {token_overlap:.2f}")
    reasons.append(f"text {sequence:.2f}")
    return min(1.0, score), tuple(reasons)


class MatchingEngine:
    """Finds candidates for a supplier product among the mirrored store catalogue."""

    def __init__(self, session: Session, *, settings: Settings | None = None, store_code: str = "g2a"):
        self.session = session
        self.settings = settings or get_settings()
        self.store_code = store_code
        self._claimed: set[int] | None = None

    # -------------------------------------------------------------- helpers
    def _claimed_store_products(self) -> set[int]:
        """Store products already owned by a mapping.

        This is the deduplication rule in one place: a store product that is
        already claimed is invisible to every other supplier SKU, so we can
        never end up with two offers on the same product.
        """
        if self._claimed is None:
            rows = self.session.execute(
                select(ProductMapping.store_product_id).where(
                    ProductMapping.store_code == self.store_code,
                    ProductMapping.store_product_id.is_not(None),
                    ProductMapping.status.in_(
                        [
                            MappingStatus.AUTO.value,
                            MappingStatus.APPROVED.value,
                            MappingStatus.PENDING.value,
                        ]
                    ),
                )
            ).scalars()
            self._claimed = {r for r in rows if r is not None}
        return self._claimed

    def _rejected_pairs(self, supplier_product_id: int) -> set[int]:
        """Store products a human already rejected for this SKU -- never re-offer."""
        rows = self.session.execute(
            select(ProductMapping.store_product_id).where(
                ProductMapping.supplier_product_id == supplier_product_id,
                ProductMapping.status == MappingStatus.REJECTED.value,
                ProductMapping.store_product_id.is_not(None),
            )
        ).scalars()
        return {r for r in rows if r is not None}

    def _existing_mapping(self, supplier_product: SupplierProduct) -> ProductMapping | None:
        """Look the mapping up by key rather than through the relationship.

        ``supplier_product.mapping`` can be stale -- a mapping created earlier in
        the same session via its foreign key does not populate the backref, and
        trusting it makes ``apply`` insert a second row and hit the unique
        constraint.
        """
        return self.session.execute(
            select(ProductMapping).where(
                ProductMapping.supplier_product_id == supplier_product.id
            )
        ).scalar_one_or_none()

    def _shortlist(self, parts: TitleParts) -> list[StoreProduct]:
        """Cheap pre-filter before the expensive scoring.

        Exact bucket first; if that finds nothing, fall back to products sharing
        a distinctive token so we do not score the whole catalogue.
        """
        exact = (
            self.session.execute(
                select(StoreProduct).where(
                    StoreProduct.store_code == self.store_code,
                    StoreProduct.match_key == parts.match_key,
                )
            )
            .scalars()
            .all()
        )
        if exact:
            return list(exact)

        anchors = sorted(
            (t for t in parts.tokens if len(t) >= 4 or t.isdigit()),
            key=len,
            reverse=True,
        )[:3]
        if not anchors:
            return []

        stmt = select(StoreProduct).where(StoreProduct.store_code == self.store_code)
        for anchor in anchors[:1]:
            stmt = stmt.where(StoreProduct.name_normalized.like(f"%{anchor}%"))
        # A generic anchor can still match thousands of rows; cap the work.
        return list(self.session.execute(stmt.limit(400)).scalars().all())

    # ---------------------------------------------------------------- public
    @staticmethod
    def _supplier_parts(row: SupplierProduct) -> TitleParts:
        return parse_title_with(row.name, region=row.region, platform=row.platform)

    @staticmethod
    def _store_parts(row: StoreProduct) -> TitleParts:
        return parse_title_with(row.name, region=row.region, platform=row.platform)

    def find_candidates(self, supplier_product: SupplierProduct) -> list[Candidate]:
        parts = self._supplier_parts(supplier_product)
        claimed = self._claimed_store_products()
        rejected = self._rejected_pairs(supplier_product.id)
        existing = self._existing_mapping(supplier_product)
        current = existing.store_product_id if existing is not None else None

        scored: list[Candidate] = []
        for store_product in self._shortlist(parts):
            if store_product.id in rejected:
                continue
            if store_product.id in claimed and store_product.id != current:
                continue
            score, reasons = score_pair(parts, self._store_parts(store_product))
            if score <= 0.0:
                continue
            scored.append(
                Candidate(
                    store_product_id=store_product.id,
                    product_ext_id=store_product.product_ext_id,
                    name=store_product.name,
                    score=score,
                    reasons=reasons,
                )
            )

        scored.sort(key=lambda c: c.score, reverse=True)
        return scored[: self.settings.match_max_candidates]

    def evaluate(self, supplier_product: SupplierProduct) -> MatchOutcome:
        candidates = self.find_candidates(supplier_product)
        best = candidates[0] if candidates else None

        if best is None or best.score < self.settings.match_review_score:
            status = MappingStatus.UNMATCHED
        elif best.score >= self.settings.match_auto_accept_score:
            status = MappingStatus.AUTO
            # A near-tie is not a certainty, even at a high score.
            if len(candidates) > 1 and candidates[1].score >= best.score - 0.02:
                status = MappingStatus.PENDING
        else:
            status = MappingStatus.PENDING

        return MatchOutcome(
            supplier_product_id=supplier_product.id,
            status=status,
            best=best,
            candidates=tuple(candidates),
            method="auto-title-v1",
        )

    def apply(self, supplier_product: SupplierProduct, outcome: MatchOutcome) -> ProductMapping:
        """Write the outcome to the mapping table.

        Never overwrites a human decision: an approved or rejected mapping is
        left exactly as it is.
        """
        mapping = self._existing_mapping(supplier_product)
        if mapping is None:
            mapping = ProductMapping(
                supplier_product_id=supplier_product.id, store_code=self.store_code
            )
            self.session.add(mapping)

        if mapping.status in (MappingStatus.APPROVED.value, MappingStatus.REJECTED.value):
            return mapping

        mapping.candidates = [c.as_json() for c in outcome.candidates]
        mapping.method = outcome.method
        if outcome.best is not None:
            mapping.store_product_id = outcome.best.store_product_id
            mapping.score = outcome.best.score
        else:
            mapping.store_product_id = None
            mapping.score = None
        mapping.status = outcome.status.value
        mapping.updated_at = dt.datetime.now(dt.timezone.utc)

        self.session.flush()
        if mapping.store_product_id is not None and outcome.status in (
            MappingStatus.AUTO,
            MappingStatus.PENDING,
        ):
            self._claimed_store_products().add(mapping.store_product_id)
        return mapping

    def run(self, *, limit: int | None = None, only_unmapped: bool = True) -> dict[str, int]:
        """Match every supplier product that needs it.  Returns counters."""
        stmt = select(SupplierProduct).where(SupplierProduct.is_active.is_(True))
        if only_unmapped:
            stmt = stmt.outerjoin(
                ProductMapping,
                ProductMapping.supplier_product_id == SupplierProduct.id,
            ).where(
                (ProductMapping.id.is_(None))
                | (
                    ProductMapping.status.in_(
                        [MappingStatus.UNMATCHED.value, MappingStatus.PENDING.value]
                    )
                )
            )
        if limit:
            stmt = stmt.limit(limit)

        counters = {"examined": 0, "auto": 0, "pending": 0, "unmatched": 0}
        for supplier_product in self.session.execute(stmt).scalars():
            counters["examined"] += 1
            outcome = self.evaluate(supplier_product)
            self.apply(supplier_product, outcome)
            if outcome.status is MappingStatus.AUTO:
                counters["auto"] += 1
            elif outcome.status is MappingStatus.PENDING:
                counters["pending"] += 1
            else:
                counters["unmatched"] += 1
        return counters


def approve(
    session: Session,
    mapping_id: int,
    *,
    store_product_id: int | None = None,
    reviewed_by: str = "operator",
    note: str | None = None,
) -> ProductMapping:
    """Confirm a mapping, optionally overriding which store product it points at."""
    mapping = session.get(ProductMapping, mapping_id)
    if mapping is None:
        raise LookupError(f"mapping {mapping_id} not found")
    if store_product_id is not None:
        clash = session.execute(
            select(ProductMapping).where(
                ProductMapping.store_code == mapping.store_code,
                ProductMapping.store_product_id == store_product_id,
                ProductMapping.id != mapping.id,
                ProductMapping.status.in_(
                    [MappingStatus.AUTO.value, MappingStatus.APPROVED.value]
                ),
            )
        ).scalar_one_or_none()
        if clash is not None:
            raise ValueError(
                f"store product {store_product_id} is already mapped by mapping {clash.id}"
            )
        mapping.store_product_id = store_product_id
    if mapping.store_product_id is None:
        raise ValueError("cannot approve a mapping with no store product")
    mapping.status = MappingStatus.APPROVED.value
    mapping.reviewed_by = reviewed_by
    mapping.reviewed_at = dt.datetime.now(dt.timezone.utc)
    if note:
        mapping.note = note
    session.flush()
    return mapping


def reject(
    session: Session, mapping_id: int, *, reviewed_by: str = "operator", note: str | None = None
) -> ProductMapping:
    """Reject a suggestion.  The pair is remembered and never suggested again."""
    mapping = session.get(ProductMapping, mapping_id)
    if mapping is None:
        raise LookupError(f"mapping {mapping_id} not found")
    mapping.status = MappingStatus.REJECTED.value
    mapping.reviewed_by = reviewed_by
    mapping.reviewed_at = dt.datetime.now(dt.timezone.utc)
    if note:
        mapping.note = note
    session.flush()
    return mapping
