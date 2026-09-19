"""领域对象：权利类型、谱系节点、系列、衍生关系与各方贡献。

只保留稳定的数据结构与序列化规则，不包含业务判定逻辑。
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
import hashlib
import json

# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def utcnow():
    return datetime.now(timezone.utc)


def now_iso():
    """全系统统一的时间戳格式（UTC，带时区偏移）。"""
    return utcnow().isoformat(timespec="seconds")


def parse_iso(value):
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def canonical_json(data):
    """对任意 JSON 兼容数据生成稳定的字节串，作为散列与快照的基础。"""
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(data):
    """生成数据的内容散列（SHA-256 十六进制）。"""
    return hashlib.sha256(canonical_json(data)).hexdigest()


def short_id(prefix, raw):
    return f"{prefix}_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:12]}"


# ---------------------------------------------------------------------------
# 权利与角色
# ---------------------------------------------------------------------------

# 四类机构掌握的权利
RIGHT_PUBLICATION = "publication"  # 出版权
RIGHT_COLLECTION = "collection"   # 馆藏（原作物权/采集授权）
RIGHT_CREATION = "creation"       # 创作（著作权/改编）
RIGHT_SCREENING = "screening"     # 展映权

ALL_RIGHTS = (RIGHT_PUBLICATION, RIGHT_COLLECTION, RIGHT_CREATION, RIGHT_SCREENING)

RIGHT_LABELS = {
    RIGHT_PUBLICATION: "出版权",
    RIGHT_COLLECTION: "馆藏权",
    RIGHT_CREATION: "创作权",
    RIGHT_SCREENING: "展映权",
}

# 谱系节点类型
NODE_ORIGINAL = "original_work"
NODE_BATCH = "acquisition_batch"
NODE_CROP = "derivative_crop"
NODE_EDITION = "art_edition"

# 作品形态
EDITION_LIMITED = "limited"   # 四款限量数字作品
EDITION_REPLICA = "replica"   # 带独立编号的仿制版
EDITION_FREE = "free"         # 可免费领取的诗句版本

# 谱系关系
REL_SOURCE = "source_of"          # 原作 -> 采集批次
REL_CROPPED = "cropped_from"      # 批次(底本) -> 局部裁切
REL_DERIVED = "derived_from"      # 裁切 -> 数字作品
REL_EMBODIED = "embodied_by"      # 数字作品 -> 系列发行物
REL_MATERIAL = "material_of"      # 展示素材 -> 展映（可替换，历史保留）


@dataclass
class Party:
    """权利方/贡献方。"""
    id: str
    name: str
    kind: str = "institution"  # institution | creator | platform

    def to_dict(self):
        return asdict(self)


@dataclass
class Contribution:
    """某机构/个人对某节点的贡献及其持有的权利。"""
    id: str
    node_id: str
    party_id: str
    role: str
    rights: list = field(default_factory=list)
    note: str = ""
    created_at: str = field(default_factory=now_iso)

    def to_dict(self):
        return asdict(self)


@dataclass
class ProvenanceEdge:
    """谱系有向边：child_id 来源于 parent_id。"""
    parent_id: str
    child_id: str
    relation: str
    detail: dict = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)
    # material_of 关系允许替换：历史边保留，active=False 标记被换版
    active: bool = True

    def to_dict(self):
        return asdict(self)


@dataclass
class Node:
    """谱系节点。不同 kind 使用 metadata 承载各自的来源信息。"""
    id: str
    kind: str
    title: str
    metadata: dict = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)

    def to_dict(self):
        return asdict(self)


@dataclass
class Series:
    """发行系列：限量版 / 仿制版 / 免费诗句版。

    required_rights：发行该系列所需齐备的权利。
    edition_size：发行上限；None 表示不限量（免费领取）。
    """
    id: str
    title: str
    kind: str
    work_node_id: str
    edition_size: object = None
    # None 表示按用途解析（发行/出版/展映所需权利不同）；显式列表则全用途强制
    required_rights: object = None
    created_at: str = field(default_factory=now_iso)

    def to_dict(self):
        return asdict(self)
