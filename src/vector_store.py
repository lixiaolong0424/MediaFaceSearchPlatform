"""向量存储抽象接口 + Qdrant 实现（规格第十三章 vector_store.py）。

抽象接口：upsert / search_knn / delete_by_file / count / reconcile
换任何后端（FAISS/Milvus/pgvector）只需再实现一个 VectorStore 子类。

Qdrant 约定：
  * 两个 collection：materials_semantic(2048) / materials_faces(512)
  * distance = Cosine，向量 int8 量化入库
  * point_id = (file_id << 20) | shot_ix  —— 两条管线回退到同一「文件+时间段」的耦合点
  * payload 扁平化且禁放模型相关字段：kind,file_id,shot_ix,sec,shot_start,shot_end,path,thumb_path
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .common import Config, get_logger, make_point_id, split_point_id

LOG = get_logger("vector_store")

# payload 白名单：规格 5.L2c「入库 payload 扁平化且禁放模型相关字段」
#
# semantic 侧（规格 5.L2c）：kind,file_id,shot_ix,sec,shot_start,shot_end,path,thumb_path
# face 侧（规格第六章 F3）：payload file_id,shot_ix,bbox,quality,face_thumb_path
#   —— 这里用 thumb_path 作为统一字段名（face 侧的 face_thumb_path 与它的语义完全一致，
#      统一命名避免检索层要按 collection 分支取字段）。
# face_id 必须进 payload：src/search.py 的 -face_id 差集与 ui_wall 的概览去重都读它。
PAYLOAD_KEYS = ("kind", "file_id", "shot_ix", "sec", "shot_start", "shot_end",
                "path", "thumb_path", "face_id", "bbox", "quality")


@dataclass
class Hit:
    """一条向量命中。语义侧 score=sim，人脸侧 score=face，两者绝不混排。"""

    point_id: int
    score: float
    payload: Dict[str, Any]

    @property
    def file_id(self) -> int:
        return int(self.payload.get("file_id") or split_point_id(self.point_id)[0])

    @property
    def shot_ix(self) -> int:
        return int(self.payload.get("shot_ix") or split_point_id(self.point_id)[1])


class VectorStore(ABC):
    """向量库抽象接口。"""

    name: str = "abstract"

    @abstractmethod
    def ensure_collection(self, collection: str, dim: int, *, recreate: bool = False) -> None: ...

    @abstractmethod
    def upsert(
        self,
        collection: str,
        ids: Sequence[int],
        vectors: Sequence[Sequence[float]],
        payloads: Sequence[Dict[str, Any]],
    ) -> int: ...

    @abstractmethod
    def search_knn(
        self,
        collection: str,
        vector: Sequence[float],
        k: int,
        *,
        flt: Optional[Dict[str, Any]] = None,
    ) -> List[Hit]: ...

    @abstractmethod
    def delete_by_file(self, collection: str, file_id: int) -> int: ...

    @abstractmethod
    def count(self, collection: str) -> int: ...

    @abstractmethod
    def reconcile(self, collection: str, expected_point_ids: Optional[Iterable[int]] = None) -> Dict[str, Any]: ...

    # 便利方法 ------------------------------------------------------------- #
    def search_many(
        self,
        collection: str,
        vectors: Sequence[Sequence[float]],
        k: int,
        *,
        flt: Optional[Dict[str, Any]] = None,
    ) -> List[List[Hit]]:
        return [self.search_knn(collection, v, k, flt=flt) for v in vectors]

    def close(self) -> None:  # pragma: no cover - 默认无状态
        return None


def sanitize_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """只保留白名单字段，丢掉 None，禁止模型相关信息进入 payload。"""
    return {k: payload[k] for k in PAYLOAD_KEYS if k in payload and payload[k] is not None}


class QdrantVectorStore(VectorStore):
    """Qdrant 实现（本地 Docker 容器，绑 127.0.0.1）。"""

    name = "qdrant"

    def __init__(self, cfg: Config, *, url: Optional[str] = None, timeout: Optional[float] = None):
        try:
            from qdrant_client import QdrantClient
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "缺少 qdrant-client。修复：.venv\\Scripts\\python.exe -m pip install qdrant-client"
            ) from exc

        self.cfg = cfg
        self.url = url or str(cfg.get("qdrant.url", "http://127.0.0.1:7333"))
        self.timeout = float(timeout or cfg.get("qdrant.timeout_sec", 30))
        self._client = QdrantClient(url=self.url, timeout=self.timeout,
                                    prefer_grpc=bool(cfg.get("qdrant.prefer_grpc", False)))
        self._ensured: set[Tuple[str, int]] = set()

    # -- 内部 -------------------------------------------------------------- #
    @property
    def client(self):
        return self._client

    def health(self) -> Tuple[bool, str]:
        try:
            cols = self._client.get_collections()
            names = [c.name for c in cols.collections]
            return True, f"ok, collections={names}"
        except Exception as exc:  # noqa: BLE001 - 需要把任何连接异常变成可读文案
            return False, (
                f"Qdrant 不可达（{self.url}）：{exc}\n"
                "修复：docker start qdrant   或   "
                "docker run -d --name qdrant -p 127.0.0.1:7333:6333 "
                "-v qdrant_storage:/qdrant/storage qdrant/qdrant"
            )

    def _quantization(self):
        mode = str(self.cfg.get("qdrant.quantization", "int8")).lower()
        if mode in ("none", "", "off", "false"):
            return None
        try:
            from qdrant_client import models as qm
        except ImportError:  # pragma: no cover
            return None
        if mode == "int8":
            return qm.ScalarQuantization(
                scalar=qm.ScalarQuantizationConfig(
                    type=qm.ScalarType.INT8, quantile=0.99, always_ram=True
                )
            )
        return None

    # -- 接口实现 ---------------------------------------------------------- #
    def ensure_collection(self, collection: str, dim: int, *, recreate: bool = False) -> None:
        from qdrant_client import models as qm

        key = (collection, dim)
        if not recreate and key in self._ensured:
            return
        exists = False
        try:
            exists = self._client.collection_exists(collection)
        except Exception:  # noqa: BLE001 - 老版本客户端无此方法
            exists = collection in [c.name for c in self._client.get_collections().collections]

        if recreate and exists:
            LOG.warning("重建 collection %s（原有点将被删除）", collection)
            self._client.delete_collection(collection)
            exists = False

        if not exists:
            self._client.create_collection(
                collection_name=collection,
                vectors_config=qm.VectorParams(size=dim, distance=qm.Distance.COSINE),
                hnsw_config=qm.HnswConfigDiff(
                    m=int(self.cfg.get("qdrant.hnsw_m", 16)),
                    ef_construct=int(self.cfg.get("qdrant.hnsw_ef_construct", 128)),
                ),
                quantization_config=self._quantization(),
                on_disk_payload=bool(self.cfg.get("qdrant.on_disk_payload", False)),
            )
            LOG.info("已创建 collection %s（dim=%d, cosine, int8=%s）", collection, dim,
                     self.cfg.get("qdrant.quantization"))
        self._ensured.add(key)

    def upsert(
        self,
        collection: str,
        ids: Sequence[int],
        vectors: Sequence[Sequence[float]],
        payloads: Sequence[Dict[str, Any]],
    ) -> int:
        from qdrant_client import models as qm

        if not ids:
            return 0
        if not (len(ids) == len(vectors) == len(payloads)):
            raise ValueError("ids/vectors/payloads 长度必须一致")
        dim = len(vectors[0])
        self.ensure_collection(collection, dim)
        points = [
            qm.PointStruct(
                id=int(pid),
                vector=[float(x) for x in vec],
                payload=sanitize_payload(pl),
            )
            for pid, vec, pl in zip(ids, vectors, payloads)
        ]
        self._client.upsert(collection_name=collection, points=points, wait=True)
        return len(points)

    def search_knn(
        self,
        collection: str,
        vector: Sequence[float],
        k: int,
        *,
        flt: Optional[Dict[str, Any]] = None,
    ) -> List[Hit]:
        from qdrant_client import models as qm

        self.ensure_collection(collection, len(vector))
        qfilter = None
        if flt:
            must = []
            for field, value in flt.items():
                if isinstance(value, (list, tuple, set)):
                    must.append(qm.FieldCondition(key=field, match=qm.MatchAny(any=list(value))))
                elif isinstance(value, dict) and "range" in value:
                    must.append(qm.FieldCondition(key=field, range=qm.Range(**value["range"])))
                else:
                    must.append(qm.FieldCondition(key=field, match=qm.MatchValue(value=value)))
            qfilter = qm.Filter(must=must)

        try:
            resp = self._client.query_points(
                collection_name=collection,
                query=[float(x) for x in vector],
                limit=int(k),
                query_filter=qfilter,
                with_payload=True,
                with_vectors=False,
            )
            points = list(resp.points)
        except AttributeError:  # pragma: no cover - 兼容老客户端
            points = list(self._client.search(
                collection_name=collection,
                query_vector=[float(x) for x in vector],
                limit=int(k),
                query_filter=qfilter,
                with_payload=True,
                with_vectors=False,
            ))
        return [Hit(point_id=int(p.id), score=float(p.score), payload=dict(p.payload or {}))
                for p in points]

    def delete_by_file(self, collection: str, file_id: int) -> int:
        from qdrant_client import models as qm

        before = self.count(collection, file_id=file_id)
        if not before:
            return 0
        self._client.delete(
            collection_name=collection,
            points_selector=qm.FilterSelector(
                filter=qm.Filter(must=[qm.FieldCondition(
                    key="file_id", match=qm.MatchValue(value=int(file_id)))])
            ),
            wait=True,
        )
        return before

    def delete_by_files(self, collection: str, file_ids: Sequence[int]) -> int:
        """一次请求删掉一批 file_id 的所有点。

        为什么要有它：`delete_by_file` 每个文件要 2 次往返（count + delete），
        1700 个文件就是 6800 次 HTTP，慢到 0.6 条/秒。
        这里用 MatchAny 一次删 2000 个 file_id，且**不做 count**
        （调用方只需要"删掉"这个结果，不需要精确返回值）。
        """
        from qdrant_client import models as qm

        ids = [int(x) for x in file_ids if x is not None]
        if not ids:
            return 0
        try:
            if not self._client.collection_exists(collection):
                return 0
        except Exception:  # noqa: BLE001
            pass
        try:
            self._client.delete(
                collection_name=collection,
                points_selector=qm.FilterSelector(
                    filter=qm.Filter(must=[qm.FieldCondition(
                        key="file_id", match=qm.MatchAny(any=ids))])),
                wait=True,
            )
        except Exception as exc:  # noqa: BLE001
            LOG.warning("批量删除向量失败 collection=%s n=%d：%s", collection, len(ids), exc)
            return 0
        return len(ids)

    def delete_ids(self, collection: str, point_ids: Sequence[int],
                   *, batch: int = 5000) -> int:
        """按点 ID 批量删除（比按 file_id 过滤快一个数量级）。

        Qdrant 的过滤删除要扫全部分类的 payload；点 ID 删除是索引定位。
        实测：1,272 文件用过滤删要 176 秒，改点 ID 后是秒级。
        """
        from qdrant_client import models as qm

        ids = [int(x) for x in point_ids]
        if not ids:
            return 0
        try:
            if not self._client.collection_exists(collection):
                return 0
        except Exception:  # noqa: BLE001
            pass
        n = 0
        for i in range(0, len(ids), batch):
            chunk = ids[i:i + batch]
            try:
                self._client.delete(
                    collection_name=collection,
                    points_selector=qm.PointIdsList(points=chunk),
                    wait=True)
                n += len(chunk)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("按 ID 删向量失败 collection=%s n=%d：%s",
                            collection, len(chunk), exc)
        return n

    def count(self, collection: str, *, file_id: Optional[int] = None) -> int:
        from qdrant_client import models as qm

        try:
            if not self._client.collection_exists(collection):
                return 0
        except Exception:  # noqa: BLE001
            if collection not in [c.name for c in self._client.get_collections().collections]:
                return 0
        cnt_filter = None
        if file_id is not None:
            cnt_filter = qm.Filter(must=[qm.FieldCondition(
                key="file_id", match=qm.MatchValue(value=int(file_id)))])
        res = self._client.count(collection_name=collection, count_filter=cnt_filter, exact=True)
        return int(res.count)

    def exists(self, collection: str) -> bool:
        try:
            return bool(self._client.collection_exists(collection))
        except Exception:  # noqa: BLE001 - 老版本客户端无 collection_exists
            try:
                return collection in [c.name for c in self._client.get_collections().collections]
            except Exception:  # noqa: BLE001
                return False

    def scroll_ids(self, collection: str, *, limit: int = 1_000_000) -> List[int]:
        """reconcile 用：把所有 point_id 拉回来做集合比对（分批 scroll）。

        collection 不存在时返回空列表，而不是让 Qdrant 抛 404。
        """
        if not self.exists(collection):
            return []
        ids: List[int] = []
        offset = None
        while True:
            points, offset = self._client.scroll(
                collection_name=collection,
                limit=10_000,
                offset=offset,
                with_payload=False,
                with_vectors=False,
            )
            ids.extend(int(p.id) for p in points)
            if offset is None or not points or len(ids) >= limit:
                break
        return ids

    def reconcile(self, collection: str, expected_point_ids: Optional[Iterable[int]] = None) -> Dict[str, Any]:
        """比对 sqlite 与 Qdrant point 数，输出差异报告（不自动删除）。"""
        exists = self.exists(collection)
        qdrant_ids = set(self.scroll_ids(collection))
        report: Dict[str, Any] = {
            "collection": collection,
            "collection_exists": exists,
            "qdrant_points": len(qdrant_ids),
        }
        if expected_point_ids is None:
            report["diff"] = None
            return report
        expected = set(int(i) for i in expected_point_ids)
        missing_in_qdrant = expected - qdrant_ids
        orphan_in_qdrant = qdrant_ids - expected
        report.update({
            "sqlite_expected": len(expected),
            "missing_in_qdrant": len(missing_in_qdrant),
            "orphan_in_qdrant": len(orphan_in_qdrant),
            "diff": len(missing_in_qdrant) + len(orphan_in_qdrant),
            "sample_missing_in_qdrant": [split_point_id(i) for i in sorted(missing_in_qdrant)[:20]],
            "sample_orphan_in_qdrant": [split_point_id(i) for i in sorted(orphan_in_qdrant)[:20]],
        })
        return report

    def collection_info(self, collection: str) -> Dict[str, Any]:
        try:
            info = self._client.get_collection(collection)
            return {
                "status": str(getattr(info, "status", "")),
                "points_count": int(getattr(info, "points_count", 0) or 0),
                "vectors_count": int(getattr(info, "vectors_count", 0) or 0),
                "dim": (info.config.params.vectors.size
                        if isinstance(info.config.params.vectors, object) else None),
            }
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}


def open_store(cfg: Config) -> VectorStore:
    """工厂：目前只有 Qdrant 后端。"""
    return QdrantVectorStore(cfg)


def l2_normalize(vec: Sequence[float]) -> List[float]:
    import numpy as np

    arr = np.asarray(vec, dtype="float32")
    n = float(np.linalg.norm(arr))
    return (arr / n).tolist() if n > 0 else arr.tolist()


def semantic_collection(cfg: Config) -> str:
    return str(cfg.get("semantic.collection", "materials_semantic"))


def face_collection(cfg: Config) -> str:
    return str(cfg.get("faces.collection", "materials_faces"))
