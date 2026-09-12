"""知识领域规则测试。

重点是三条容易写错的地方：
  置信度按独立来源数而非素材篇数（Q34）
  三层结论的主体必须完整且互斥（Q26）
  没有溯源的结论不得入库
"""

from __future__ import annotations

from datetime import date

import pytest

from lushu.domain.knowledge import (
    MIN_INDEPENDENT_SOURCES,
    Claim,
    ClaimEvidence,
    ClaimSubject,
    Confidence,
    Facet,
    Polarity,
    SubjectType,
    confidence_for,
    refresh_days_for,
)


def _evidence(doc: str = "d1", group: str | None = "g1") -> ClaimEvidence:
    return ClaimEvidence(source_document_id=doc, source_group_id=group, quote="原文片段")


def _claim(**overrides) -> Claim:
    base = dict(
        id="c1",
        subject=ClaimSubject(subject_type=SubjectType.POI, poi_id="B000A8UIN8"),
        polarity=Polarity.AVOID,
        facet=Facet.QUEUE,
        text="下午两点后排队会短很多",
        independent_source_count=1,
        first_seen_at=date(2026, 9, 1),
        evidence=(_evidence(),),
    )
    base.update(overrides)
    return Claim(**base)


# ─── 置信度 ──────────────────────────────────────────────────


def test_multi_source_reaches_high_confidence() -> None:
    assert confidence_for(MIN_INDEPENDENT_SOURCES) is Confidence.HIGH
    assert confidence_for(MIN_INDEPENDENT_SOURCES + 5) is Confidence.HIGH


@pytest.mark.parametrize("count", [1, 2])
def test_below_threshold_stays_single_source(count: int) -> None:
    assert confidence_for(count) is Confidence.SINGLE_SOURCE


def test_reposted_content_counts_as_one_source() -> None:
    """一篇爆款被三个站转载仍然只是一个来源——置信度不能退化成转发量。"""
    claim = _claim(
        independent_source_count=1,
        evidence=(
            _evidence("d1", "g1"),
            _evidence("d2", "g1"),
            _evidence("d3", "g1"),
        ),
    )
    assert claim.distinct_source_groups == 1
    assert claim.confidence is Confidence.SINGLE_SOURCE


def test_genuinely_independent_sources_raise_confidence() -> None:
    claim = _claim(
        independent_source_count=3,
        evidence=(_evidence("d1", "g1"), _evidence("d2", "g2"), _evidence("d3", "g3")),
    )
    assert claim.distinct_source_groups == 3
    assert claim.confidence is Confidence.HIGH


# ─── 主体完整性 ──────────────────────────────────────────────


def test_poi_subject_requires_poi_id() -> None:
    with pytest.raises(ValueError, match="poi_id"):
        ClaimSubject(subject_type=SubjectType.POI)


def test_poi_subject_rejects_city_field() -> None:
    with pytest.raises(ValueError, match="不应带有"):
        ClaimSubject(subject_type=SubjectType.POI, poi_id="p1", city_adcode="110100")


def test_route_subject_requires_two_endpoints() -> None:
    with pytest.raises(ValueError, match="两个端点"):
        ClaimSubject(subject_type=SubjectType.ROUTE, poi_a_id="p1")


def test_route_subject_rejects_identical_endpoints() -> None:
    with pytest.raises(ValueError, match="不能是同一个"):
        ClaimSubject(subject_type=SubjectType.ROUTE, poi_a_id="p1", poi_b_id="p1")


def test_city_subject_requires_adcode() -> None:
    with pytest.raises(ValueError, match="city_adcode"):
        ClaimSubject(subject_type=SubjectType.CITY)


def test_route_subject_is_valid() -> None:
    subject = ClaimSubject(subject_type=SubjectType.ROUTE, poi_a_id="p1", poi_b_id="p2")
    assert subject.poi_a_id == "p1" and subject.poi_b_id == "p2"


# ─── 溯源与构造校验 ──────────────────────────────────────────


def test_claim_without_evidence_is_rejected() -> None:
    with pytest.raises(ValueError, match="证据"):
        _claim(evidence=())


def test_empty_quote_is_rejected() -> None:
    with pytest.raises(ValueError, match="原文片段"):
        ClaimEvidence(source_document_id="d1", quote="   ")


def test_empty_text_is_rejected() -> None:
    with pytest.raises(ValueError, match="正文"):
        _claim(text="  ")


def test_zero_sources_is_rejected() -> None:
    with pytest.raises(ValueError, match="独立来源数"):
        _claim(independent_source_count=0)


# ─── 复验周期分级（Q49）──────────────────────────────────────


def test_volatile_facets_expire_sooner_than_slow_ones() -> None:
    assert refresh_days_for(Facet.PRICE_DIFF) == 90
    assert refresh_days_for(Facet.CLOSURE) == 90
    assert refresh_days_for(Facet.QUEUE) == 180
    assert refresh_days_for(Facet.CROWD) == 180
    assert refresh_days_for(Facet.PRICE_DIFF) < refresh_days_for(Facet.QUEUE)


def test_photo_spots_never_expire() -> None:
    assert refresh_days_for(Facet.PHOTO) is None


def test_verify_due_at_is_computed_from_the_category() -> None:
    claim = _claim(facet=Facet.PRICE_DIFF, first_seen_at=date(2026, 1, 1))
    assert claim.verify_due_at == date(2026, 4, 1)  # 90 天后


def test_last_verified_at_resets_the_clock() -> None:
    claim = _claim(
        facet=Facet.PRICE_DIFF,
        first_seen_at=date(2026, 1, 1),
        last_verified_at=date(2026, 6, 1),
    )
    assert claim.verify_due_at == date(2026, 8, 30)


def test_due_for_review_flips_on_the_boundary() -> None:
    claim = _claim(facet=Facet.PRICE_DIFF, first_seen_at=date(2026, 1, 1))
    assert not claim.is_due_for_review(date(2026, 3, 31))
    assert claim.is_due_for_review(date(2026, 4, 1))


def test_never_expiring_claim_is_never_due() -> None:
    claim = _claim(facet=Facet.PHOTO, first_seen_at=date(2000, 1, 1))
    assert claim.verify_due_at is None
    assert not claim.is_due_for_review(date(2030, 1, 1))
