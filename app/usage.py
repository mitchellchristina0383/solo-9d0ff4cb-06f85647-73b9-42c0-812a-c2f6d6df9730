"""按层统计目标目录下普通文件的占用 / 遮蔽关系。

背景
----
容器镜像按层叠加，构建阶段在**后层**删除（whiteout / 不透明目录标记）或
替换掉的文件，并不会从**前层**的 tar 里消失——前一层的那些字节依然计入
镜像体积。本模块只读取每层 tar 的条目元数据（归档路径、类型、普通文件
size），回答两个问题：

1. 每个写入层在目标目录下写了多少字节、最终还能看见多少；
2. 这些字节分别被哪个**后层首次**遮蔽（同一份写入只归因一次）。

统计口径
--------
* 目标目录按**归档路径前缀**匹配，只统计严格位于其下的普通文件；
  目录本身是否存在、是否已被删除都不影响查询（历史层仍可回顾）。
* 不做软链接跟随：``/a`` 是软链接时，``/a/f`` 仍按归档路径归在 ``/a`` 下。
* 只使用 tar 元数据里的 ``size``，不读取、不提取文件内容。
* 同一路径在后层重建算该层的**新写入**；被覆盖的旧版本仍记在原写入层，
  并在覆盖层计为「首次遮蔽」。因此每一份写入最终要么可见，要么恰好被
  一个后层遮蔽一次。
* 后层把路径 P 换成普通文件或软链接时，P 下旧子树的普通文件都由该层
  首次遮蔽（目录被“截断”）。
* whiteout 遮蔽 P 及其整个旧子树；不透明目录标记遮蔽目录内更下层的
  全部内容，目录自身与同层新增不受影响。
* 每层采用两阶段处理：先应用本层所有「遮蔽下层」的标记，再写入本层
  新增条目。因此 whiteout / opaque 与同层新增在 tar 中的先后顺序不会
  改变归因。

复杂度
------
每层需要扫描一遍 tar 事件（随读入条目数增长）；遮蔽操作只遍历真正被
移除的活跃文件，每个文件一生只会被移除一次，因此总工作量随
「读入条目数 + 受影响条目数」线性增长，而不是每层全表扫描。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .tarloader import LayerEvent, NodeEvent, OpaqueEvent, WhiteoutEvent

_REL_ALL = "all"  # 事件路径是目标目录本身或其祖先：影响目标目录内全部文件
_REL_BELOW = "below"  # 事件路径严格位于目标目录之下：只影响该子树
RELATION_NONE = "none"


def _relation(event_parts: tuple[str, ...], target_parts: tuple[str, ...]) -> str:
    """判断事件路径与目标目录的关系（按分量比较，避免 /app 误匹配 /app2）。"""

    common = min(len(event_parts), len(target_parts))
    if event_parts[:common] != target_parts[:common]:
        return RELATION_NONE
    if len(event_parts) <= len(target_parts):
        # 包含相等：事件路径等于目标目录时，目录被整体替换 / 删除，
        # 其下的历史文件都受影响。
        return _REL_ALL
    return _REL_BELOW


@dataclass
class LayerUsage:
    """单层在目标目录下的统计结果。"""

    layer_index: int
    layer_name: str
    written_bytes: int = 0
    visible_bytes: int = 0
    # 后层索引 -> 该后层首次遮蔽掉的字节数（被本层写入、后来被遮蔽的版本）
    shadowed_by: dict[int, int] = field(default_factory=dict)

    @property
    def shadowed_bytes(self) -> int:
        return sum(self.shadowed_by.values())


@dataclass
class UsageReport:
    target_parts: tuple[str, ...]
    layers: list[LayerUsage]
    total_written_bytes: int
    total_visible_bytes: int
    total_shadowed_bytes: int


class _TrieNode:
    """归档路径前缀树；只在目标目录下挂载“活跃普通文件”。"""

    __slots__ = ("children", "file")

    def __init__(self) -> None:
        self.children: dict[str, "_TrieNode"] = {}
        # (size, writer_layer_index)；目录节点没有 file。
        self.file: tuple[int, int] | None = None


class _FileTrie:
    """目标目录下的活跃文件索引，支持按子树 / 精确路径摘除。

    每次摘除都会把整棵子树从父节点上断开，空的祖先链一并回收；因此一个
    文件被摘除后不会再被后续事件重复遍历。
    """

    def __init__(self, stats: list[LayerUsage]) -> None:
        self._root = _TrieNode()
        self._stats = stats

    # -- 写入 ---------------------------------------------------------------
    def add(self, parts: tuple[str, ...], size: int, writer: int) -> None:
        node = self._root
        for comp in parts:
            child = node.children.get(comp)
            if child is None:
                child = _TrieNode()
                node.children[comp] = child
            node = child
        node.file = (size, writer)

    # -- 遮蔽 ---------------------------------------------------------------
    def _record_subtree(self, node: _TrieNode, shadower: int) -> int:
        """遍历 node 子树，把其中所有活跃文件记为被 shadower 首次遮蔽。

        遍历时顺便拆掉子节点，保证被遮蔽的条目不会被后续标记重复遍历。
        """

        removed = 0
        stack = [node]
        while stack:
            cur = stack.pop()
            if cur.file is not None:
                size, writer = cur.file
                cur.file = None
                self._stats[writer].shadowed_by[shadower] = (
                    self._stats[writer].shadowed_by.get(shadower, 0) + size
                )
                removed += 1
            stack.extend(cur.children.values())
            cur.children.clear()
        return removed

    @staticmethod
    def _prune_chain(chain: list[tuple[_TrieNode, str, _TrieNode]]) -> None:
        """从叶子向根回收变空的节点，避免多层操作后累积空壳。"""

        for parent, name, child in reversed(chain):
            if child.file is None and not child.children:
                if parent.children.get(name) is child:
                    del parent.children[name]
            else:
                break

    def _walk(self, parts: tuple[str, ...]):
        """按路径分量走到节点，返回 (祖先链, 节点)；节点不存在返回 (None, None)。"""

        chain: list[tuple[_TrieNode, str, _TrieNode]] = []
        node = self._root
        for comp in parts:
            child = node.children.get(comp)
            if child is None:
                return None, None
            chain.append((node, comp, child))
            node = child
        return chain, node

    def shadow_subtree(self, parts: tuple[str, ...], shadower: int) -> None:
        """遮蔽 parts 本身及其全部后代。"""

        if not parts:
            self.shadow_all(shadower)
            return

        chain, node = self._walk(parts)
        if node is None:
            return
        self._record_subtree(node, shadower)
        self._prune_chain(chain)

    def shadow_all(self, shadower: int) -> None:
        """遮蔽目标目录下的全部活跃文件（事件发生在目标目录本身或更上层）。"""

        for child in self._root.children.values():
            self._record_subtree(child, shadower)
        self._root.children.clear()

    def shadow_strict_descendants(self, parts: tuple[str, ...], shadower: int) -> None:
        """只遮蔽 parts 目录内的后代，parts 自身的文件（异常归档）保留。"""

        if not parts:
            self.shadow_all(shadower)
            return

        chain, node = self._walk(parts)
        if node is None:
            return
        for child in node.children.values():
            self._record_subtree(child, shadower)
        node.children.clear()
        # 目录自身没有文件且不再有子节点时，祖先链也可能变空。
        if node.file is None:
            self._prune_chain(chain)

    def shadow_exact_file(self, parts: tuple[str, ...], shadower: int) -> None:
        """只遮蔽 parts 自身上的普通文件（目录项替换下层文件），不动其后代。"""

        if not parts:
            return

        chain, node = self._walk(parts)
        if node is None or node.file is None:
            return

        size, writer = node.file
        node.file = None
        self._stats[writer].shadowed_by[shadower] = (
            self._stats[writer].shadowed_by.get(shadower, 0) + size
        )
        self._prune_chain(chain)

    # -- 收尾 ---------------------------------------------------------------
    def finish_visible(self) -> None:
        """把仍活跃的文件计入其写入层的最终可见字节数。"""

        stack = [self._root]
        while stack:
            cur = stack.pop()
            if cur.file is not None:
                size, writer = cur.file
                self._stats[writer].visible_bytes += size
                cur.file = None
            stack.extend(cur.children.values())


def compute_usage(
    layers: list[list[LayerEvent]],
    target_parts: tuple[str, ...],
    layer_names: list[str] | None = None,
) -> UsageReport:
    """按底到顶统计各层在 target_parts 下的写入 / 可见 / 遮蔽字节数。

    ``layers`` 索引 0 为最底层；``layer_names`` 与之一一对应，仅用于回显。
    """

    names = layer_names or [f"layer[{i}]" for i in range(len(layers))]
    if len(names) != len(layers):
        raise ValueError("layer_names 与 layers 数量不一致")

    stats = [
        LayerUsage(layer_index=index, layer_name=names[index])
        for index in range(len(layers))
    ]
    trie = _FileTrie(stats)

    for layer_index, events in enumerate(layers):
        # 第一阶段：所有“遮蔽更下层”的动作。只作用于此前层遗留的活跃文件，
        # 因此本层新增条目永远不会被本层自己的标记遮蔽，且与 tar 内顺序无关。
        for event in events:
            if isinstance(event, OpaqueEvent):
                relation = _relation(event.dir_parts, target_parts)
                if relation == _REL_ALL:
                    trie.shadow_all(layer_index)
                elif relation == _REL_BELOW:
                    trie.shadow_strict_descendants(event.dir_parts, layer_index)
                continue

            if isinstance(event, WhiteoutEvent):
                path = event.parent_parts + (event.name,)
                relation = _relation(path, target_parts)
                if relation == _REL_ALL:
                    trie.shadow_all(layer_index)
                elif relation == _REL_BELOW:
                    trie.shadow_subtree(path, layer_index)
                continue

            if not isinstance(event, NodeEvent):
                continue

            relation = _relation(event.parts, target_parts)
            if event.entry_type == "dir":
                # 目录替换下层同路径的普通文件；目录内的下层内容保留
                # （要遮蔽内容需要 whiteout / opaque）。
                if relation == _REL_BELOW:
                    trie.shadow_exact_file(event.parts, layer_index)
            else:
                # 普通文件 / 软链接替换同名路径：旧子树整体失效。
                if relation == _REL_ALL:
                    trie.shadow_all(layer_index)
                elif relation == _REL_BELOW:
                    trie.shadow_subtree(event.parts, layer_index)

        # 第二阶段：写入本层新增的普通文件（只有普通文件有可统计的字节）。
        for event in events:
            if not isinstance(event, NodeEvent) or event.entry_type != "file":
                continue
            if _relation(event.parts, target_parts) != _REL_BELOW:
                continue
            size = max(0, event.size)
            stats[layer_index].written_bytes += size
            trie.add(event.parts, size, layer_index)

    trie.finish_visible()

    return UsageReport(
        target_parts=target_parts,
        layers=stats,
        total_written_bytes=sum(item.written_bytes for item in stats),
        total_visible_bytes=sum(item.visible_bytes for item in stats),
        total_shadowed_bytes=sum(item.shadowed_bytes for item in stats),
    )
