"""素材人脸检索系统 · 内部模块包。

分层（对应规格的架构总览）：
    common          公共基础设施（配置/日志/CLI/子进程/point_id）
    db              SQLite WAL + 迁移 + 两阶段状态机
    vector_store    向量存储抽象 + Qdrant 实现
    manifest        L1 清单扫描
    exif            时间来源解析（EXIF → QuickTime → 文件名 → mtime）
    extract         L2b 镜头切分抽帧
    encode_model    Qwen3-VL-Embedding-2B 加载与编码
    encode          L2c 编码 + L3 入库
    query_rewrite   7.4 文字检索轻量改写
    face_search     查询侧 SCRFD/ArcFace（精确找人）
    search          三路路由 + adaptive_cut + RRF + L4
    ui_wall         Tab1 照片墙（keyset 分页）
    export          TSV / 结构化 TXT 导出
    static_serve    本地静态服务（白名单 + 防穿越）
"""

__all__ = [
    "common", "db", "vector_store", "manifest", "exif", "extract",
    "encode_model", "encode", "query_rewrite", "face_search", "search",
    "ui_wall", "export", "static_serve",
]
