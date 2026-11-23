# OCI 层叠加与卷挂载排障接口

容器启动时报「找不到配置文件」时，值班员需要判断问题出在**镜像构建**（层叠加、
whiteout、软链接）还是**卷挂载**（挂载点把镜像内容盖住）。本服务用 FastAPI
模拟这两套机制，对同一个待查路径分别给出**挂载前**与**挂载后**的解析结果。

另外提供**层占用诊断**：构建时删掉的临时文件仍留在旧层 tar 里、继续占镜像
体积。按目录统计每个写入层写了多少、最终可见多少、被哪个后层首次遮蔽多少。

## 能力范围

- 按「底到顶」顺序上传多个**未压缩** OCI 层 tar，只读取 tar 条目元数据，
  不落盘、不提取文件内容；
- 镜像层条目支持：普通文件、目录、软链接、whiteout（`.wh.<name>`）、
  不透明目录标记（`.wh..wh..opq`）；
- 叠加语义：whiteout 只遮蔽更下层，同层新增同路径条目照常可见（两阶段处理，
  与 tar 内顺序无关）；不透明标记遮蔽目录的下层内容，目录本身与上层新增保留；
- 挂载点必须是镜像里存在的目录，且多个挂载点**互不嵌套**；
- 卷只含文件和目录；挂载后挂载点目录内的镜像内容被整体盖住；
- 容器内逐分量解析软链接，支持相对/绝对目标、**跨挂载点**跳转；
  与 Linux `stat(2)` 语义一致：软链接作为路径末分量时返回链接本身
  （`entry_type=symlink`），只有链接后还有子分量时才跟随；
  跟随过程成环返回 `symlink_cycle`，跳数超 40 返回 `too_many_symlink_hops`；
- 查找命中时返回实际路径、类型、来源层（layer_index，0 为最底层）或来源卷；
  中断时返回断在哪一分量、绝对位置与具体原因（含遮蔽来源挂载点）；
- 占用诊断按归档路径前缀统计目标目录下的普通文件，不跟随软链接，目录本身
  当前是否存在不影响统计（已被删除的历史目录仍可回顾）。

## 目录结构

```
app/
  errors.py      # 统一业务异常（400 tar 不可读 / 422 输入不合法）
  tarloader.py   # 只读解析未压缩层 tar -> 有序事件（含普通文件 size）
  imagefs.py     # 底到顶层合并（whiteout / opaque / 覆盖）+ 挂载点校验
  volume.py      # 卷清单解析（仅 file/dir，父目录必须显式声明）
  resolver.py    # 容器视图、软链接跟随、跨挂载、成环检测
  usage.py       # 按目录统计各层写入 / 可见 / 被后层首次遮蔽字节数
  main.py        # FastAPI 路由与响应模型
tests/           # 63 个 pytest 用例（排障 33 + 占用诊断 30）
scripts/
  e2e_smoke.py   # 端到端冒烟脚本（造层 -> 打接口 -> 断言）
Dockerfile
docker-compose.yml
```

## 用 Docker Compose 启动

```bash
docker compose up --build
# 打开交互文档
open http://localhost:8000/docs      # Linux: xdg-open http://localhost:8000/docs
```

`/docs` 里可直接试用 `POST /api/troubleshoot`（排障查找）与
`POST /api/layer-usage`（层占用诊断）。前者并排展示 `before_mount` 与
`after_mount` 两组结果，后者按层列出目标目录下的字节统计。

## 本地开发（无 Docker 时）

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload
.venv/bin/python -I -m pytest tests/ -q          # 单元/接口测试
.venv/bin/python -I scripts/e2e_smoke.py         # 起服务后的端到端冒烟
```

## 请求格式（multipart/form-data）

### `POST /api/troubleshoot`

| 字段 | 形式 | 说明 |
| --- | --- | --- |
| `layers` | 文件，可重复 | 未压缩 tar，重复字段的顺序 = 最底层 → 最顶层 |
| `target_path` | 字符串 | 待查容器内绝对路径，如 `/etc/app/config.yaml` |
| `mount_points` | JSON 字符串 | 挂载点数组，如 `["/etc/app","/var/lib/app"]` |
| `volume_entries` | JSON 字符串 | 挂载点 → 卷条目清单，路径相对卷根，仅 `file`/`dir` |

### `POST /api/layer-usage`

| 字段 | 形式 | 说明 |
| --- | --- | --- |
| `layers` | 文件，可重复 | 同上，顺序 = 最底层 → 最顶层 |
| `target_dir` | 字符串 | 要统计的容器内绝对目录，如 `/opt/app`；按归档路径前缀匹配，目录不必在当前镜像里存在 |
| `target_path` | 字符串 | `target_dir` 的等价别名，二选一 |

`volume_entries` 示例：

```json
{
  "/etc/app": [
    {"path": "mounted.yaml", "type": "file"},
    {"path": "sub", "type": "dir"},
    {"path": "sub/v", "type": "file"}
  ],
  "/var/lib/app": []
}
```

## 响应解读

命中：

```json
{
  "status": "found",
  "actual_path": "/etc/app/mounted.yaml",
  "entry_type": "file",
  "source": {"origin": "volume", "mountpoint": "/etc/app"},
  "shadowed_by_mount": "/etc/app",
  "symlink_hops": [
    {"symlink_path": "/appcfg", "target": "/etc/app/mounted.yaml", "resolved_from": "/"}
  ]
}
```

中断（注意 `broken_segment` / `broken_at` / `reason` 三段式定位，
以及 `shadowed_by_mount` 提示镜像内容被卷盖住）：

```json
{
  "status": "missing",
  "broken_segment": "config.yaml",
  "broken_at": "/etc/app/config.yaml",
  "reason": "卷 /etc/app 的条目清单中不存在 /config.yaml",
  "shadowed_by_mount": "/etc/app"
}
```

成环：

```json
{"status": "error", "error_code": "symlink_cycle",
 "reason": "软链接解析在 /cycle1 处成环（ELOOP）"}
```

## 占用诊断响应解读

```json
{
  "target_dir": "/app",
  "layer_count": 3,
  "layers": [
    {
      "layer_index": 0, "layer_name": "layer0.tar",
      "written_bytes": 150, "visible_bytes": 0, "shadowed_bytes": 150,
      "shadowed_by": {"1": 50, "2": 100},
      "shadowed_by_layers": [
        {"layer_index": 1, "layer_name": "layer1.tar", "bytes": 50},
        {"layer_index": 2, "layer_name": "layer2.tar", "bytes": 100}
      ]
    }
  ],
  "total_written_bytes": 192,
  "total_visible_bytes": 42,
  "total_shadowed_bytes": 150
}
```

- `written_bytes`：该层在目标目录下写入的普通文件字节数（tar 元数据 `size`）；
- `visible_bytes`：其中最终仍可见的部分；
- `shadowed_by`：后层索引（字符串键）→ 被该后层首次遮蔽的字节数；
  `shadowed_by_layers` 是同一归因的明细形式（带遮蔽层文件名）；
- 每份写入恰好被遮蔽一次，不会重复计数到更后的层；
- 恒等式：`written_bytes = visible_bytes + shadowed_bytes`。

归因规则：

- 同路径在后层重建算该层**新写入**，旧版本仍记在原层，并在覆盖层计为首次遮蔽；
- 后层把目录换成普通文件或软链接时，旧子树中的文件整体计入该层遮蔽；
- whiteout 遮蔽目标及其旧子树；不透明标记遮蔽目录内更下层内容，目录本身
  与同层新增保留；
- 每层先处理“遮蔽下层”的标记、再写入本层新增，因此标记与新增在 tar 内的
  先后顺序不改变归因；
- 目标目录按归档路径分量前缀匹配（`/app` 不会误匹配 `/app2`），不跟随软链接。

## 排障判断指引

- 挂载前 missing、挂载后也 missing：问题在镜像构建（路径不存在 / 被 whiteout /
  不透明目录遮蔽 / 软链接断链），`reason` 会指出具体机制与层号；
- 挂载前 found、挂载后 missing 且带 `shadowed_by_mount`：问题在卷挂载，
  镜像里的文件被卷目录盖住；
- `status=error` + `symlink_cycle`：镜像里的软链接本身成环，与卷无关。

## 约束与限制

- 仅接受未压缩 tar；gzip/xz/zstd/bzip2 流会被魔数识别并明确拒绝；
- 不支持 hardlink、设备节点、FIFO 等条目（返回 422）；whiteout / 不透明
  标记按 OCI 惯例接受任意条目类型，仅以 `.wh.` 名称识别；
- 卷内不允许软链接；卷清单的每个父目录必须显式声明；
- 单层上限 128 MiB、总计 384 MiB、单层 5 万条目（防御性阈值）；
- 同层 tar 内出现重复路径条目会被拒绝（层间覆盖才是合法叠加方式）；
- 占用诊断只统计 `tar` 条目里的逻辑 `size`（未压缩字节），不读取内容、
  不计算块对齐或压缩后体积；目录与软链接本身占 0 字节；
- 占用诊断是**归档路径**口径：只要目标目录路径前缀下出现过该普通文件条目
  就计入，不模拟“祖先路径已变成普通文件”导致的不可达（这类不可达不在
  whiteout / 不透明标记的遮蔽规则内）；
- 层 tar 无法解析时接口只返回原因（400/422 + `error` 字段），不输出统计结果。
