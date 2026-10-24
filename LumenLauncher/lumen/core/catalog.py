"""资源目录客户端：Modrinth（公开 API）与 CurseForge（需要 API Key）。

只负责「查」与「给出下载信息」，不负责落盘；下载统一交给 ``core.downloader``，
整合包导入交给 ``modpack.importer``，这样 UI 层可以复用同一条链路。

Modrinth 文档：https://docs.modrinth.com/api/
CurseForge 文档：https://docs.curseforge.com/rest-api/
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..utils.errors import LumenError, NetworkError
from ..utils.log import get as get_logger
from ..utils.net import get_json

log = get_logger("core.catalog")

MODRINTH_API = "https://api.modrinth.com/v2"
CURSEFORGE_API = "https://api.curseforge.com/v1"
CURSEFORGE_GAME_ID = 432

#: CurseForge classId
CURSEFORGE_CLASS = {"mod": 6, "modpack": 4471, "resourcepack": 12, "shader": 6552}
#: CurseForge modLoaderType
CURSEFORGE_LOADER = {"forge": 1, "fabric": 4, "quilt": 5, "neoforge": 6}
#: CurseForge 作者关闭 API 直链时的 CDN 兜底
CURSEFORGE_CDN = "https://edge.forgecdn.net/files/{a}/{b}/{name}"

SOURCE_LABELS = {"modrinth": "Modrinth", "curseforge": "CurseForge"}
TYPE_LABELS = {"mod": "模组", "modpack": "整合包", "resourcepack": "资源包", "shader": "光影"}


@dataclass
class CatalogHit:
    """搜索结果的统一表示（两个平台字段差异都收敛到这里）。"""

    source: str
    project_id: str
    slug: str = ""
    title: str = ""
    description: str = ""
    author: str = ""
    downloads: int = 0
    follows: int = 0
    icon_url: str = ""
    categories: list[str] = field(default_factory=list)
    game_versions: list[str] = field(default_factory=list)
    loaders: list[str] = field(default_factory=list)
    project_type: str = "mod"

    @property
    def source_label(self) -> str:
        return SOURCE_LABELS.get(self.source, self.source)

    @property
    def downloads_text(self) -> str:
        value = float(self.downloads)
        if value >= 1_000_000:
            return f"{value / 1_000_000:.1f}M"
        if value >= 1_000:
            return f"{value / 1_000:.1f}K"
        return str(int(value))


@dataclass
class CatalogDependency:
    """资源声明的依赖（前置模组）。

    * Modrinth：``dependencies[].dependency_type`` = required / optional / incompatible / embedded
    * CurseForge：``dependencies[].relationType`` = 1 embedded / 2 optional / 3 required / 4 tool / 5 incompatible
    """

    project_id: str = ""
    version_id: str = ""
    kind: str = "required"
    source: str = ""

    @property
    def is_required(self) -> bool:
        return self.kind == "required"


@dataclass
class DependencyInfo:
    """解析后的依赖：带标题、网页链接与可直接下载的文件版本。"""

    dependency: CatalogDependency
    title: str = ""
    slug: str = ""
    page_url: str = ""
    version: "CatalogVersion | None" = None
    note: str = ""


@dataclass
class CatalogVersion:
    """某个资源的一个具体文件版本（可直接下载）。"""

    version_id: str
    name: str = ""
    version_number: str = ""
    game_versions: list[str] = field(default_factory=list)
    loaders: list[str] = field(default_factory=list)
    date: str = ""
    download_url: str = ""
    filename: str = ""
    file_size: int = 0
    sha1: str = ""
    release_type: str = "release"
    project_type: str = "mod"
    dependencies: list[CatalogDependency] = field(default_factory=list)

    @property
    def label(self) -> str:
        bits = [self.version_number or self.name or self.version_id]
        if self.game_versions:
            bits.append("MC " + "/".join(self.game_versions[:2]))
        if self.loaders:
            bits.append("/".join(self.loaders))
        return " · ".join(bits)

    @property
    def required_dependencies(self) -> list[CatalogDependency]:
        return [dep for dep in self.dependencies if dep.is_required]


class CatalogError(LumenError):
    """目录接口返回了不可用的结果（需要 API Key、被限流等）。"""


# --------------------------------------------------------------------------- #
# Modrinth
# --------------------------------------------------------------------------- #
class ModrinthClient:
    source = "modrinth"

    def __init__(self, api_key: str = "") -> None:
        self.api_key = api_key
        self.headers = {"User-Agent": "LumenLauncher/1.0 (modrinth catalog)"}

    # ------------------------------------------------------------------ #
    def search(
        self,
        query: str,
        *,
        project_type: str = "mod",
        loader: str = "",
        game_version: str = "",
        limit: int = 20,
        offset: int = 0,
        sort: str = "relevance",
    ) -> list[CatalogHit]:
        facets: list[list[str]] = [[f"project_type:{project_type}"]]
        if loader:
            facets.append([f"categories:{loader}"])
        if game_version:
            facets.append([f"versions:{game_version}"])
        params = {
            "query": query,
            "limit": max(1, min(50, limit)),
            "offset": max(0, offset),
            "index": sort,
            "facets": json.dumps(facets, separators=(",", ":")),
        }
        try:
            payload = get_json(f"{MODRINTH_API}/search", params=params, headers=self.headers, retries=1)
        except NetworkError as exc:
            raise CatalogError(f"Modrinth 搜索失败：{exc}", hint=exc.hint) from exc
        hits: list[CatalogHit] = []
        for raw in payload.get("hits") or []:
            if not isinstance(raw, dict):
                continue
            hits.append(
                CatalogHit(
                    source="modrinth",
                    project_id=str(raw.get("project_id") or ""),
                    slug=str(raw.get("slug") or ""),
                    title=str(raw.get("title") or ""),
                    description=str(raw.get("description") or ""),
                    author=str(raw.get("author") or ""),
                    downloads=int(raw.get("downloads") or 0),
                    follows=int(raw.get("follows") or 0),
                    icon_url=str(raw.get("icon_url") or ""),
                    categories=[str(c) for c in (raw.get("display_categories") or raw.get("categories") or [])],
                    game_versions=[str(v) for v in (raw.get("versions") or [])],
                    loaders=[str(v) for v in (raw.get("categories") or [])],
                    project_type=str(raw.get("project_type") or project_type),
                )
            )
        return hits

    # ------------------------------------------------------------------ #
    def versions(
        self,
        project_id: str,
        *,
        loader: str = "",
        game_version: str = "",
        limit: int = 60,
    ) -> list[CatalogVersion]:
        params: dict[str, Any] = {}
        if loader:
            params["loaders"] = json.dumps([loader], separators=(",", ":"))
        if game_version:
            params["game_versions"] = json.dumps([game_version], separators=(",", ":"))
        try:
            payload = get_json(
                f"{MODRINTH_API}/project/{project_id}/version",
                params=params or None,
                headers=self.headers,
                retries=1,
                expect=list,
            )
        except NetworkError as exc:
            raise CatalogError(f"Modrinth 版本列表获取失败：{exc}", hint=exc.hint) from exc
        versions: list[CatalogVersion] = []
        for raw in payload:
            if not isinstance(raw, dict):
                continue
            files = raw.get("files") or []
            primary = next((f for f in files if isinstance(f, dict) and f.get("primary")), files[0] if files else None)
            if not isinstance(primary, dict):
                continue
            hashes = primary.get("hashes") or {}
            versions.append(
                CatalogVersion(
                    version_id=str(raw.get("id") or ""),
                    name=str(raw.get("name") or ""),
                    version_number=str(raw.get("version_number") or ""),
                    game_versions=[str(v) for v in (raw.get("game_versions") or [])],
                    loaders=[str(v) for v in (raw.get("loaders") or [])],
                    date=str(raw.get("date_published") or ""),
                    download_url=str(primary.get("url") or ""),
                    filename=str(primary.get("filename") or ""),
                    file_size=int(primary.get("size") or 0),
                    sha1=str(hashes.get("sha1") or ""),
                    release_type=str(raw.get("version_type") or "release"),
                    dependencies=_modrinth_dependencies(raw),
                )
            )
            if len(versions) >= limit:
                break
        return versions

    # ------------------------------------------------------------------ #
    def projects(self, project_ids: list[str]) -> list[dict]:
        """批量取项目信息（``/projects?ids=[...]``），用于补全依赖标题与链接。"""
        ids = [str(item) for item in project_ids if item]
        if not ids:
            return []
        try:
            payload = get_json(
                f"{MODRINTH_API}/projects",
                params={"ids": json.dumps(ids, separators=(",", ":"))},
                headers=self.headers,
                retries=1,
                expect=list,
            )
        except NetworkError as exc:
            raise CatalogError(f"Modrinth 项目信息获取失败：{exc}", hint=exc.hint) from exc
        return [item for item in payload if isinstance(item, dict)]

    # ------------------------------------------------------------------ #
    def project(self, project_id: str) -> dict:
        try:
            payload = get_json(
                f"{MODRINTH_API}/project/{project_id}", headers=self.headers, retries=1
            )
        except NetworkError as exc:
            raise CatalogError(f"Modrinth 项目信息获取失败：{exc}", hint=exc.hint) from exc
        return payload


# --------------------------------------------------------------------------- #
# CurseForge
# --------------------------------------------------------------------------- #
class CurseForgeClient:
    source = "curseforge"

    def __init__(self, api_key: str = "") -> None:
        self.api_key = (api_key or "").strip()

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise CatalogError(
                "CurseForge 需要 API Key",
                hint="到「设置 → 整合包」填入 CurseForge API Key 后即可搜索与下载（Modrinth 无需 Key）。",
            )
        return {"x-api-key": self.api_key, "Accept": "application/json"}

    def _request(self, path: str, *, params: dict | None = None, expect=dict):
        try:
            return get_json(
                f"{CURSEFORGE_API}{path}",
                params=params,
                headers=self._headers(),
                retries=1,
                expect=expect,
            )
        except NetworkError as exc:
            message = str(exc)
            if "403" in message or "401" in message:
                raise CatalogError(
                    "CurseForge API Key 被拒绝",
                    hint="请检查 Key 是否填写正确、是否被限流（403/401）。",
                ) from exc
            raise CatalogError(f"CurseForge 请求失败：{message}", hint=exc.hint) from exc

    # ------------------------------------------------------------------ #
    def search(
        self,
        query: str,
        *,
        project_type: str = "mod",
        loader: str = "",
        game_version: str = "",
        limit: int = 20,
        offset: int = 0,
        sort: str = "popularity",
    ) -> list[CatalogHit]:
        params: dict[str, Any] = {
            "gameId": CURSEFORGE_GAME_ID,
            "classId": CURSEFORGE_CLASS.get(project_type, 6),
            "searchFilter": query,
            "pageSize": max(1, min(50, limit)),
            "index": max(0, offset),
            "sortField": 2 if sort == "popularity" else 6,  # 2=Popularity 6=TotalDownloads
            "sortOrder": "desc",
        }
        if game_version:
            params["gameVersion"] = game_version
        if loader and loader in CURSEFORGE_LOADER:
            params["modLoaderType"] = CURSEFORGE_LOADER[loader]
        payload = self._request("/mods/search", params=params)
        hits: list[CatalogHit] = []
        for raw in payload.get("data") or []:
            if not isinstance(raw, dict):
                continue
            logo = raw.get("logo") or {}
            categories = [str(c.get("name")) for c in (raw.get("categories") or []) if isinstance(c, dict)]
            hits.append(
                CatalogHit(
                    source="curseforge",
                    project_id=str(raw.get("id") or ""),
                    slug=str(raw.get("slug") or ""),
                    title=str(raw.get("name") or ""),
                    description=str(raw.get("summary") or ""),
                    author="",
                    downloads=int(raw.get("downloadCount") or 0),
                    icon_url=str(logo.get("thumbnailUrl") or logo.get("url") or ""),
                    categories=categories,
                    game_versions=_curse_game_versions(raw),
                    loaders=[loader] if loader else [],
                    project_type=project_type,
                )
            )
        return hits

    # ------------------------------------------------------------------ #
    def versions(
        self,
        project_id: str,
        *,
        loader: str = "",
        game_version: str = "",
        limit: int = 60,
    ) -> list[CatalogVersion]:
        params: dict[str, Any] = {"pageSize": min(50, limit)}
        if game_version:
            params["gameVersion"] = game_version
        if loader and loader in CURSEFORGE_LOADER:
            params["modLoaderType"] = CURSEFORGE_LOADER[loader]
        payload = self._request(f"/mods/{project_id}/files", params=params)
        versions: list[CatalogVersion] = []
        for raw in payload.get("data") or []:
            if not isinstance(raw, dict):
                continue
            download_url = str(raw.get("downloadUrl") or "")
            filename = str(raw.get("fileName") or "")
            file_id = int(raw.get("id") or 0)
            if not download_url and filename:
                download_url = CURSEFORGE_CDN.format(a=file_id // 1000, b=file_id % 1000, name=filename)
            sha1 = ""
            for entry in raw.get("hashes") or []:
                if int(entry.get("algo") or 0) == 1:
                    sha1 = str(entry.get("value") or "")
            versions.append(
                CatalogVersion(
                    version_id=str(file_id),
                    name=str(raw.get("displayName") or ""),
                    version_number=str(raw.get("displayName") or filename),
                    game_versions=[str(v) for v in (raw.get("gameVersions") or [])],
                    loaders=[loader] if loader else [],
                    date=str(raw.get("fileDate") or ""),
                    download_url=download_url,
                    filename=filename,
                    file_size=int(raw.get("fileLength") or 0),
                    sha1=sha1,
                    release_type={1: "release", 2: "beta", 3: "alpha"}.get(int(raw.get("releaseType") or 1), "release"),
                    dependencies=_curse_dependencies(raw),
                )
            )
        return versions[:limit]

    # ------------------------------------------------------------------ #
    def project(self, project_id: str) -> dict:
        """取单个项目信息（依赖标题 / 网页链接）。"""
        payload = self._request(f"/mods/{project_id}")
        data = payload.get("data") if isinstance(payload, dict) else None
        return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------- #
def make_client(source: str, *, curseforge_key: str = "") -> ModrinthClient | CurseForgeClient:
    if source == "curseforge":
        return CurseForgeClient(curseforge_key)
    return ModrinthClient()


def pick_best_version(
    versions: list[CatalogVersion],
    *,
    loader: str = "",
    game_version: str = "",
) -> CatalogVersion | None:
    """从版本列表里挑一个最适合当前实例的（稳定版优先、版本号靠前）。"""

    def score(version: CatalogVersion) -> tuple:
        stable = 0 if version.release_type == "release" else 1
        loader_ok = 0 if (not loader or loader in version.loaders or not version.loaders) else 1
        mc_ok = 0 if (not game_version or game_version in version.game_versions or not version.game_versions) else 1
        return (loader_ok, mc_ok, stable, -_date_rank(version.date))

    usable = [v for v in versions if v.download_url]
    if not usable:
        return None
    return min(usable, key=score)


def _date_rank(text: str) -> float:
    """把 ISO 时间转成可比较的数字（越大越新）。"""
    digits = "".join(ch for ch in str(text) if ch.isdigit())
    try:
        return float(digits[:14] or 0)
    except ValueError:
        return 0.0


#: CurseForge relationType → 依赖类型
_CURSE_RELATIONS = {1: "embedded", 2: "optional", 3: "required", 4: "tool", 5: "incompatible", 6: "include"}


def _modrinth_dependencies(raw: dict) -> list[CatalogDependency]:
    deps: list[CatalogDependency] = []
    for item in raw.get("dependencies") or []:
        if not isinstance(item, dict):
            continue
        project_id = str(item.get("project_id") or "")
        if not project_id:
            continue
        deps.append(
            CatalogDependency(
                source="modrinth",
                project_id=project_id,
                version_id=str(item.get("version_id") or ""),
                kind=str(item.get("dependency_type") or "required"),
            )
        )
    return deps


def _curse_dependencies(raw: dict) -> list[CatalogDependency]:
    deps: list[CatalogDependency] = []
    for item in raw.get("dependencies") or []:
        if not isinstance(item, dict):
            continue
        mod_id = str(item.get("modId") or "")
        if not mod_id:
            continue
        relation = int(item.get("relationType") or 0)
        if relation not in (2, 3):
            # 1=内置库 / 4=工具 / 5=不兼容 / 6=捆绑，都不该当"前置"提示
            continue
        deps.append(
            CatalogDependency(
                source="curseforge",
                project_id=mod_id,
                kind=_CURSE_RELATIONS.get(relation, "optional"),
            )
        )
    return deps


def resolve_dependencies(
    client: "ModrinthClient | CurseForgeClient",
    dependencies: list[CatalogDependency],
    *,
    loader: str = "",
    game_version: str = "",
    limit: int = 12,
) -> list[DependencyInfo]:
    """把依赖声明补全成「标题 + 网页链接 + 可下载版本」。

    只处理 required（可选/内置依赖不提示）；任何一项失败都只是缺标题或缺下载按钮，
    不影响其它依赖。
    """
    required = [dep for dep in dependencies if dep.is_required][:limit]
    if not required:
        return []
    infos: list[DependencyInfo] = []

    if isinstance(client, ModrinthClient):
        by_id: dict[str, dict] = {}
        try:
            for item in client.projects([dep.project_id for dep in required]):
                by_id[str(item.get("id") or "")] = item
        except LumenError as exc:
            log.debug("批量获取 Modrinth 项目信息失败：%s", exc)
        for dep in required:
            project = by_id.get(dep.project_id) or {}
            slug = str(project.get("slug") or "")
            info = DependencyInfo(
                dependency=dep,
                title=str(project.get("title") or "") or dep.project_id,
                slug=slug,
                page_url=f"https://modrinth.com/mod/{slug}" if slug else "",
            )
            try:
                versions = client.versions(dep.project_id, loader=loader, game_version=game_version)
                info.version = pick_best_version(versions, loader=loader, game_version=game_version)
            except LumenError as exc:
                info.note = f"获取版本失败：{exc.message}"
            if info.version is None:
                info.note = info.note or "没有匹配当前实例的可用文件"
            infos.append(info)
        return infos

    for dep in required:
        info = DependencyInfo(dependency=dep)
        try:
            data = client.project(dep.project_id)
            slug = str(data.get("slug") or "")
            info.title = str(data.get("name") or "") or dep.project_id
            info.slug = slug
            info.page_url = (
                f"https://www.curseforge.com/minecraft/mc-mods/{slug}"
                if slug
                else f"https://www.curseforge.com/projects/{dep.project_id}"
            )
        except LumenError as exc:
            info.note = f"获取项目信息失败：{exc.message}"
        try:
            versions = client.versions(dep.project_id, loader=loader, game_version=game_version)
            info.version = pick_best_version(versions, loader=loader, game_version=game_version)
        except LumenError as exc:
            info.note = f"{info.note} 版本获取失败：{exc.message}".strip()
        if info.version is None:
            info.note = info.note or "没有匹配当前实例的可用文件"
        infos.append(info)
    return infos


def _curse_game_versions(raw: dict, *, limit: int = 8) -> list[str]:
    """从 CurseForge 搜索结果里提取适配的 Minecraft 版本。

    ``gameVersionLatestFiles`` 是对象数组（``[{"gameVersion": "1.21.4", ...}]``），
    早先按字符串过滤，导致这一列永远是空的。
    """
    versions: list[str] = []
    entries = raw.get("gameVersionLatestFiles") or []
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            value = str(entry.get("gameVersion") or "").strip()
            if value and value not in versions:
                versions.append(value)
            if len(versions) >= limit:
                break
    if not versions:
        # 少数接口直接给字符串数组，顺手兼容
        for value in raw.get("gameVersions") or raw.get("versions") or []:
            text = str(value or "").strip()
            if text and text not in versions:
                versions.append(text)
            if len(versions) >= limit:
                break
    return versions
