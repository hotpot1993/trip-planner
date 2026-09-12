"""POI 领域模型的测试：类型分类、层级、跨城校验。

分类规则全部来自实测（docs/M3-PROBE.md 第三节），样本是高德的真实返回。
这些规则的价值在于**挡住不该进候选的东西**：停车场、公交站、售票处、
上车点。放过任何一个，它就会出现在行程里。
"""

from __future__ import annotations

import pytest

from lushu.domain.poi import CandidatePoi, PoiKind, PoiLevel, classify_poi


def poi(poi_id: str, name: str, **kwargs: object) -> CandidatePoi:
    defaults: dict[str, object] = {
        "typecode": "110200",
        "type_name": "风景名胜;风景名胜;风景名胜",
        "adcode": "110101",
        "city_name": "北京市",
    }
    defaults.update(kwargs)
    return CandidatePoi(poi_id=poi_id, name=name, **defaults)  # type: ignore[arg-type]


class TestClassifyPoi:
    """类型分类。这是把停车场与公交站挡在候选之外的那道闸。"""

    @pytest.mark.parametrize(
        ("typecode", "expected"),
        [
            ("110201", PoiKind.DESTINATION),  # 故宫博物院
            ("110202", PoiKind.DESTINATION),  # 秦始皇帝陵博物院
            ("140100", PoiKind.DESTINATION),  # 陕西历史博物馆，博物馆
            ("080300", PoiKind.DESTINATION),  # 体育休闲
            ("060101", PoiKind.IRRELEVANT),  # 开元商城钟楼店，商场
            ("150904", PoiKind.IRRELEVANT),  # 陕西历史博物馆停车场
            ("150700", PoiKind.IRRELEVANT),  # 陕西历史博物馆(公交站)
            ("150500", PoiKind.IRRELEVANT),  # 钟楼(地铁站)
            ("070000", PoiKind.IRRELEVANT),  # 故宫博物院检票处、网络预约发票处
            ("200301", PoiKind.IRRELEVANT),  # 午门西卫生间
            ("120000", PoiKind.IRRELEVANT),  # 兵马俑旅游广场，商务住宅
            ("100100", PoiKind.IRRELEVANT),  # 回民街家庭民宿，宾馆酒店
            ("190700", PoiKind.PLACE_NAME),  # 钟楼，热点地名
            ("190301", PoiKind.PLACE_NAME),  # 洒金桥，交通地名;桥
        ],
    )
    def test_by_typecode(self, typecode: str, expected: PoiKind) -> None:
        assert classify_poi(typecode) is expected

    def test_huimin_street_is_a_destination_despite_being_shopping(self) -> None:
        """回民街的实测 typecode 是 061001，而它确实值得去。

        按「购物服务一律排除」会把它丢掉，所以特色商业街是白名单而不是放行前缀。
        """
        assert classify_poi("061001", "购物服务;特色商业街;步行街") is PoiKind.DESTINATION

    def test_shopping_mall_is_not_a_destination(self) -> None:
        """同属购物服务，商场不该进候选。"""
        assert classify_poi("060101", "购物服务;商场;购物中心") is PoiKind.IRRELEVANT
        assert classify_poi("061209", "购物服务;专卖店;礼品饰品店") is PoiKind.IRRELEVANT

    def test_shuttle_stop_is_rejected_despite_scenic_typecode(self) -> None:
        """「兵马俑直通车乘车点」的 typecode 是 110000（风景名胜）。

        只按类型码判断会把它当成候选，而它是个上车点。名称判据必须参与。
        """
        assert (
            classify_poi("110000", "风景名胜;风景名胜相关;旅游景点", "兵马俑直通车乘车点")
            is PoiKind.IRRELEVANT
        )
        assert (
            classify_poi("110000", "风景名胜;风景名胜相关;旅游景点", "西安城墙-游客中心")
            is PoiKind.DESTINATION
        )

    def test_falls_back_to_type_name_when_typecode_missing(self) -> None:
        """现有 poi 表的 typecode 全是空的（docs/M3-PROBE.md 第四节），退化路径不是摆设。"""
        assert classify_poi(None, "风景名胜;风景名胜;国家级景点") is PoiKind.DESTINATION
        assert classify_poi("", "地名地址信息;交通地名;道路名") is PoiKind.PLACE_NAME
        assert classify_poi(None, "交通设施服务;停车场;公共停车场") is PoiKind.IRRELEVANT

    def test_unknown_is_irrelevant(self) -> None:
        """宁可漏掉也不能放过：不确定的候选不该自己挂到行程上。"""
        assert classify_poi(None, None) is PoiKind.IRRELEVANT
        assert classify_poi("999999") is PoiKind.IRRELEVANT


class TestPoiLevel:
    def test_parent_empty_means_root(self) -> None:
        assert poi("B000A8UIN8", "故宫博物院").level is PoiLevel.ROOT

    def test_parent_present_means_sub(self) -> None:
        assert poi("B000A84GDN", "故宫博物院-午门", parent_id="B000A8UIN8").level is PoiLevel.SUB


class TestInCity:
    def test_same_city_different_district_passes(self) -> None:
        """兵马俑在临潼区（610115），陕历博在雁塔区（610113），都属西安（610100）。"""
        assert poi("B001D09OYW", "秦始皇帝陵博物院", adcode="610115").in_city("610100")

    def test_missing_adcode_is_rejected(self) -> None:
        """没有 adcode 就无法校验城市，放过它等于承认跨城错误。"""
        assert not poi("X", "某景点", adcode=None).in_city("610100")

    def test_other_city_is_rejected(self) -> None:
        assert not poi("X", "某景点", adcode="610104").in_city("110100")

    def test_no_target_city_accepts_everything(self) -> None:
        assert poi("X", "某景点", adcode="610104").in_city(None)


class TestCandidatePoi:
    def test_id_is_required(self) -> None:
        """高德 id 是实体主键（ADR-0002），没有它这条数据没有身份。"""
        with pytest.raises(ValueError, match="主键"):
            CandidatePoi(poi_id="  ", name="某景点")

    def test_kind_uses_name_too(self) -> None:
        """类型码不足以判断，名称要一起看。"""
        assert poi("X", "兵马俑(公交站)", typecode="150700").kind is PoiKind.IRRELEVANT
        assert poi("X", "故宫博物院-午门", parent_id="B000A8UIN8").kind is PoiKind.DESTINATION
