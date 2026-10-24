"""Lumen Launcher 自检脚本。

用法：
    python tests/selftest.py            # 离线自检（不需要网络，几秒钟）
    python tests/selftest.py --online   # 额外验证版本清单 / 测速 / 真实下载

设计成「零依赖、可被 CI 直接调用」：只用标准库 + requests，
所有临时数据写在系统临时目录，不污染用户数据。
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import http.server
import json
import os
import socket
import sys
import tempfile
import threading
import traceback
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 必须在导入 lumen.config 之前设置，保证测试不碰真实用户目录
_TMP_ROOT = Path(tempfile.mkdtemp(prefix="lumen-selftest-"))
os.environ["LUMEN_DATA_DIR"] = str(_TMP_ROOT / "data")

from lumen.auth.account import offline_account, offline_uuid, validate_username  # noqa: E402
from lumen.config import AppPaths, Settings, resolve_data_root  # noqa: E402
from lumen.core.crash import analyze_lines  # noqa: E402
from lumen.core.downloader import DownloadTask, Downloader  # noqa: E402
from lumen.core.instances import InstanceStore  # noqa: E402
from lumen.core.launcher import LaunchOptions, prepare_launch  # noqa: E402
from lumen.core.manifest import (  # noqa: E402
    VersionDetail,
    argument_list,
    evaluate_rules,
    library_files,
    maven_to_path,
    merge_version_data,
)
from lumen.core.mirrors import MirrorSelector, to_mirror_url  # noqa: E402
from lumen.modpack.importer import inspect_modpack  # noqa: E402
from lumen.utils.versions import (  # noqa: E402
    java_major_from_string,
    parse_java_version_output,
    parse_release,
    required_java_major,
)

TESTS: list[tuple[str, callable]] = []


def test(name: str):
    def decorator(fn):
        TESTS.append((name, fn))
        return fn

    return decorator


def _paths() -> AppPaths:
    return AppPaths(data_root=resolve_data_root()).ensure()


# --------------------------------------------------------------------------- #
# 1. 版本号与 Java 需求
# --------------------------------------------------------------------------- #
@test("版本号解析与 Java 需求推断")
def test_version_math() -> None:
    assert parse_release("1.21.4") == (1, 21, 4, 5, 0), parse_release("1.21.4")
    assert parse_release("1.21.4-pre2") == (1, 21, 4, 3, 2)
    assert parse_release("1.7.10")[0:3] == (1, 7, 10)
    assert required_java_major("1.16.5") == 8
    assert required_java_major("1.17") == 17
    assert required_java_major("1.20.4") == 17
    assert required_java_major("1.20.5") == 21
    assert required_java_major("1.21.4") == 21
    assert required_java_major("1.12.2") == 8
    assert required_java_major("1.21.4", detail_major=17) == 17, "version.json 的 javaVersion 必须优先"
    assert java_major_from_string("1.8.0_402") == 8
    assert java_major_from_string("21.0.1") == 21
    version, major = parse_java_version_output('openjdk version "17.0.8" 2023-07-18')
    assert (version, major) == ("17.0.8", 17), (version, major)
    version, major = parse_java_version_output('java version "1.8.0_402"\nJava(TM) SE Runtime')
    assert (version, major) == ("1.8.0_402", 8)


# --------------------------------------------------------------------------- #
# 2. 镜像改写
# --------------------------------------------------------------------------- #
@test("镜像地址改写表")
def test_mirrors() -> None:
    cases = {
        "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json": "https://bmclapi2.bangbang93.com/mc/game/version_manifest_v2.json",
        "https://launchermeta.mojang.com/v1/objects/aa/bb": "https://bmclapi2.bangbang93.com/v1/objects/aa/bb",
        "https://piston-data.mojang.com/v1/objects/cc/dd": "https://bmclapi2.bangbang93.com/v1/objects/cc/dd",
        "https://libraries.minecraft.net/com/google/code/gson/gson/2.10.1/gson-2.10.1.jar": "https://bmclapi2.bangbang93.com/maven/com/google/code/gson/gson/2.10.1/gson-2.10.1.jar",
        "https://resources.download.minecraft.net/ab/abcdef": "https://bmclapi2.bangbang93.com/assets/ab/abcdef",
        "https://maven.minecraftforge.net/net/minecraftforge/forge/maven-metadata.xml": "https://bmclapi2.bangbang93.com/maven/net/minecraftforge/forge/maven-metadata.xml",
        "https://maven.neoforged.net/releases/net/neoforged/neoforge/maven-metadata.xml": "https://bmclapi2.bangbang93.com/maven/net/neoforged/neoforge/maven-metadata.xml",
        "https://meta.fabricmc.net/v2/versions/loader/1.21.4": "https://bmclapi2.bangbang93.com/fabric-meta/v2/versions/loader/1.21.4",
        "https://maven.fabricmc.net/net/fabricmc/fabric-loader/0.16.9/fabric-loader-0.16.9.jar": "https://bmclapi2.bangbang93.com/maven/net/fabricmc/fabric-loader/0.16.9/fabric-loader-0.16.9.jar",
        "https://repo1.maven.org/maven2/org/ow2/asm/asm/9.6/asm-9.6.jar": "https://mirrors.cloud.tencent.com/nexus/repository/maven-public/org/ow2/asm/asm/9.6/asm-9.6.jar",
    }
    for source, expected in cases.items():
        actual = to_mirror_url(source)
        assert actual == expected, f"{source}\n  期望 {expected}\n  实际 {actual}"
    assert to_mirror_url("https://example.com/foo.jar") is None, "未知地址不应被改写"


# --------------------------------------------------------------------------- #
# 3. rules 判定
# --------------------------------------------------------------------------- #
@test("libraries/arguments 的 rules 判定")
def test_rules() -> None:
    assert evaluate_rules(None) is True
    assert evaluate_rules([{"action": "allow"}]) is True
    assert evaluate_rules([{"action": "allow", "os": {"name": "windows"}}], os_name="linux") is False
    assert evaluate_rules([{"action": "allow", "os": {"name": "windows"}}], os_name="windows") is True
    assert (
        evaluate_rules(
            [{"action": "allow"}, {"action": "disallow", "os": {"name": "windows"}}],
            os_name="windows",
        )
        is False
    )
    # 32 位专用规则在 64 位系统上不生效
    assert (
        evaluate_rules(
            [{"action": "allow", "os": {"name": "windows", "arch": "x86"}}],
            os_name="windows",
            arch="x86_64",
        )
        is False
    )
    # features
    assert evaluate_rules([{"action": "allow", "features": {"is_demo_user": True}}], features={}) is False
    assert (
        evaluate_rules([{"action": "allow", "features": {"is_demo_user": True}}], features={"is_demo_user": True})
        is True
    )


# --------------------------------------------------------------------------- #
# 4. Maven / libraries 解析
# --------------------------------------------------------------------------- #
@test("Maven 坐标与 libraries 解析")
def test_libraries() -> None:
    assert maven_to_path("org.lwjgl:lwjgl:3.3.2") == "org/lwjgl/lwjgl/3.3.2/lwjgl-3.3.2.jar"
    assert maven_to_path("org.lwjgl:lwjgl:3.3.2:natives-windows") == "org/lwjgl/lwjgl/3.3.2/lwjgl-3.3.2-natives-windows.jar"
    assert maven_to_path("de.oceanlabs.mcp:mcp_config:1.20.1@zip") == "de/oceanlabs/mcp/mcp_config/1.20.1/mcp_config-1.20.1.zip"

    libraries = [
        {"name": "com.google.code.gson:gson:2.10.1", "downloads": {"artifact": {"url": "https://libraries.minecraft.net/gson.jar", "path": "com/google/code/gson/gson/2.10.1/gson-2.10.1.jar", "sha1": "abc", "size": 283367}}},
        {
            "name": "org.lwjgl:lwjgl:3.3.2:natives-windows",
            "rules": [{"action": "allow", "os": {"name": "windows"}}],
            "downloads": {"artifact": {"url": "https://libraries.minecraft.net/lwjgl-natives.jar", "path": "org/lwjgl/lwjgl/3.3.2/lwjgl-3.3.2-natives-windows.jar"}},
        },
        {
            "name": "old:natives-lib:1.0",
            "natives": {"windows": "natives-windows-${arch}"},
            "downloads": {"classifiers": {"natives-windows-64": {"url": "https://libraries.minecraft.net/n64.jar", "path": "old/natives-lib/1.0/natives-lib-1.0-natives-windows-64.jar"}}},
        },
        # 没有 downloads 字段（Fabric profile 风格），靠 url 基址拼接
        {"name": "net.fabricmc:fabric-loader:0.16.9", "url": "https://maven.fabricmc.net/"},
    ]
    classpath, natives = library_files(libraries, os_name="windows")
    coordinates = [lib.coordinate for lib in classpath]
    assert "com.google.code.gson:gson:2.10.1" in coordinates
    assert "net.fabricmc:fabric-loader:0.16.9" in coordinates
    fabric = next(lib for lib in classpath if lib.coordinate.startswith("net.fabricmc"))
    assert fabric.url == "https://maven.fabricmc.net/net/fabricmc/fabric-loader/0.16.9/fabric-loader-0.16.9.jar", fabric.url
    native_coords = [lib.coordinate for lib in natives]
    assert any("natives-windows" in c for c in native_coords), native_coords

    # 1.19+ 的 version.json 里 x64/arm64/x86 三种 natives 共用一条 rules，
    # 必须按 classifier 后缀只保留当前架构，否则会互相覆盖成 32 位 DLL
    arch_libraries = [
        {
            "name": f"org.lwjgl:lwjgl:3.3.3:natives-windows{suffix}",
            "downloads": {"artifact": {"url": f"https://libraries.minecraft.net/lwjgl{suffix}.jar", "path": f"org/lwjgl/lwjgl/3.3.3/lwjgl-3.3.3-natives-windows{suffix}.jar"}},
        }
        for suffix in ("", "-arm64", "-x86")
    ]
    _, arch_natives = library_files(arch_libraries, os_name="windows")
    assert [lib.coordinate for lib in arch_natives] == [
        "org.lwjgl:lwjgl:3.3.3:natives-windows"
    ], [lib.coordinate for lib in arch_natives]

    jvm, game = argument_list(
        {
            "jvm": ["-Djava.library.path=${natives_directory}", "-cp", "${classpath}"],
            "game": [
                "--username", "${auth_player_name}",
                {"rules": [{"action": "allow", "features": {"has_custom_resolution": True}}], "value": ["--width", "854"]},
                {"rules": [{"action": "allow", "os": {"name": "osx"}}], "value": "-XstartOnFirstThread"},
            ],
        }
    )
    assert jvm == ["-Djava.library.path=${natives_directory}", "-cp", "${classpath}"], jvm
    assert "--username" in game and "--width" not in game, game


# --------------------------------------------------------------------------- #
# 5. 版本合并（inheritsFrom）
# --------------------------------------------------------------------------- #
@test("inheritsFrom 版本合并")
def test_merge_versions() -> None:
    parent = {
        "id": "1.20.1",
        "mainClass": "net.minecraft.client.main.Main",
        "libraries": [{"name": "a:a:1.0"}],
        "arguments": {"jvm": ["-cp", "${classpath}"], "game": ["--username", "${auth_player_name}"]},
        "assetIndex": {"id": "5"},
        "downloads": {"client": {"url": "x"}},
    }
    child = {
        "id": "1.20.1-forge-47.2.0",
        "inheritsFrom": "1.20.1",
        "mainClass": "cpw.mods.bootstraplauncher.BootstrapLauncher",
        "libraries": [{"name": "a:a:1.0"}, {"name": "b:b:2.0"}],
        "arguments": {"jvm": ["-Dfoo=bar"], "game": ["--fml.forgeVersion", "47.2.0"]},
    }
    merged = merge_version_data(parent, child, version_id="1.20.1-forge-47.2.0")
    assert merged["mainClass"] == "cpw.mods.bootstraplauncher.BootstrapLauncher"
    assert merged["id"] == "1.20.1-forge-47.2.0"
    assert [lib["name"] for lib in merged["libraries"]] == ["a:a:1.0", "b:b:2.0"]
    assert merged["arguments"]["jvm"] == ["-cp", "${classpath}", "-Dfoo=bar"]
    assert merged["arguments"]["game"] == ["--username", "${auth_player_name}", "--fml.forgeVersion", "47.2.0"]
    assert merged["assetIndex"] == {"id": "5"}, "子版本没有的字段必须继承父版本"

    # 同 artifact 不同版本：子版本（Forge）必须覆盖父版本（vanilla）。
    # 否则 guava 32.1.2 与 33.3.1 会同时进 classpath，Java 模块系统直接崩溃。
    parent_dup = {
        "id": "1.21.4",
        "libraries": [
            {"name": "com.google.guava:guava:33.3.1-jre"},
            {"name": "com.google.guava:failureaccess:1.0.2"},
            {"name": "org.lwjgl:lwjgl:3.3.3"},
        ],
    }
    child_dup = {
        "id": "1.21.4-forge",
        "inheritsFrom": "1.21.4",
        "libraries": [
            {"name": "com.google.guava:guava:32.1.2-jre"},
            {"name": "com.google.guava:failureaccess:1.0.1"},
            {"name": "net.minecraftforge:forge:1.21.4-54.1.22:universal"},
        ],
    }
    merged_dup = merge_version_data(parent_dup, child_dup, version_id="1.21.4-forge")
    names_dup = [lib["name"] for lib in merged_dup["libraries"]]
    assert "com.google.guava:guava:32.1.2-jre" in names_dup, names_dup
    assert "com.google.guava:guava:33.3.1-jre" not in names_dup, names_dup
    assert "com.google.guava:failureaccess:1.0.1" in names_dup, names_dup
    assert "com.google.guava:failureaccess:1.0.2" not in names_dup, names_dup
    assert len(names_dup) == 4, names_dup


# --------------------------------------------------------------------------- #
# 6. 启动命令组装
# --------------------------------------------------------------------------- #
def _fake_instance(paths: AppPaths, version_id: str = "1.21.4"):
    store = InstanceStore(paths)
    instance = store.create("自检实例", version_id)
    version_dir = instance.version_dir(version_id)
    version_dir.mkdir(parents=True, exist_ok=True)
    (version_dir / "natives").mkdir(exist_ok=True)
    (instance.version_jar(version_id)).write_bytes(b"PK\x03\x04fake-jar")
    library_rel = "com/google/code/gson/gson/2.10.1/gson-2.10.1.jar"
    library_path = paths.libraries_dir / library_rel
    library_path.parent.mkdir(parents=True, exist_ok=True)
    library_path.write_bytes(b"PK\x03\x04fake-lib")
    data = {
        "id": version_id,
        "type": "release",
        "mainClass": "net.minecraft.client.main.Main",
        "assets": "17",
        "assetIndex": {"id": "17", "url": "https://piston-meta.mojang.com/x.json"},
        "downloads": {"client": {"url": "https://piston-meta.mojang.com/client.jar"}},
        "javaVersion": {"majorVersion": 21},
        "arguments": {
            "jvm": ["-Djava.library.path=${natives_directory}", "-cp", "${classpath}"],
            "game": [
                "--username", "${auth_player_name}",
                "--version", "${version_name}",
                "--gameDir", "${game_directory}",
                "--assetsDir", "${assets_root}",
                "--assetIndex", "${assets_index_name}",
                "--uuid", "${auth_uuid}",
                "--accessToken", "${auth_access_token}",
                "--userType", "${user_type}",
            ],
        },
        "libraries": [
            {
                "name": "com.google.code.gson:gson:2.10.1",
                "downloads": {"artifact": {"url": "https://libraries.minecraft.net/gson.jar", "path": library_rel, "sha1": "x"}},
            }
        ],
    }
    instance.version_json(version_id).write_text(json.dumps(data), encoding="utf-8")
    return instance


@test("启动参数组装（-Xmx/-cp/natives/账号变量）")
def test_launch_arguments() -> None:
    paths = _paths()
    instance = _fake_instance(paths)
    account = offline_account("Tester")
    options = LaunchOptions(java_path=sys.executable, memory_mb=4096, jvm_args='-Dlumen.test=1 "-Dquoted=hello world"')
    prepared = prepare_launch(paths, instance, account, options)
    command = prepared.command
    joined = " ".join(command)
    assert command[0] == sys.executable
    assert "-Xmx4096M" in command, command[:6]
    # 不再硬编码 -Xmn：G1/ZGC 下会改变分代策略，和用户参数也容易冲突
    assert not any(token.startswith("-Xmn") for token in command), command[:8]
    # 用户在 JVM 参数里自己写了内存开关时，启动器不能再追加 -Xmx
    custom = prepare_launch(
        paths,
        instance,
        account,
        LaunchOptions(java_path=sys.executable, memory_mb=4096, jvm_args="-Xmx2048M -Xmn512M"),
    )
    assert "-Xmx2048M" in custom.command
    assert "-Xmx4096M" not in custom.command, "用户显式内存参数必须被尊重"
    assert "-Xmn512M" in custom.command
    assert "-cp" in command, "缺少 -cp"
    assert "-Djava.library.path=" in joined
    assert "net.minecraft.client.main.Main" in command
    assert "--username" in command and "Tester" in command
    assert account.uuid in command, "UUID 必须注入"
    assert "--accessToken" in command and account.access_token in command
    assert "--version" in command and "1.21.4" in command
    assert "-Dlumen.test=1" in command
    assert "-Dquoted=hello world" in command, command
    main_index = command.index("net.minecraft.client.main.Main")
    cp_index = command.index("-cp")
    assert cp_index < main_index, "-cp 必须位于主类之前"
    cp_value = command[cp_index + 1]
    assert os.pathsep in cp_value
    assert "gson-2.10.1.jar" in cp_value and "1.21.4.jar" in cp_value
    assert prepared.log_file.parent.is_dir()


# --------------------------------------------------------------------------- #
# 6.5 Java 运行时选择 / 镜像并发测速 / CurseForge 搜索解析
# --------------------------------------------------------------------------- #
@test("Java 选择：同 major 下挑最新补丁版本")
def test_pick_java() -> None:
    from lumen.java.detect import JavaRuntime, pick_java

    older = JavaRuntime(path=Path("/fake/jdk8/bin/java"), version="17.0.8", major=17, source="测试")
    newer = JavaRuntime(path=Path("/fake/jdk10/bin/java"), version="17.0.10", major=17, source="测试")
    gathered = pick_java([older, newer], 17)
    # 字符串比较会把 17.0.8 当成更新，必须按数字元组比较
    assert gathered is not None and gathered.version == "17.0.10", gathered.version
    assert pick_java([older], 21).version == "17.0.8", "没有精确版本时应退到更高版本里最小的"
    assert pick_java([], 21) is None


@test("镜像测速：两个源并发探测")
def test_probe_concurrent() -> None:
    import threading
    import time

    from lumen.core import mirrors

    paths = _paths()
    selector = mirrors.MirrorSelector(Settings(paths), paths)
    calls: list[str] = []
    lock = threading.Lock()

    def fake_sample(url: str, *, sample_bytes: int = 65536, timeout: float = 8.0):
        with lock:
            calls.append(url)
        time.sleep(0.35)
        return 10.0, 512.0

    original = mirrors.sample_url
    mirrors.sample_url = fake_sample  # type: ignore[assignment]
    try:
        started = time.monotonic()
        result = selector.probe_all(force=True)
        elapsed = time.monotonic() - started
    finally:
        mirrors.sample_url = original  # type: ignore[assignment]

    assert set(result) == set(mirrors.SOURCE_ORDER), result
    assert all(probe.ok for probe in result.values()), result
    assert len(calls) == len(mirrors.SOURCE_ORDER) * len(mirrors.PROBE_URLS_OFFICIAL), calls
    # 4 个样本 × 0.35s 串行至少 1.4s；并发应接近 0.35s
    assert elapsed < 0.9, f"测速没有真正并发：{elapsed:.2f}s"


@test("资源目录：CurseForge 搜索结果解析")
def test_curse_search_parsing() -> None:
    from lumen.core.catalog import CurseForgeClient

    client = CurseForgeClient("fake-key")
    captured: dict = {}

    def fake_request(path: str, *, params=None, expect=dict):
        captured["path"] = path
        captured["params"] = params
        return {
            "data": [
                {
                    "id": 238222,
                    "name": "JEI",
                    "slug": "jei",
                    "summary": "物品管理器",
                    "downloadCount": 123456,
                    "logo": {"thumbnailUrl": "https://example.invalid/jei.png"},
                    "categories": [{"name": "Utility"}],
                    "gameVersionLatestFiles": [
                        {"gameVersion": "1.21.4", "fileId": 1},
                        {"gameVersion": "1.20.1", "fileId": 2},
                        {"gameVersion": "1.21.4", "fileId": 3},
                    ],
                }
            ]
        }

    client._request = fake_request  # type: ignore[assignment]
    hits = client.search("jei", loader="fabric", game_version="1.21.4")
    assert len(hits) == 1
    hit = hits[0]
    # 曾按字符串过滤 dict 列表 → 永远是空；现在必须解析出真实版本并去重
    assert hit.game_versions == ["1.21.4", "1.20.1"], hit.game_versions
    assert hit.title == "JEI" and hit.source == "curseforge"
    assert hit.downloads_text == "123.5K"
    assert captured["params"]["gameVersion"] == "1.21.4"
    assert captured["params"]["modLoaderType"] == 4, captured["params"]


@test("资源目录：前置依赖解析（只提示 required）")
def test_dependency_resolution() -> None:
    from lumen.core.catalog import (
        CatalogDependency,
        CatalogVersion,
        CurseForgeClient,
        ModrinthClient,
        resolve_dependencies,
    )

    class FakeModrinth(ModrinthClient):
        def projects(self, ids):
            return [{"id": "P1", "slug": "fabric-api", "title": "Fabric API"}]

        def versions(self, project_id, *, loader="", game_version="", limit=60):
            return [
                CatalogVersion(
                    version_id="new",
                    version_number="0.116.0",
                    loaders=["fabric"],
                    game_versions=["1.21.4"],
                    download_url="https://cdn.modrinth.com/x.jar",
                    filename="fabric-api.jar",
                    file_size=100,
                    sha1="a" * 40,
                ),
                CatalogVersion(
                    version_id="old",
                    version_number="0.100.0",
                    loaders=["fabric"],
                    game_versions=["1.20.1"],
                    download_url="https://cdn.modrinth.com/y.jar",
                    filename="fabric-api-old.jar",
                    date="2024-01-01T00:00:00Z",
                ),
            ]

    infos = resolve_dependencies(
        FakeModrinth(),
        [
            CatalogDependency(project_id="P1", kind="required", source="modrinth"),
            CatalogDependency(project_id="P2", kind="optional", source="modrinth"),
        ],
        loader="fabric",
        game_version="1.21.4",
    )
    assert len(infos) == 1, "可选依赖不应该打扰用户"
    info = infos[0]
    assert info.title == "Fabric API"
    assert info.page_url == "https://modrinth.com/mod/fabric-api"
    assert info.version is not None and info.version.version_number == "0.116.0", info.version

    class FakeCurse(CurseForgeClient):
        def project(self, project_id):
            return {"name": "JEI", "slug": "jei"}

        def versions(self, project_id, *, loader="", game_version="", limit=60):
            return [
                CatalogVersion(
                    version_id="9",
                    version_number="19.21.0.247",
                    download_url="https://edge.forgecdn.net/files/1/2/jei.jar",
                    filename="jei.jar",
                )
            ]

    infos2 = resolve_dependencies(
        FakeCurse("fake-key"),
        [CatalogDependency(project_id="238222", kind="required", source="curseforge")],
        loader="forge",
        game_version="1.21.4",
    )
    assert len(infos2) == 1
    assert infos2[0].title == "JEI"
    assert infos2[0].page_url.endswith("/minecraft/mc-mods/jei")
    assert infos2[0].version is not None

    # CurseForge 的 relationType：只有 2=optional / 3=required 才算依赖
    from lumen.core.catalog import _curse_dependencies

    parsed = _curse_dependencies(
        {"dependencies": [{"modId": 1, "relationType": 3}, {"modId": 2, "relationType": 2}, {"modId": 3, "relationType": 1}]}
    )
    assert [dep.project_id for dep in parsed] == ["1", "2"], parsed
    assert parsed[0].is_required and not parsed[1].is_required


@test("微软登录：请求体、错误分支与账号落地")
def test_microsoft_auth() -> None:
    import time

    from lumen.auth import microsoft as ms
    from lumen.utils.errors import NetworkError

    flow = ms.parse_device_code(
        {
            "user_code": "ABCD-EFGH",
            "device_code": "device",
            "verification_uri": "https://www.microsoft.com/link",
            "expires_in": 900,
            "interval": 5,
        }
    )
    assert flow.user_code == "ABCD-EFGH" and flow.expires_text == "15 分钟内有效"
    assert ms.token_poll_body("cid", "device")["grant_type"] == ms.DEVICE_GRANT
    assert ms.refresh_body("cid", "rt")["grant_type"] == "refresh_token"

    body = ms.xbox_auth_body("msa-token")
    assert body["Properties"]["RpsTicket"] == "d=msa-token"
    assert body["Properties"]["AuthMethod"] == "RPS"
    assert body["RelyingParty"] == "http://auth.xboxlive.com"
    assert ms.xsts_body("xbl")["Properties"]["UserTokens"] == ["xbl"]
    assert ms.minecraft_login_body("uhs", "xsts")["identityToken"] == "XBL3.0 x=uhs;xsts"

    assert ms.parse_uhs({"DisplayClaims": {"xui": [{"uhs": "12345"}]}}) == "12345"
    assert ms.parse_uhs({}) == ""
    assert ms.xsts_error({"XErr": 2148916233})[0].startswith("该微软账号还没有 Xbox")
    assert ms.xsts_error({"XErr": 999}) is None

    profile = ms.parse_profile({"id": "069a79f4", "name": "Notch"})
    assert profile.id == "069a79f4" and profile.name == "Notch"

    # 没有 client_id 时必须报可读错误，而不是静默失败
    try:
        ms.MicrosoftAuth("").request_device_code()
    except NetworkError as exc:
        assert "应用 ID" in str(exc), str(exc)
    else:
        raise AssertionError("没有 client_id 时应当抛错")

    account = ms.MicrosoftAuth("client-id").to_account(
        ms.SignInResult(
            access_token="mc-token",
            expires_at=0.0,
            refresh_token="refresh-token",
            profile=profile,
            xuid="uhs-123",
        )
    )
    assert account.type == "msa" and account.user_type == "msa"
    assert account.uuid == "069a79f4" and account.access_token == "mc-token"
    assert account.xuid == "uhs-123" and account.client_id == "client-id"
    assert account.is_offline is False
    assert account.needs_refresh is True, "expires_at=0 应视为需要刷新"
    account.expires_at = time.time() + 3600
    assert account.needs_refresh is False
    assert account.to_dict()["expires_at"] > 0


# --------------------------------------------------------------------------- #
# 7. 崩溃分析
# --------------------------------------------------------------------------- #
@test("崩溃分析命中常见原因")
def test_crash_analysis() -> None:
    findings = analyze_lines(
        [
            "java.lang.UnsupportedClassVersionError: net/minecraft/client/main/Main has been compiled by a more",
            "recent version of the Java Runtime (class file version 65.0)",
            "Exception in thread \"main\" java.lang.OutOfMemoryError: Java heap space",
            "Caused by: java.lang.UnsatisfiedLinkError: no lwjgl in java.library.path",
            "java.lang.DuplicateModsFoundException: duplicate mods",
        ],
        exit_code=1,
        java_major=8,
        required_java=21,
        memory_mb=1024,
    )
    titles = [finding.title for finding in findings]
    assert "Java 版本过低" in titles, titles
    assert "内存不足" in titles, titles
    assert "natives 本地库缺失" in titles, titles
    assert "模组重复" in titles, titles
    assert all(finding.suggestion for finding in findings), "每条结论都必须带可执行建议"
    assert "未识别的崩溃" not in titles


# --------------------------------------------------------------------------- #
# 8. mrpack 解析
# --------------------------------------------------------------------------- #
@test("mrpack 解析与导入计划")
def test_mrpack() -> None:
    archive = _TMP_ROOT / "sample.mrpack"
    index = {
        "formatVersion": 1,
        "game": "minecraft",
        "versionId": "1.0.0",
        "name": "示例整合包",
        "summary": "selftest",
        "dependencies": {"minecraft": "1.20.1", "fabric-loader": "0.15.11"},
        "files": [
            {
                "path": "mods/example-mod.jar",
                "hashes": {"sha1": "a" * 40, "sha512": "b" * 128},
                "env": {"client": "required", "server": "optional"},
                "downloads": ["https://cdn.modrinth.com/data/abc/versions/1/example-mod.jar"],
                "fileSize": 12345,
            },
            {
                "path": "mods/server-only.jar",
                "hashes": {"sha1": "c" * 40, "sha512": "d" * 128},
                "env": {"client": "unsupported", "server": "required"},
                "downloads": ["https://cdn.modrinth.com/data/def/versions/1/server-only.jar"],
                "fileSize": 999,
            },
        ],
    }
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("modrinth.index.json", json.dumps(index))
        zf.writestr("overrides/mods/config-example.toml", "key = 1")
        zf.writestr("overrides/config/test.json", "{}")
    plan = inspect_modpack(archive)
    assert plan.source == "mrpack", plan.source
    assert plan.mc_version == "1.20.1", plan.mc_version
    assert plan.loader_type == "fabric" and plan.loader_version == "0.15.11"
    assert plan.file_count == 1, plan.file_count
    assert plan.files[0].path.as_posix() == "mods/example-mod.jar"
    assert plan.override_prefixes == ["overrides"], plan.override_prefixes
    assert plan.total_size == 12345


# --------------------------------------------------------------------------- #
# 9. 实例管理
# --------------------------------------------------------------------------- #
@test("实例 CRUD 与 mods 启停")
def test_instances() -> None:
    paths = _paths()
    store = InstanceStore(paths)
    before = len(store.list_instances())
    instance = store.create("自检-实例", "1.21.4")
    assert (instance.game_dir / "mods").is_dir(), "应自动创建 mods 目录"
    assert store.get(instance.id) is not None
    assert len(store.list_instances()) == before + 1
    assert not hasattr(instance, "memory_gb") or instance.memory_gb is None

    mod = instance.mods_dir / "example.jar"
    mod.write_bytes(b"PK\x03\x04")
    mods = store.list_mods(instance)
    assert len(mods) == 1 and mods[0].enabled
    mods[0] = store.toggle_mod(mods[0])
    assert not mods[0].enabled
    assert (instance.mods_dir / "example.jar.disabled").exists()
    mods[0] = store.toggle_mod(mods[0])
    assert mods[0].enabled and mod.exists()
    store.delete_mod(mods[0])
    assert not mod.exists()

    cloned = store.clone(instance, "自检-副本")
    assert cloned.id != instance.id and cloned.name == "自检-副本"
    store.rename(cloned, "自检-改名")
    assert store.get(cloned.id).name == "自检-改名"

    store.delete(cloned)
    store.delete(instance)
    assert len(store.list_instances()) == before


# --------------------------------------------------------------------------- #
# 10. 设置读写
# --------------------------------------------------------------------------- #
@test("设置持久化与损坏恢复")
def test_settings() -> None:
    paths = _paths()
    settings = Settings(paths)
    settings.memory_gb = 7
    settings.download_source = "mirror"
    settings.set("show_snapshots", True)
    reloaded = Settings(paths)
    assert reloaded.memory_gb == 7, reloaded.memory_gb
    assert reloaded.download_source == "mirror"
    assert reloaded.get("show_snapshots") is True
    # 损坏的 JSON 不能导致启动失败
    paths.config_file.write_text("{not valid json", encoding="utf-8")
    recovered = Settings(paths)
    assert recovered.memory_gb > 0
    assert paths.config_file.with_suffix(".json.corrupt").exists(), "损坏配置应被备份"


# --------------------------------------------------------------------------- #
# 11. 下载器（本地 HTTP，验证断点/校验/跳过/重试）
# --------------------------------------------------------------------------- #
class _RangeHandler(http.server.SimpleHTTPRequestHandler):
    """支持 Range 的最小 HTTP 服务，用来验证断点续传。"""

    def log_message(self, *args):  # noqa: D401 - 静音
        return

    def do_GET(self):  # noqa: N802
        path = self.translate_path(self.path)
        try:
            data = Path(path).read_bytes()
        except OSError:
            self.send_error(404)
            return
        range_header = self.headers.get("Range", "")
        if range_header.startswith("bytes=") and data:
            spec = range_header[6:]
            try:
                start = int(spec.split("-")[0] or 0)
            except ValueError:
                start = 0
            chunk = data[start:] if start < len(data) else b""
            self.send_response(206 if start < len(data) else 416)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(chunk)))
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
            self.end_headers()
            self.wfile.write(chunk)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@test("资源目录：版本择优与 CurseForge Key 校验")
def test_catalog() -> None:
    from lumen.core.catalog import CatalogError, CatalogVersion, CurseForgeClient, pick_best_version

    versions = [
        CatalogVersion(version_id="forge-old", version_number="1.0", loaders=["forge"], game_versions=["1.20.1"], download_url="u1"),
        CatalogVersion(version_id="fabric-stable", version_number="2.0", loaders=["fabric"], game_versions=["1.21.4"], download_url="u2"),
        CatalogVersion(
            version_id="fabric-beta",
            version_number="3.0-beta",
            loaders=["fabric"],
            game_versions=["1.21.4"],
            download_url="u3",
            release_type="beta",
        ),
        CatalogVersion(version_id="no-url", version_number="9.9", download_url=""),
    ]
    best = pick_best_version(versions, loader="fabric", game_version="1.21.4")
    assert best is not None and best.version_id == "fabric-stable", (
        best.version_id if best else None,
        "应优先匹配加载器/版本且偏向稳定版，且跳过没有下载链接的条目",
    )
    assert pick_best_version([], loader="fabric") is None

    client = CurseForgeClient("")
    assert client.available is False
    try:
        client.search("jei")
    except CatalogError as exc:
        assert "API Key" in str(exc)
    else:
        raise AssertionError("CurseForge 没有 Key 时应当抛出可读错误")


@test("资源目录：Modrinth 结果解析（离线 fixtures）")
def test_catalog_parsing() -> None:
    from lumen.core.catalog import pick_best_version

    # 模拟 Modrinth 返回的版本结构（由 _parse 逻辑消费的核心字段）
    payload = [
        {
            "id": "abc",
            "name": "Sodium 0.6.13",
            "version_number": "mc1.21.4-0.6.13-fabric",
            "game_versions": ["1.21.4"],
            "loaders": ["fabric"],
            "date_published": "2025-01-01T00:00:00Z",
            "version_type": "release",
            "files": [
                {
                    "url": "https://cdn.modrinth.com/x.jar",
                    "filename": "sodium-fabric-0.6.13+mc1.21.4.jar",
                    "size": 1306799,
                    "primary": True,
                    "hashes": {"sha1": "a" * 40, "sha512": "b" * 128},
                }
            ],
        }
    ]
    file_entry = payload[0]["files"][0]
    assert file_entry["hashes"]["sha1"]
    assert file_entry["primary"] is True
    best = pick_best_version(
        [
            __import__("lumen.core.catalog", fromlist=["CatalogVersion"]).CatalogVersion(
                version_id="abc",
                version_number="mc1.21.4-0.6.13-fabric",
                loaders=["fabric"],
                game_versions=["1.21.4"],
                download_url=file_entry["url"],
                filename=file_entry["filename"],
                file_size=file_entry["size"],
                sha1=file_entry["hashes"]["sha1"],
            )
        ],
        loader="fabric",
        game_version="1.21.4",
    )
    assert best is not None and best.filename.endswith(".jar")


@test("下载器：校验/跳过/续传/失败重试")
def test_downloader() -> None:
    paths = _paths()
    serve_dir = Path(tempfile.mkdtemp(dir=_TMP_ROOT, prefix="http-"))
    payload = os.urandom(300_000)
    (serve_dir / "one.bin").write_bytes(payload)
    (serve_dir / "two.bin").write_bytes(payload[:1000])
    sha_one = hashlib.sha1(payload).hexdigest()
    sha_two = hashlib.sha1(payload[:1000]).hexdigest()

    handler = functools.partial(_RangeHandler, directory=str(serve_dir))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        settings = Settings(paths)
        selector = MirrorSelector(settings, paths)
        dest_dir = Path(tempfile.mkdtemp(dir=_TMP_ROOT, prefix="dl-"))
        tasks = [
            DownloadTask(url=f"{base}/one.bin", path=dest_dir / "one.bin", sha1=sha_one, size=len(payload)),
            DownloadTask(url=f"{base}/two.bin", path=dest_dir / "two.bin", sha1=sha_two, size=1000),
        ]
        downloader = Downloader(selector, max_workers=4, retries=1)
        snapshots: list = []
        report = downloader.run(tasks, progress=snapshots.append, label="自检下载")
        assert report.failed == 0, report.errors
        assert report.ok == 2, (report.ok, report.skipped)
        assert (dest_dir / "one.bin").read_bytes() == payload
        assert hashlib.sha1((dest_dir / "one.bin").read_bytes()).hexdigest() == sha_one

        # 第二次：已存在且校验通过 → 全部跳过
        report2 = Downloader(selector, max_workers=4).run(tasks)
        assert report2.skipped == 2 and report2.ok == 0, (report2.skipped, report2.ok)

        # 断点续传：预置半截 .part 文件，应只补后半段
        broken = dest_dir / "resume.bin"
        part = broken.with_name(broken.name + ".part")
        part.write_bytes(payload[:120_000])
        report3 = Downloader(selector, max_workers=2, retries=1).run(
            [DownloadTask(url=f"{base}/one.bin", path=broken, sha1=sha_one, size=len(payload))]
        )
        assert report3.ok == 1, report3.errors
        assert broken.read_bytes() == payload, "断点续传结果必须与源文件一致"
        assert not part.exists(), ".part 应在完成后被改名"

        # 校验失败 → 记一次失败而不是崩溃
        bad = dest_dir / "bad.bin"
        report4 = Downloader(selector, max_workers=2, retries=0).run(
            [DownloadTask(url=f"{base}/one.bin", path=bad, sha1="0" * 40, size=len(payload))]
        )
        assert report4.failed == 1 and not bad.exists(), report4.errors
    finally:
        server.shutdown()
        server.server_close()


# --------------------------------------------------------------------------- #
# 12. 账号与用户名
# --------------------------------------------------------------------------- #
@test("离线账号与 UUID 规则")
def test_account() -> None:
    account = offline_account("Notch")
    assert account.uuid == offline_uuid("Notch")
    assert len(account.uuid) == 36 and account.uuid.count("-") == 4
    assert account.user_type == "legacy"
    assert offline_uuid("Notch") == "b50ad385-829d-3141-a216-7e7d7539ba7f", offline_uuid("Notch")
    assert validate_username("ab") != ""
    assert validate_username("good_name123") == ""
    assert validate_username("坏名字") != ""


# --------------------------------------------------------------------------- #
# 在线测试
# --------------------------------------------------------------------------- #
def _online_selector() -> tuple[MirrorSelector, AppPaths]:
    paths = _paths()
    settings = Settings(paths)
    settings.download_source = "auto"
    return MirrorSelector(settings, paths), paths


@test("在线：测速与版本清单")
def test_online_manifest() -> None:
    selector, paths = _online_selector()
    probes = selector.probe_all(force=True)
    available = [source for source, probe in probes.items() if probe.ok]
    assert available, f"官方源与镜像源都不可用：{ {k: v.error for k, v in probes.items()} }"
    print(f"    测速结果：{selector.describe_sources()}")

    from lumen.core.manifest import VersionDetail, fetch_version_json, load_manifest

    manifest = load_manifest(selector, paths, force=True)
    assert len(manifest) > 500, len(manifest)
    counts = manifest.counts()
    print(f"    版本总数 {len(manifest)}（release={counts['release']}, snapshot={counts['snapshot']}, old={counts['old']}）")
    latest = manifest.latest_of("release")
    assert latest is not None, "缺少最新正式版"
    data = fetch_version_json(selector, paths, latest)
    detail = VersionDetail(data, version_id=latest.id)
    assert detail.main_class, "mainClass 为空"
    classpath, natives = detail.classpath_and_natives()
    assert len(classpath) > 5, len(classpath)
    print(f"    {latest.id}: mainClass={detail.main_class} 库={len(classpath)} natives={len(natives)} java={detail.java_major}")


@test("在线：真实下载与 SHA1 校验")
def test_online_download() -> None:
    selector, paths = _online_selector()
    target = Path(tempfile.mkdtemp(dir=_TMP_ROOT, prefix="online-")) / "gson.jar"
    task = DownloadTask(
        url="https://libraries.minecraft.net/com/google/code/gson/gson/2.10.1/gson-2.10.1.jar",
        path=target,
        size=283367,
        label="gson-2.10.1.jar",
    )
    report = Downloader(selector, max_workers=2, retries=2).run([task], label="在线下载")
    assert report.failed == 0, report.errors
    assert target.stat().st_size == 283367, target.stat().st_size
    with zipfile.ZipFile(target) as zf:
        assert any(name.endswith(".class") for name in zf.namelist()), "下载到的不是有效 jar"
    print(f"    下载完成：{target.stat().st_size} 字节，SHA1={hashlib.sha1(target.read_bytes()).hexdigest()[:12]}…")


@test("在线：Java 运行时清单")
def test_online_runtime() -> None:
    selector, paths = _online_selector()
    from lumen.java.runtime import fetch_runtime_index, find_package

    index = fetch_runtime_index(selector, paths, force=True)
    package = find_package(index, component=None, major=21)
    assert package is not None, "Mojang 清单里找不到 Java 21 运行时"
    print(f"    Java 21 运行时：{package.label}")


# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description="Lumen Launcher 自检")
    parser.add_argument("--online", action="store_true", help="包含联网测试（清单/测速/下载）")
    parser.add_argument("--keep", action="store_true", help="保留临时目录便于排查")
    args = parser.parse_args()

    selected = [item for item in TESTS if args.online or not item[0].startswith("在线")]
    print(f"Lumen Launcher 自检 · 共 {len(selected)} 项 · 临时数据：{_TMP_ROOT}\n")
    passed = 0
    failed: list[tuple[str, str]] = []
    for name, fn in selected:
        try:
            fn()
            passed += 1
            print(f"  [PASS] {name}")
        except Exception as exc:  # noqa: BLE001
            failed.append((name, f"{exc.__class__.__name__}: {exc}"))
            print(f"  [FAIL] {name}")
            traceback.print_exc(limit=4)
    print(f"\n结果：{passed}/{len(selected)} 通过")
    if failed:
        print("失败项：")
        for name, reason in failed:
            print(f"  - {name}: {reason}")
    if not args.keep:
        import shutil

        shutil.rmtree(_TMP_ROOT, ignore_errors=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
