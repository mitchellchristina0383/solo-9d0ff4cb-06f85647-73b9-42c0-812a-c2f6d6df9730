"""FastAPI 排障接口。

POST /api/troubleshoot：
  * 底到顶上传若干未压缩 OCI 层 tar（multipart 字段 ``layers``，
    顺序即从最底层到最顶层）；
  * 提交待查容器内绝对路径 ``target_path``；
  * 提交 ``mount_points``（JSON 字符串数组）与 ``volume_entries``
    （JSON 对象：挂载点 -> 卷内条目清单）；
  * 分别返回「挂载前（纯镜像叠加视图）」与「挂载后（卷遮蔽挂载点）」的查找结果。

POST /api/layer-usage：
  * 同样的底到顶层上传；
  * 提交待查的容器内绝对目录 ``target_dir``（不必在当前镜像里存在）；
  * 按归档路径统计该目录下普通文件的写入 / 最终可见 / 被后层首次遮蔽的字节数。
"""

from __future__ import annotations

import json
from typing import Optional

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from .errors import InvalidInputError, TroubleshootError
from .imagefs import build_image_tree, normalize_absolute, validate_mount_points
from .resolver import ContainerView, ResolveResult, resolve
from .tarloader import LayerEvent, load_layer
from .usage import UsageReport, compute_usage
from .volume import VolumeTree, build_volume_tree

MAX_LAYER_BYTES = 128 * 1024 * 1024  # 单层 128 MiB
MAX_TOTAL_BYTES = 384 * 1024 * 1024  # 全部层合计 384 MiB

app = FastAPI(
    title="OCI 层叠加与卷挂载排障接口",
    description=(
        "按底到顶上传未压缩 OCI 层 tar，模拟 whiteout / 不透明目录叠加、"
        "卷挂载遮蔽、软链接（可跨挂载点，成环报错）解析，"
        "对比挂载前后的路径查找结果；并提供按目录统计各层占用 / 遮蔽字节数的"
        "镜像体积诊断。"
    ),
    version="1.1.0",
)


# ---------------------------------------------------------------------------
# 响应模型（仅用于 OpenAPI /docs 展示与输出规整）
# ---------------------------------------------------------------------------

class SourceInfo(BaseModel):
    origin: str = Field(description="来源类别：layer=镜像层，volume=挂载卷，image=镜像根")
    layer_index: Optional[int] = Field(default=None, description="镜像层索引，0 为最底层")
    mountpoint: Optional[str] = Field(default=None, description="卷来源时的挂载点")
    detail: Optional[str] = None


class SymlinkHopModel(BaseModel):
    symlink_path: str = Field(description="被跟随的软链接容器绝对路径")
    target: str = Field(description="软链接原始目标（链接里写的内容）")
    resolved_from: str = Field(description="跟随该链接时所在的目录")


class LookupOutcome(BaseModel):
    status: str = Field(description="found=找到；missing=路径中断；error=解析错误（如成环）")
    query_path: str
    actual_path: Optional[str] = Field(default=None, description="解析后的实际容器绝对路径")
    entry_type: Optional[str] = Field(default=None, description="file/dir/symlink")
    source: Optional[SourceInfo] = Field(default=None, description="来源层或卷")
    broken_segment: Optional[str] = Field(default=None, description="中断在哪个路径分量上")
    broken_at: Optional[str] = Field(default=None, description="中断处的绝对路径")
    reason: Optional[str] = Field(default=None, description="中断或报错的原因说明")
    error_code: Optional[str] = Field(default=None, description="symlink_cycle / too_many_symlink_hops")
    shadowed_by_mount: Optional[str] = Field(
        default=None, description="中断/命中位置所在的挂载点（即镜像内容被卷遮蔽处）"
    )
    symlink_hops: list[SymlinkHopModel] = Field(default_factory=list)


class TroubleshootResponse(BaseModel):
    target_path: str
    layer_count: int
    mount_points: list[str]
    before_mount: LookupOutcome = Field(description="挂载前：仅镜像层叠加的查找结果")
    after_mount: LookupOutcome = Field(description="挂载后：挂载点被卷遮蔽后的查找结果")


class LayerShadowEntry(BaseModel):
    layer_index: int = Field(description="首次遮蔽该层写入的后层索引（越大越靠上）")
    layer_name: str = Field(description="遮蔽层上传时的文件名")
    bytes: int = Field(description="被该后层遮蔽的字节数")


class LayerUsageEntry(BaseModel):
    layer_index: int = Field(description="写入层索引，0 为最底层")
    layer_name: str = Field(description="写入层上传时的文件名")
    written_bytes: int = Field(description="该层在目标目录下写入的普通文件总字节数")
    visible_bytes: int = Field(description="其中最终仍可见的字节数（扣除被后层遮蔽）")
    shadowed_bytes: int = Field(description="其中被后续层首次遮蔽掉的字节数")
    shadowed_by: dict[str, int] = Field(
        default_factory=dict,
        description="后层索引（字符串键）-> 被该后层首次遮蔽的字节数；只记首次遮蔽",
    )
    shadowed_by_layers: list[LayerShadowEntry] = Field(
        default_factory=list,
        description="与 shadowed_by 相同归因的明细列表（含遮蔽层文件名）",
    )


class LayerUsageResponse(BaseModel):
    target_dir: str = Field(description="统计的容器内绝对目录（规范化后）")
    layer_count: int
    layers: list[LayerUsageEntry] = Field(
        description="按底到顶每个写入层的统计；没有写入的层也会列出（各项为 0）"
    )
    total_written_bytes: int = Field(description="所有层写入合计")
    total_visible_bytes: int = Field(description="最终可见合计")
    total_shadowed_bytes: int = Field(description="被后层遮蔽合计")


@app.exception_handler(TroubleshootError)
async def _troubleshoot_error_handler(_: Request, exc: TroubleshootError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"error": exc.message})


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["运维"])
async def health() -> dict:
    return {"status": "ok"}


def _result_to_dict(result: ResolveResult) -> dict:
    return {
        "status": result.status,
        "query_path": result.query_path,
        "actual_path": result.actual_path,
        "entry_type": result.entry_type,
        "source": result.source,
        "broken_segment": result.broken_segment,
        "broken_at": result.broken_at,
        "reason": result.reason,
        "error_code": result.error_code,
        "shadowed_by_mount": result.shadowed_by_mount,
        "symlink_hops": [
            {
                "symlink_path": hop.symlink_path,
                "target": hop.target,
                "resolved_from": hop.resolved_from,
            }
            for hop in result.symlink_hops
        ],
    }


async def _read_layers(layers: list[UploadFile]) -> tuple[list[list[LayerEvent]], list[str]]:
    """读入并逐层解析 tar（保留上传顺序：底 -> 顶），返回事件与显示名。"""

    total_bytes = 0
    layer_events: list[list[LayerEvent]] = []
    layer_names: list[str] = []
    for index, upload in enumerate(layers):
        data = await upload.read()
        total_bytes += len(data)
        if len(data) > MAX_LAYER_BYTES:
            raise InvalidInputError(f"第 {index} 层超过 128MiB 单层上限")
        if total_bytes > MAX_TOTAL_BYTES:
            raise InvalidInputError("全部层合计超过 384MiB 上限")
        name = upload.filename or f"layer[{index}]"
        layer_events.append(load_layer(data, index, name))
        layer_names.append(name)
    return layer_events, layer_names


@app.post(
    "/api/troubleshoot",
    response_model=TroubleshootResponse,
    tags=["排障"],
    summary="上传 OCI 层与挂载清单，对比挂载前后的路径查找结果",
)
async def troubleshoot(
    layers: list[UploadFile] = File(
        description=(
            "未压缩 OCI 层 tar 文件，multipart 顺序必须是从最底层到最顶层；"
            "接口只读取 tar 条目元数据，不提取任何文件内容。"
        )
    ),
    target_path: str = Form(description="待查的容器内绝对路径，如 /etc/app/config.yaml"),
    mount_points: str = Form(
        default="[]",
        description="JSON 字符串数组：挂载点绝对路径，必须是镜像里的目录且互不嵌套",
    ),
    volume_entries: str = Form(
        default="{}",
        description=(
            "JSON 对象：键为挂载点绝对路径，值为卷内条目清单 "
            '[{"path": "相对卷根的路径", "type": "file|dir"}]；卷只含文件和目录'
        ),
    ),
) -> TroubleshootResponse:
    # 1) 读入并逐层解析 tar（保留上传顺序：底 -> 顶）
    if not layers:
        raise InvalidInputError("至少需要上传一个 OCI 层 tar（layers 字段）")

    layer_events, layer_names = await _read_layers(layers)

    # 2) 合并镜像层
    tree = build_image_tree(layer_events)

    # 3) 校验目标路径
    target_parts = normalize_absolute(target_path, "target_path")

    # 4) 解析挂载点 JSON 并校验（镜像里存在、是目录、互不嵌套）
    try:
        raw_mounts = json.loads(mount_points)
    except json.JSONDecodeError as exc:
        raise InvalidInputError(f"mount_points 不是合法 JSON: {exc}") from exc
    if not isinstance(raw_mounts, list) or not all(isinstance(x, str) for x in raw_mounts):
        raise InvalidInputError("mount_points 必须是字符串数组（绝对路径）")
    mount_parts_list = validate_mount_points(raw_mounts, tree)

    # 5) 解析卷清单并逐挂载点构建卷视图
    try:
        raw_volumes = json.loads(volume_entries)
    except json.JSONDecodeError as exc:
        raise InvalidInputError(f"volume_entries 不是合法 JSON: {exc}") from exc
    if not isinstance(raw_volumes, dict):
        raise InvalidInputError("volume_entries 必须是 JSON 对象：挂载点 -> 条目清单")

    # 把用户给的卷清单键统一规范化为分量形式，避免 /a、/a/ 写法差异。
    normalized_volumes: dict[tuple[str, ...], list] = {}
    for raw_key, entries in raw_volumes.items():
        key_parts = normalize_absolute(raw_key, "volume_entries 的键")
        if key_parts in normalized_volumes:
            raise InvalidInputError(f"volume_entries 中挂载点 {raw_key} 重复")
        if not isinstance(entries, list):
            raise InvalidInputError(f"挂载点 {raw_key} 的卷清单必须是数组")
        normalized_volumes[key_parts] = entries

    mounts: dict[tuple[str, ...], VolumeTree] = {}
    for mount_parts in mount_parts_list:
        if mount_parts not in normalized_volumes:
            raise InvalidInputError(
                f"挂载点 {_join_parts(mount_parts)} 缺少对应的 volume_entries 清单（可为空数组）"
            )
        mounts[mount_parts] = build_volume_tree(
            normalized_volumes[mount_parts], f"挂载点 {_join_parts(mount_parts)} 的卷"
        )

    extra = [k for k in normalized_volumes if k not in mount_parts_list]
    if extra:
        raise InvalidInputError(
            "volume_entries 出现了未在 mount_points 中声明的挂载点: "
            + ", ".join(_join_parts(p) for p in extra)
        )

    # 6) 挂载前 / 挂载后分别解析
    before_view = ContainerView(tree, mounts={})
    after_view = ContainerView(tree, mounts=mounts)

    before = resolve(before_view, target_parts)
    after = resolve(after_view, target_parts)

    return TroubleshootResponse(
        target_path=_join_parts(target_parts),
        layer_count=len(layers),
        mount_points=[_join_parts(p) for p in mount_parts_list],
        before_mount=LookupOutcome(**_result_to_dict(before)),
        after_mount=LookupOutcome(**_result_to_dict(after)),
    )


def _join_parts(parts: tuple[str, ...]) -> str:
    return "/" + "/".join(parts) if parts else "/"


@app.post(
    "/api/layer-usage",
    response_model=LayerUsageResponse,
    tags=["占用诊断"],
    summary="按目录统计各层写入 / 最终可见 / 被后层首次遮蔽的字节数",
)
async def layer_usage(
    layers: list[UploadFile] = File(
        description=(
            "未压缩 OCI 层 tar 文件，multipart 顺序必须是从最底层到最顶层；"
            "接口只读取 tar 条目元数据（归档路径、类型、普通文件 size），"
            "不提取任何文件内容。"
        )
    ),
    target_dir: Optional[str] = Form(
        default=None,
        description=(
            "要统计的容器内绝对目录，如 /opt/app 或 /；按归档路径前缀匹配，"
            "目录本身不必在当前镜像里存在（已经删掉的历史目录同样可以统计）"
        ),
    ),
    target_path: Optional[str] = Form(
        default=None,
        description="target_dir 的等价别名（与既有排障接口的字段命名保持一致），二选一",
    ),
) -> LayerUsageResponse:
    if not layers:
        raise InvalidInputError("至少需要上传一个 OCI 层 tar（layers 字段）")

    if target_dir and target_path and target_dir != target_path:
        raise InvalidInputError("target_dir 与 target_path 同时提交且不一致，请只提交一个")
    raw_target = target_dir or target_path
    if raw_target is None or not raw_target.strip():
        raise InvalidInputError("必须提交要统计的绝对目录（target_dir）")

    target_parts = normalize_absolute(raw_target, "target_dir")

    layer_events, layer_names = await _read_layers(layers)
    report = compute_usage(layer_events, target_parts, layer_names)
    return _usage_to_response(report)


def _usage_to_response(report: UsageReport) -> LayerUsageResponse:
    return LayerUsageResponse(
        target_dir=_join_parts(report.target_parts),
        layer_count=len(report.layers),
        layers=[
            LayerUsageEntry(
                layer_index=item.layer_index,
                layer_name=item.layer_name,
                written_bytes=item.written_bytes,
                visible_bytes=item.visible_bytes,
                shadowed_bytes=item.shadowed_bytes,
                shadowed_by={str(shadower): amount for shadower, amount in sorted(item.shadowed_by.items())},
                shadowed_by_layers=[
                    LayerShadowEntry(
                        layer_index=shadower,
                        # 遮蔽层一定存在（索引来自同一个 layers 列表）。
                        layer_name=report.layers[shadower].layer_name,
                        bytes=amount,
                    )
                    for shadower, amount in sorted(item.shadowed_by.items())
                ],
            )
            for item in report.layers
        ],
        total_written_bytes=report.total_written_bytes,
        total_visible_bytes=report.total_visible_bytes,
        total_shadowed_bytes=report.total_shadowed_bytes,
    )
