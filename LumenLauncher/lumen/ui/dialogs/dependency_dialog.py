"""前置依赖提示：下载完模组后列出缺失的 required 依赖，可一键下载到实例 mods/。

设计取舍：
* 只提示 ``required``（可选依赖、内置库、工具、明确不兼容的都不打扰用户）；
* 每个依赖都留一个「网页」按钮——解析失败或作者禁止第三方下载时可以手动补；
* 下载复用 ``core.downloader``，与资源页走同一条 SHA1 校验链路。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from ...core.catalog import DependencyInfo
from ...core.downloader import DownloadTask, Downloader
from ...core.instances import Instance
from ...utils.fsutil import ensure_dir
from ...utils.log import get as get_logger
from ..context import AppContext
from ..widgets import Badge, Card, GlyphWidget, label, make_button
from ..workers import TaskManager, Worker
from .common import BaseDialog, MessageDialog

log = get_logger("ui.dependencies")


class DependencyRow(Card):
    """一行依赖：标题 + 版本说明 + 网页 / 下载按钮。"""

    def __init__(self, parent: QWidget | None, *, info: DependencyInfo, dialog: "DependencyDialog") -> None:
        super().__init__(parent, margins=(12, 10, 12, 10), spacing=4, flat=True)
        self.info = info
        self.state = "idle"
        self._dialog = dialog

        row = QHBoxLayout()
        row.setSpacing(10)
        text_box = QVBoxLayout()
        text_box.setSpacing(2)
        text_box.addWidget(label(info.title, "body"))
        text_box.addWidget(label(self._describe(), "muted", wrap=True))
        row.addLayout(text_box, 1)

        self._badge = Badge("必需", "warning")
        row.addWidget(self._badge)
        row.addWidget(make_button("网页", kind="external", handler=self._open_web))
        self._download_button = make_button("下载", kind="download", role="primary", handler=self._download)
        row.addWidget(self._download_button)
        self.body().addLayout(row)

        if info.version is None:
            self._download_button.setEnabled(False)
            self._download_button.setToolTip(info.note or "没有匹配当前实例的文件")

    def _describe(self) -> str:
        info = self.info
        if info.version is not None:
            bits = [info.version.label]
            if info.version.filename:
                bits.append(info.version.filename)
            return " · ".join(bits)
        return info.note or "没有找到匹配当前实例的可用文件，可点「网页」手动下载"

    def _open_web(self) -> None:
        if self.info.page_url:
            QDesktopServices.openUrl(QUrl(self.info.page_url))

    def _download(self) -> None:
        self._dialog.download(self.info)

    def set_state(self, state: str) -> None:
        self.state = state
        if state == "downloading":
            self._download_button.setEnabled(False)
            self._badge.set_kind("primary")
            self._badge.set_text("下载中")
        elif state == "done":
            self._download_button.setText("已安装")
            self._download_button.setEnabled(False)
            self._badge.set_kind("success")
            self._badge.set_text("已安装")
        elif state == "failed":
            self._download_button.setEnabled(True)
            self._download_button.setText("重试")
            self._badge.set_kind("danger")
            self._badge.set_text("失败")


class DependencyDialog(BaseDialog):
    """下载完模组后提示缺失的前置依赖。"""

    def __init__(
        self,
        parent: QWidget | None,
        *,
        ctx: AppContext,
        tasks: TaskManager,
        instance: Instance,
        infos: list[DependencyInfo],
        source_label: str = "",
        mod_title: str = "",
    ) -> None:
        super().__init__(parent, title="缺少前置模组", width=660)
        self.ctx = ctx
        self.tasks = tasks
        self.instance = instance
        self._infos = infos
        self._rows: dict[str, DependencyRow] = {}
        self._workers: list[Worker] = []
        self._build(source_label, mod_title)

    # ------------------------------------------------------------------ #
    def _build(self, source_label: str, mod_title: str) -> None:
        head = QHBoxLayout()
        head.setSpacing(12)
        head.addWidget(GlyphWidget("warning", 22, width=1.7), 0, Qt.AlignTop)
        text_box = QVBoxLayout()
        text_box.setSpacing(4)
        if mod_title:
            headline = f"「{mod_title}」需要 {len(self._infos)} 个前置模组"
        else:
            headline = f"这个模组需要 {len(self._infos)} 个前置模组"
        text_box.addWidget(label(headline, "subtitle", wrap=True))
        text_box.addWidget(
            label(
                "前置缺失时游戏通常会直接崩溃或模组不加载。可以直接在这里装好，也可以记下名字稍后自己装。",
                "muted",
                wrap=True,
            )
        )
        head.addLayout(text_box, 1)
        self._layout.addLayout(head)

        for info in self._infos:
            row = DependencyRow(None, info=info, dialog=self)
            self._rows[info.dependency.project_id] = row
            self._layout.addWidget(row)

        target_text = f"目标实例：{self.instance.name}（{self.instance.mc_version} · {self.instance.display_loader}）"
        if source_label:
            target_text = f"来源：{source_label} · {target_text}"
        self._layout.addWidget(label(target_text, "muted", wrap=True))

        self.add_footer_button(make_button("稍后再说", handler=self.accept))
        self._all_button = self.add_footer_button(
            make_button("全部下载", kind="download", role="primary", handler=self.download_all)
        )
        self._all_button.setDefault(True)
        self.finish()

    # ------------------------------------------------------------------ #
    def download(self, info: DependencyInfo) -> None:
        row = self._rows.get(info.dependency.project_id)
        version = info.version
        if version is None or not version.download_url:
            MessageDialog.tell(
                self,
                "没有可下载的文件",
                "这个前置没有匹配当前实例的版本，请点「网页」手动下载。",
                level="warning",
            )
            return
        filename = version.filename or f"{info.slug or info.dependency.project_id}.jar"
        target = ensure_dir(self.instance.mods_dir) / filename
        if target.exists():
            if row is not None:
                row.set_state("done")
            self.ctx.toast.emit(f"{info.title} 已经存在，跳过下载")
            return
        task = DownloadTask(
            url=version.download_url,
            path=target,
            sha1=version.sha1 or None,
            size=version.file_size or None,
            label=filename,
        )
        if row is not None:
            row.set_state("downloading")
        worker = Worker(
            lambda **_: Downloader(self.ctx.selector, max_workers=4, retries=3).run(
                [task], label=f"下载 {info.title}"
            ),
            label=f"下载 {info.title}",
        )
        worker.succeeded.connect(lambda report, i=info: self._on_downloaded(i, report))
        worker.failed.connect(lambda message, hint, i=info: self._on_failed(i, message, hint))
        self._workers.append(worker)
        self.tasks.start(worker)

    def download_all(self) -> None:
        pending = [info for info in self._infos if info.version is not None]
        if not pending:
            MessageDialog.tell(
                self,
                "没有可下载的文件",
                "所有前置都没有匹配当前实例的版本，请点各自的「网页」手动下载。",
                level="warning",
            )
            return
        for info in pending:
            row = self._rows.get(info.dependency.project_id)
            if row is not None and row.state == "done":
                continue
            self.download(info)

    # ------------------------------------------------------------------ #
    def _on_downloaded(self, info: DependencyInfo, report) -> None:  # noqa: ANN001 - DownloadReport
        row = self._rows.get(info.dependency.project_id)
        if getattr(report, "failed", 0):
            if row is not None:
                row.set_state("failed")
            message = report.errors[0][1] if getattr(report, "errors", None) else "下载失败"
            log.warning("前置下载失败：%s（%s）", info.title, message)
            return
        if row is not None:
            row.set_state("done")
        self.ctx.toast.emit(f"已安装前置 {info.title}")
        self.ctx.instances_changed.emit()

    def _on_failed(self, info: DependencyInfo, message: str, hint: str) -> None:
        row = self._rows.get(info.dependency.project_id)
        if row is not None:
            row.set_state("failed")
        log.warning("前置下载异常：%s（%s / %s）", info.title, message, hint)

    # ------------------------------------------------------------------ #
    def reject(self) -> None:
        # 下载本身是后台任务，这里只做个 best-effort 等待；
        # worker 可能已被 TaskManager 回收（RuntimeError），忽略即可。
        for worker in list(self._workers):
            try:
                if worker.isRunning():
                    worker.wait(1500)
            except RuntimeError:
                continue
        super().reject()
