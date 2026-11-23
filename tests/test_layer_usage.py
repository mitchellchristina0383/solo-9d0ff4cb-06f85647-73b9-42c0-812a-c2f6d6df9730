"""层占用诊断接口（POST /api/layer-usage）的测试。

tar 在内存里现造；普通文件的 content 长度即 tar 元数据里的 size，
接口只读元数据中的 size，不读取内容。
"""

from __future__ import annotations

import io
import tarfile

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def make_tar(members: list[tuple]) -> bytes:
    """members: (name, kind, content?)，kind ∈ file/dir/symlink/whiteout/opaque。"""

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:") as tf:
        for spec in members:
            name, kind = spec[0], spec[1]
            if kind == "dir":
                ti = tarfile.TarInfo(name)
                ti.type = tarfile.DIRTYPE
                ti.mode = 0o755
            elif kind == "file":
                payload = (spec[2] if len(spec) > 2 else "data").encode()
                ti = tarfile.TarInfo(name)
                ti.type = tarfile.REGTYPE
                ti.size = len(payload)
                tf.addfile(ti, io.BytesIO(payload))
                continue
            elif kind == "symlink":
                ti = tarfile.TarInfo(name)
                ti.type = tarfile.SYMTYPE
                ti.linkname = spec[2]
            elif kind in ("whiteout", "opaque"):
                ti = tarfile.TarInfo(name)
                ti.type = tarfile.CHRTYPE
                ti.devmajor = 0
                ti.devminor = 0
            else:
                raise AssertionError(kind)
            tf.addfile(ti)
    return buf.getvalue()


def usage(layers, target_dir):
    files = [
        ("layers", (f"layer{i}.tar", data, "application/x-tar"))
        for i, data in enumerate(layers)
    ]
    return client.post(
        "/api/layer-usage", files=files, data={"target_dir": target_dir}
    )


def layer_map(body: dict) -> dict[int, dict]:
    return {item["layer_index"]: item for item in body["layers"]}


def body_of(layers, target_dir) -> dict:
    r = usage(layers, target_dir)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# 基础：写入、可见、按归档路径前缀统计
# ---------------------------------------------------------------------------

def test_basic_written_and_visible():
    l0 = make_tar([
        ("app", "dir"),
        ("app/a.bin", "file", "x" * 100),
        ("app/sub", "dir"),
        ("app/sub/b.bin", "file", "y" * 50),
        ("other", "dir"),
        ("other/c.bin", "file", "z" * 999),
    ])
    body = body_of([l0], "/app")
    assert body["target_dir"] == "/app"
    assert body["layer_count"] == 1
    assert len(body["layers"]) == 1
    only = body["layers"][0]
    assert only["written_bytes"] == 150
    assert only["visible_bytes"] == 150
    assert only["shadowed_bytes"] == 0
    assert only["shadowed_by"] == {}
    assert only["shadowed_by_layers"] == []
    assert body["total_written_bytes"] == 150
    assert body["total_visible_bytes"] == 150
    assert body["total_shadowed_bytes"] == 0


def test_target_is_prefix_by_component_not_string():
    l0 = make_tar([
        ("app", "dir"),
        ("app/a", "file", "x" * 10),
        ("app2", "dir"),
        ("app2/b", "file", "y" * 20),
    ])
    body = body_of([l0], "/app")
    assert body["total_written_bytes"] == 10


def test_root_target_counts_everything():
    l0 = make_tar([
        ("a", "file", "x" * 10),
        ("d", "dir"),
        ("d/b", "file", "y" * 20),
    ])
    body = body_of([l0], "/")
    assert body["target_dir"] == "/"
    assert body["total_written_bytes"] == 30


def test_missing_directory_still_reports_history():
    """目标目录在当前镜像里不存在（被删除）也能统计历史层。"""

    l0 = make_tar([
        ("tmp", "dir"),
        ("tmp/build", "dir"),
        ("tmp/build/obj.o", "file", "x" * 400),
    ])
    l1 = make_tar([(".wh.tmp", "whiteout")])
    body = body_of([l0, l1], "/tmp/build")
    layers = layer_map(body)
    assert layers[0]["written_bytes"] == 400
    assert layers[0]["visible_bytes"] == 0
    assert layers[0]["shadowed_by"] == {"1": 400}
    assert body["total_visible_bytes"] == 0


def test_dir_entry_itself_not_counted():
    l0 = make_tar([("app", "dir"), ("app/sub", "dir")])
    body = body_of([l0], "/app")
    assert body["total_written_bytes"] == 0


# ---------------------------------------------------------------------------
# 覆盖 / 遮蔽归因
# ---------------------------------------------------------------------------

def test_overwrite_attributes_old_bytes_to_original_and_new_layer():
    l0 = make_tar([("app", "dir"), ("app/a", "file", "x" * 100)])
    l1 = make_tar([("app/a", "file", "y" * 30)])
    body = body_of([l0, l1], "/app")
    layers = layer_map(body)
    assert layers[0]["written_bytes"] == 100
    assert layers[0]["visible_bytes"] == 0
    assert layers[0]["shadowed_bytes"] == 100
    assert layers[0]["shadowed_by"] == {"1": 100}
    # 同路径重建算新写入；最终可见的是新写入的 30 字节
    assert layers[1]["written_bytes"] == 30
    assert layers[1]["visible_bytes"] == 30
    assert layers[1]["shadowed_bytes"] == 0


def test_only_first_shadower_gets_credit():
    l0 = make_tar([("app", "dir"), ("app/a", "file", "x" * 100)])
    l1 = make_tar([("app/a", "file", "y" * 30)])
    l2 = make_tar([("app/a", "file", "z" * 20)])
    layers = layer_map(body_of([l0, l1, l2], "/app"))
    # l0 的 100 字节只归因给 l1（首次遮蔽），l2 不能再记一次
    assert layers[0]["shadowed_by"] == {"1": 100}
    # l1 的 30 字节归因给 l2
    assert layers[1]["shadowed_by"] == {"2": 30}
    assert layers[2]["visible_bytes"] == 20
    assert body_of([l0, l1, l2], "/app")["total_written_bytes"] == 150


def test_whiteout_directory_removes_lower_subtree():
    l0 = make_tar([
        ("app", "dir"),
        ("app/sub", "dir"),
        ("app/sub/a", "file", "x" * 70),
        ("app/sub/deep", "dir"),
        ("app/sub/deep/b", "file", "y" * 30),
        ("app/keep", "file", "z" * 5),
    ])
    l1 = make_tar([("app/.wh.sub", "whiteout")])
    layers = layer_map(body_of([l0, l1], "/app"))
    assert layers[0]["written_bytes"] == 105
    assert layers[0]["visible_bytes"] == 5
    assert layers[0]["shadowed_by"] == {"1": 100}


def test_opaque_hides_lower_children_but_keeps_same_layer_additions():
    l0 = make_tar([
        ("app", "dir"),
        ("app/old", "file", "x" * 100),
        ("app/sub", "dir"),
        ("app/sub/old2", "file", "y" * 40),
    ])
    l1 = make_tar([
        ("app/.wh..wh..opq", "opaque"),
        ("app/new", "file", "z" * 10),
    ])
    layers = layer_map(body_of([l0, l1], "/app"))
    assert layers[0]["written_bytes"] == 140
    assert layers[0]["visible_bytes"] == 0
    assert layers[0]["shadowed_by"] == {"1": 140}
    assert layers[1]["written_bytes"] == 10
    assert layers[1]["visible_bytes"] == 10


def test_opaque_in_subdirectory_only_hides_that_subtree():
    l0 = make_tar([
        ("app", "dir"),
        ("app/a", "file", "x" * 10),
        ("app/sub", "dir"),
        ("app/sub/b", "file", "y" * 20),
    ])
    l1 = make_tar([("app/sub/.wh..wh..opq", "opaque")])
    layers = layer_map(body_of([l0, l1], "/app"))
    assert layers[0]["visible_bytes"] == 10
    assert layers[0]["shadowed_by"] == {"1": 20}


# ---------------------------------------------------------------------------
# 目录换成普通文件 / 软链接：旧子树计入该层遮蔽
# ---------------------------------------------------------------------------

def test_directory_replaced_by_file_shadows_old_subtree():
    l0 = make_tar([
        ("app", "dir"),
        ("app/sub", "dir"),
        ("app/sub/a", "file", "x" * 60),
        ("app/sub/deep", "dir"),
        ("app/sub/deep/b", "file", "y" * 40),
        ("app/keep", "file", "z" * 7),
    ])
    l1 = make_tar([("app/sub", "file", "now-a-file")])
    layers = layer_map(body_of([l0, l1], "/app"))
    assert layers[0]["written_bytes"] == 107
    assert layers[0]["visible_bytes"] == 7
    assert layers[0]["shadowed_by"] == {"1": 100}
    # 新普通文件本身也是该层写入
    assert layers[1]["written_bytes"] == len("now-a-file")
    assert layers[1]["visible_bytes"] == len("now-a-file")


def test_directory_replaced_by_symlink_shadows_old_subtree():
    l0 = make_tar([
        ("app", "dir"),
        ("app/sub", "dir"),
        ("app/sub/a", "file", "x" * 25),
    ])
    l1 = make_tar([("app/sub", "symlink", "/elsewhere")])
    layers = layer_map(body_of([l0, l1], "/app"))
    assert layers[0]["shadowed_by"] == {"1": 25}
    # 软链接没有文件字节，不计写入
    assert layers[1]["written_bytes"] == 0


def test_directory_over_lower_file_does_not_shadow_lower_files_below():
    """下层同名路径是普通文件时，上层建目录只替换该文件，不影响其它。"""

    l0 = make_tar([
        ("app", "dir"),
        ("app/x", "file", "x" * 12),
        ("app/y", "file", "y" * 8),
    ])
    l1 = make_tar([("app/x", "dir")])
    layers = layer_map(body_of([l0, l1], "/app"))
    assert layers[0]["visible_bytes"] == 8
    assert layers[0]["shadowed_by"] == {"1": 12}


# ---------------------------------------------------------------------------
# 同层新增不受本层 whiteout / opaque 影响，且与 tar 内顺序无关
# ---------------------------------------------------------------------------

def test_same_layer_whiteout_does_not_hide_same_layer_addition():
    l0 = make_tar([("app", "dir"), ("app/a", "file", "x" * 100)])

    def check(layer1_members: list[tuple]) -> None:
        l1 = make_tar(layer1_members)
        layers = layer_map(body_of([l0, l1], "/app"))
        # 下层旧版本仍被本层 whiteout 遮蔽
        assert layers[0]["shadowed_by"] == {"1": 100}
        # 同层新增照常可见
        assert layers[1]["written_bytes"] == 30
        assert layers[1]["visible_bytes"] == 30
        assert layers[1]["shadowed_bytes"] == 0

    check([("app/.wh.a", "whiteout"), ("app/a", "file", "y" * 30)])
    check([("app/a", "file", "y" * 30), ("app/.wh.a", "whiteout")])


def test_same_layer_opaque_does_not_hide_same_layer_addition_any_order():
    l0 = make_tar([("app", "dir"), ("app/old", "file", "x" * 50)])

    def check(layer1_members: list[tuple]) -> None:
        layers = layer_map(body_of([l0, make_tar(layer1_members)], "/app"))
        assert layers[0]["shadowed_by"] == {"1": 50}
        assert layers[1]["written_bytes"] == 11
        assert layers[1]["visible_bytes"] == 11

    check([("app/.wh..wh..opq", "opaque"), ("app/new", "file", "y" * 11)])
    check([("app/new", "file", "y" * 11), ("app/.wh..wh..opq", "opaque")])


def test_whiteout_then_recreate_in_higher_layer_attribution():
    l0 = make_tar([("app", "dir"), ("app/a", "file", "x" * 100)])
    l1 = make_tar([("app/.wh.a", "whiteout")])
    l2 = make_tar([("app/a", "file", "y" * 40)])
    layers = layer_map(body_of([l0, l1, l2], "/app"))
    assert layers[0]["shadowed_by"] == {"1": 100}
    assert layers[1]["written_bytes"] == 0
    assert layers[2]["written_bytes"] == 40
    assert layers[2]["visible_bytes"] == 40


# ---------------------------------------------------------------------------
# 层无效 / 输入非法：只返回原因
# ---------------------------------------------------------------------------

def test_gzip_layer_rejected_with_reason_only():
    import gzip

    raw = make_tar([("app", "dir"), ("app/a", "file", "x")])
    r = usage([gzip.compress(raw)], "/app")
    assert r.status_code == 400
    body = r.json()
    assert set(body) == {"error"}
    assert "gzip" in body["error"]


def test_garbage_layer_rejected_with_reason_only():
    r = usage([b"not a tar at all" * 10], "/app")
    assert r.status_code == 400
    assert set(r.json()) == {"error"}


def test_relative_target_dir_rejected():
    l0 = make_tar([("app", "dir")])
    r = usage([l0], "app")
    assert r.status_code == 422
    assert "绝对路径" in r.json()["error"]


def test_no_layers_rejected():
    r = client.post("/api/layer-usage", data={"target_dir": "/app"})
    assert r.status_code == 422


def test_invalid_second_layer_returns_only_reason():
    """任一层无效时整次请求只返回原因，不输出部分统计。"""

    l0 = make_tar([("app", "dir"), ("app/a", "file", "x" * 10)])
    l1 = make_tar([("app/.wh.a", "whiteout")])
    r = usage([l0, b"garbage" * 20, l1], "/app")
    assert r.status_code == 400
    assert set(r.json()) == {"error"}
    assert "layer1.tar" in r.json()["error"]


def test_docs_lists_usage_endpoint():
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    assert "/api/layer-usage" in schema["paths"]
    assert "/api/troubleshoot" in schema["paths"]


def test_invariant_written_equals_visible_plus_shadowed():
    l0 = make_tar([("app", "dir"), ("app/a", "file", "x" * 10)])
    l1 = make_tar([("app/a", "file", "y" * 4)])
    l2 = make_tar([("app/b", "file", "z" * 6)])
    body = body_of([l0, l1, l2], "/app")
    for item in body["layers"]:
        assert item["written_bytes"] == item["visible_bytes"] + item["shadowed_bytes"]
    assert body["total_written_bytes"] == (
        body["total_visible_bytes"] + body["total_shadowed_bytes"]
    )


def test_file_path_as_target_dir_reports_zero():
    l0 = make_tar([("app", "dir"), ("app/a", "file", "x" * 10)])
    body = body_of([l0], "/app/a")
    assert body["total_written_bytes"] == 0


def test_ancestor_replaced_by_file_shadows_whole_target_history():
    l0 = make_tar([
        ("app", "dir"),
        ("app/sub", "dir"),
        ("app/sub/a", "file", "x" * 30),
    ])
    l1 = make_tar([("app/sub", "file", "now-file")])
    body = body_of([l0, l1], "/app/sub")
    layers = layer_map(body)
    assert layers[0]["shadowed_by"] == {"1": 30}
    assert layers[0]["visible_bytes"] == 0


def test_target_path_alias_accepted():
    l0 = make_tar([("app", "dir"), ("app/a", "file", "x" * 10)])
    r = client.post(
        "/api/layer-usage",
        files=[("layers", ("l0.tar", l0, "application/x-tar"))],
        data={"target_path": "/app"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["target_dir"] == "/app"
    assert r.json()["total_written_bytes"] == 10


def test_conflicting_aliases_rejected():
    l0 = make_tar([("app", "dir")])
    r = client.post(
        "/api/layer-usage",
        files=[("layers", ("l0.tar", l0, "application/x-tar"))],
        data={"target_dir": "/app", "target_path": "/other"},
    )
    assert r.status_code == 422
    assert "不一致" in r.json()["error"]


def test_blank_target_rejected():
    l0 = make_tar([("app", "dir")])
    r = usage([l0], "  ")
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# 复杂度：大量条目 + 大量 whiteout 不应退化为逐层全表扫描
# ---------------------------------------------------------------------------

def test_many_entries_and_whiteouts_scale():
    import time

    big = make_tar(
        [("root", "dir")]
        + [(f"root/f{i}", "file", "x" * 4) for i in range(20_000)]
    )
    wo = make_tar(
        [(f"root/.wh.f{i}", "whiteout") for i in range(0, 20_000, 2)]
    )
    t0 = time.perf_counter()
    body = body_of([big, wo], "/root")
    elapsed = time.perf_counter() - t0
    assert body["total_written_bytes"] == 20_000 * 4
    assert body["total_visible_bytes"] == 10_000 * 4
    assert body["total_shadowed_bytes"] == 10_000 * 4
    assert elapsed < 8


def test_repeated_layer_replacement_is_linear_in_events():
    """每层大量新增 + 一个整体删除，反复叠加也不应出现平方级退化。"""

    import time

    layers = []
    for _ in range(12):
        members = [("work", "dir")] + [
            (f"work/f{i}", "file", "x" * 8) for i in range(3_000)
        ]
        layers.append(make_tar(members))
        layers.append(make_tar([("work/.wh..wh..opq", "opaque")]))
    t0 = time.perf_counter()
    body = body_of(layers, "/work")
    elapsed = time.perf_counter() - t0
    assert body["total_written_bytes"] == 12 * 3_000 * 8
    assert body["total_visible_bytes"] == 0
    assert elapsed < 8
