"""演示场景播种：灵境艺术馆 · 祝允明《草书诗帖》数字作品。

谱系：
  原作《草书诗帖》
    └─ 高精度采集批次
         ├─ 局部裁切一/二/三/四
         │    └─ 四款限量数字作品（各独立系列）
         └─ 仿制底本裁切
              └─ 带独立编号的仿制版
  诗句文本 ─┘
              └─ 可免费领取的诗句版本（不限量）

四类权利分属出版机构、馆藏机构、数字创作方与展映运营方。
"""

from registry import Registry
from application import ArtRightsService
from provenance import ProvenanceService
from datetime import timedelta
from domain import (
    EDITION_LIMITED, EDITION_REPLICA, EDITION_FREE,
    RIGHT_PUBLICATION, RIGHT_COLLECTION, RIGHT_CREATION, RIGHT_SCREENING,
    utcnow,
)
from licensing import (
    MEDIA_DIGITAL, MEDIA_PRINT, MEDIA_SCREEN, MEDIA_PHYSICAL,
    PURPOSE_DISTRIBUTION, PURPOSE_PUBLISH, PURPOSE_PUBLIC_SCREENING,
)

REGION_CN = "CN"
REGION_GLOBAL = "*"


def build():
    registry = Registry()
    svc = ArtRightsService(registry)
    prov = ProvenanceService(registry)

    # ---- 各方机构 ----
    svc.register_party("LJ", "灵境艺术馆", "institution")          # 馆藏
    svc.register_party("CBS", "中华书画出版社", "institution")      # 出版
    svc.register_party("EXH", "云展映运营中心", "institution")      # 展映
    svc.register_party("DS1", "墨韵数字工作室", "creator")          # 创作一
    svc.register_party("DS2", "飞花数字工作室", "creator")
    svc.register_party("DS3", "惊鸿数字工作室", "creator")
    svc.register_party("DS4", "游龙数字工作室", "creator")

    # ---- 原作与采集 ----
    svc.register_original(
        "ORG_ZYSM", "祝允明《草书诗帖》",
        artist="祝允明", dynasty="明", medium="纸本草书卷",
        held_by="LJ", catalog_no="LJ-AAA-0117")
    svc.add_contribution("CT_ORG_LJ", "ORG_ZYSM", "LJ", "馆藏与底本提供",
                         [RIGHT_COLLECTION])

    svc.register_batch(
        "BATCH_HD2026", "ORG_ZYSM", "2026 年高精度全卷采集批次",
        method="多光谱扫描", dpi=1200, acquired_by="LJ", batch_no="HD-2026-03")
    svc.add_contribution("CT_BATCH_LJ", "BATCH_HD2026", "LJ", "采集执行与质检",
                         [RIGHT_COLLECTION])

    # ---- 四个局部裁切与四款限量作品 ----
    crops = [
        ("CROP_1", "《太湖》诗句局部", {"bbox": "卷首 0.00-0.21", "verse": "太湖惟见水如天"}),
        ("CROP_2", "《闲居》诗句局部", {"bbox": "0.24-0.46", "verse": "闲门客去过逢稀"}),
        ("CROP_3", "《秋日》诗句局部", {"bbox": "0.49-0.71", "verse": "秋日荒凉石径危"}),
        ("CROP_4", "《行乐》诗句局部", {"bbox": "0.74-1.00", "verse": "百年行乐未知归"}),
    ]
    creators = [("DS1", "墨韵"), ("DS2", "飞花"), ("DS3", "惊鸿"), ("DS4", "游龙")]
    for i, ((crop_id, crop_title, detail), (creator_id, creator_name)) in enumerate(
            zip(crops, creators), start=1):
        svc.register_crop(crop_id, "BATCH_HD2026", crop_title, **detail)
        svc.add_contribution(f"CT_CROP_{i}_LJ", crop_id, "LJ", "局部裁切审定",
                             [RIGHT_COLLECTION])
        work_id = f"WORK_{i}"
        svc.register_artwork(work_id, [crop_id], f"{creator_name}·{crop_title}",
                             treatment="动态笔触重构", creator=creator_id)
        svc.add_contribution(f"CT_WORK_{i}_CR", work_id, creator_id, "数字创作",
                             [RIGHT_CREATION])

    # ---- 仿制版（独立编号）与免费诗句版 ----
    svc.register_crop("CROP_R", "BATCH_HD2026", "全卷等比例仿制底本",
                      bbox="全卷", treatment="原色仿真")
    svc.add_contribution("CT_CROPR_LJ", "CROP_R", "LJ", "仿制底本审定",
                         [RIGHT_COLLECTION])
    svc.register_artwork("WORK_R", ["CROP_R"], "《草书诗帖》高精度仿制版",
                         treatment="原寸原色仿制")
    svc.add_contribution("CT_WORKR_CBS", "WORK_R", "CBS", "仿制出版监制",
                         [RIGHT_PUBLICATION])

    svc.register_artwork("WORK_F", ["CROP_1", "CROP_2", "CROP_3", "CROP_4"],
                         "《草书诗帖》诗句领取版", treatment="诗句静态卡片")
    svc.add_contribution("CT_WORKF_LJ", "WORK_F", "LJ", "诗句卡片编排",
                         [RIGHT_CREATION])
    svc.add_contribution("CT_WORKF_CBS", "WORK_F", "CBS", "诗句文字校订",
                         [RIGHT_PUBLICATION])

    # ---- 发行系列 ----
    for i in range(1, 5):
        svc.create_series(f"LIM_{i}", f"WORK_{i}",
                          f"草书诗帖·限量数字作品 第{i}款",
                          kind=EDITION_LIMITED, edition_size=99)
    svc.create_series("REP", "WORK_R", "草书诗帖·独立编号仿制版",
                      kind=EDITION_REPLICA, edition_size=500)
    svc.create_series("FREE", "WORK_F", "草书诗帖·诗句免费领取版",
                      kind=EDITION_FREE, edition_size=None)

    # ---- 许可（按地域/媒介/用途/期限组合）----
    # 馆藏与创作许可以节点为锚，覆盖数字发行与展映
    svc.grant_license(
        "LIC_COLL_DIG", RIGHT_COLLECTION, "LJ", "LJ",
        regions=[REGION_CN],
        media=[MEDIA_DIGITAL, MEDIA_SCREEN, MEDIA_PHYSICAL],
        purposes=[PURPOSE_DISTRIBUTION, PURPOSE_PUBLIC_SCREENING],
        node_ids=["ORG_ZYSM", "BATCH_HD2026",
                  "CROP_1", "CROP_2", "CROP_3", "CROP_4", "CROP_R"],
        terms_ref="HT-LJ-2026-017", note="馆藏数字化及实物仿制授权主合同")
    svc.grant_license(
        "LIC_PUB_DIG", RIGHT_PUBLICATION, "CBS", "LJ",
        regions=[REGION_CN], media=[MEDIA_DIGITAL, MEDIA_PRINT, MEDIA_PHYSICAL],
        purposes=[PURPOSE_DISTRIBUTION, PURPOSE_PUBLISH],
        node_ids=["ORG_ZYSM", "WORK_F", "WORK_R"],
        terms_ref="HT-CBS-2026-033", note="数字与纸本出版转授权")
    svc.grant_license(
        "LIC_PUB_FREE", RIGHT_PUBLICATION, "CBS", "LJ",
        regions=[REGION_GLOBAL], media=[MEDIA_DIGITAL],
        purposes=[PURPOSE_DISTRIBUTION],
        node_ids=["WORK_F"],
        terms_ref="HT-CBS-2026-034", note="诗句版全球免费领取专项")
    svc.grant_license(
        "LIC_COLL_FREE", RIGHT_COLLECTION, "LJ", "LJ",
        regions=[REGION_GLOBAL], media=[MEDIA_DIGITAL],
        purposes=[PURPOSE_DISTRIBUTION],
        node_ids=["WORK_F"],
        terms_ref="HT-LJ-2026-018", note="诗句免费领取的馆藏全球授权")
    svc.grant_license(
        "LIC_CRE_FREE", RIGHT_CREATION, "LJ", "LJ",
        regions=[REGION_GLOBAL], media=[MEDIA_DIGITAL],
        purposes=[PURPOSE_DISTRIBUTION],
        node_ids=["WORK_F"],
        terms_ref="HT-LJ-2026-019", note="诗句卡片编排自用授权")
    for i, (creator_id, _) in enumerate(creators, start=1):
        svc.grant_license(
            f"LIC_CRE_{i}", RIGHT_CREATION, creator_id, "LJ",
            regions=[REGION_CN, REGION_GLOBAL], media=[MEDIA_DIGITAL, MEDIA_SCREEN],
            purposes=[PURPOSE_DISTRIBUTION, PURPOSE_PUBLIC_SCREENING],
            node_ids=[f"WORK_{i}"],
            terms_ref=f"HT-DS{i}-2026-00{i}", note="数字创作授权及收益分成")
    svc.grant_license(
        "LIC_CRE_R", RIGHT_CREATION, "CBS", "LJ",
        regions=[REGION_CN], media=[MEDIA_PHYSICAL, MEDIA_DIGITAL],
        purposes=[PURPOSE_DISTRIBUTION],
        node_ids=["WORK_R"],
        terms_ref="HT-CBS-2026-035", note="仿制版监制创作授权")
    screen_from = (utcnow() - timedelta(days=30)).isoformat(timespec="seconds")
    screen_until = (utcnow() + timedelta(days=730)).isoformat(timespec="seconds")
    svc.grant_license(
        "LIC_SCR", RIGHT_SCREENING, "EXH", "LJ",
        regions=[REGION_CN], media=[MEDIA_SCREEN],
        purposes=[PURPOSE_PUBLIC_SCREENING],
        node_ids=["WORK_1", "WORK_2", "WORK_3", "WORK_4"],
        effective_from=screen_from,
        effective_until=screen_until,
        terms_ref="HT-EXH-2026-008", note="两年期公开展映授权")

    return registry, svc, prov


if __name__ == "__main__":
    registry, svc, prov = build()
    print(f"场景播种完成：{len(registry.list('nodes'))} 个谱系节点，"
          f"{len(registry.list('licenses'))} 份许可，"
          f"{len(registry.list('series'))} 个发行系列")
