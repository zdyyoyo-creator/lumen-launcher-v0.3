"""微软正版登录对话框：填写 client_id → 设备码 → 等待授权 → 完成。

不索取密码：走的是微软官方的设备码授权（device code flow），
用户在自己浏览器里完成登录，启动器只拿到 Minecraft 访问令牌。
"""

from __future__ import annotations

import threading

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLineEdit, QVBoxLayout, QWidget

from ...auth.microsoft import DeviceCodeFlow, MicrosoftAuth, SignInResult
from ...utils.errors import LumenError
from ...utils.log import get as get_logger
from ..context import AppContext
from ..widgets import Card, GlyphWidget, SettingRow, label, make_button
from ..workers import Worker
from .common import BaseDialog, MessageDialog

log = get_logger("ui.microsoft")

AZURE_PORTAL_URL = "https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade"
VERIFY_URL = "https://www.microsoft.com/link"


class MicrosoftLoginDialog(BaseDialog):
    """设备码登录向导。登录成功后直接写入 AppContext 的账号。"""

    def __init__(self, parent: QWidget | None, *, ctx: AppContext) -> None:
        super().__init__(parent, title="微软正版登录", width=620)
        self.ctx = ctx
        self.signed_in = False
        self._flow: DeviceCodeFlow | None = None
        self._auth: MicrosoftAuth | None = None
        self._worker: Worker | None = None
        self._cancel = threading.Event()

        self._build()
        self._finish_footer()

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        intro = Card(margins=(16, 14, 16, 14), spacing=6, flat=True)
        head = QHBoxLayout()
        head.setSpacing(10)
        head.addWidget(GlyphWidget("account", 20, width=1.6))
        head.addWidget(label("用微软账号登录 Minecraft: Java 版", "subtitle"), 1)
        intro.body().addLayout(head)
        intro.body().addWidget(
            label(
                "登录使用微软官方「设备码」授权：不会索取你的密码，启动器只保存 Minecraft 访问令牌。",
                "muted",
                wrap=True,
            )
        )
        self.body().addWidget(intro)

        client_row = QWidget()
        client_layout = QHBoxLayout(client_row)
        client_layout.setContentsMargins(0, 0, 0, 0)
        client_layout.setSpacing(8)
        self._client_edit = QLineEdit(self.ctx.msa_client_id())
        self._client_edit.setPlaceholderText("Azure 应用 ID（client_id，形如 00000000-0000-...）")
        self._client_edit.setMinimumWidth(320)
        client_layout.addWidget(self._client_edit)
        client_layout.addWidget(make_button("如何获取", kind="external", handler=self._show_client_help))
        self.body().addWidget(SettingRow("微软应用 ID", "Azure 门户注册的公共客户端应用", client_row))

        self._code_card = Card(margins=(16, 14, 16, 14), spacing=8, flat=True)
        code_row = QHBoxLayout()
        code_row.setSpacing(12)
        code_row.addWidget(label("设备码", "section"))
        self._code_label = label("------", "title")
        code_font = QFont(self._code_label.font())
        code_font.setPixelSize(26)
        code_font.setWeight(QFont.Bold)
        code_font.setLetterSpacing(QFont.AbsoluteSpacing, 4)
        self._code_label.setFont(code_font)
        code_row.addWidget(self._code_label)
        code_row.addStretch(1)
        code_row.addWidget(make_button("复制", kind="copy", handler=self._copy_code))
        self._code_card.body().addLayout(code_row)

        self._uri_label = label("点「开始登录」后，这里会显示设备码与验证地址。", "muted", wrap=True)
        self._code_card.body().addWidget(self._uri_label)

        link_row = QHBoxLayout()
        link_row.setSpacing(8)
        link_row.addWidget(make_button("在浏览器打开", kind="external", handler=self._open_verify_url))
        link_row.addWidget(make_button("换一个设备码", kind="refresh", handler=self._start))
        link_row.addStretch(1)
        self._code_card.body().addLayout(link_row)
        self.body().addWidget(self._code_card)

        self._state = label("准备好后点「开始登录」。", "muted", wrap=True)
        self._state.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.body().addWidget(self._state)

    def _finish_footer(self) -> None:
        self._close_button = self.add_footer_button(make_button("取消", handler=self.reject))
        self._start_button = self.add_footer_button(
            make_button("开始登录", kind="account", role="primary", handler=self._start)
        )
        self._start_button.setDefault(True)
        self.finish()

    # ------------------------------------------------------------------ #
    # 流程
    # ------------------------------------------------------------------ #
    def _start(self) -> None:
        client_id = self._client_edit.text().strip()
        if not client_id:
            MessageDialog.tell(
                self,
                "需要微软应用 ID",
                "微软要求每个第三方启动器自带一个 Azure 应用 ID（client_id）。"
                "点「如何获取」按提示 3 分钟就能注册一个。",
                level="warning",
            )
            self._client_edit.setFocus()
            return
        self.ctx.set_msa_client_id(client_id)
        self._auth = MicrosoftAuth(client_id)
        self._cancel.clear()
        self._signed_in_reset()
        self._set_busy(True, "正在向微软申请设备码…")
        self._run(lambda: self._auth.request_device_code(), "申请设备码", self._on_device_code)

    def _run(self, fn, label_text: str, on_ok) -> None:  # noqa: ANN001
        worker = Worker(lambda **_: fn(), label=label_text)
        worker.succeeded.connect(on_ok)
        worker.failed.connect(self._on_failed)
        self._worker = worker
        worker.start()

    def _signed_in_reset(self) -> None:
        self.signed_in = False
        self._start_button.setText("开始登录")
        self._start_button.setVisible(True)
        self._close_button.setText("取消")

    def _on_device_code(self, flow: DeviceCodeFlow) -> None:
        if not isinstance(flow, DeviceCodeFlow):
            return
        self._flow = flow
        self._code_card.setVisible(True)
        self._code_label.setText(flow.user_code)
        self._uri_label.setText(f"打开 {flow.verification_uri}，输入上面的设备码（{flow.expires_text}）")
        self._set_busy(False, f"等待你在浏览器里完成授权…（{flow.expires_text}）")
        assert self._auth is not None
        self._run(
            lambda: self._auth.sign_in(flow, should_cancel=self._cancel.is_set),
            "等待微软授权",
            self._on_signed_in,
        )

    def _on_signed_in(self, result: SignInResult) -> None:
        if self._cancel.is_set():
            return
        if not isinstance(result, SignInResult):
            return
        account = self.ctx.apply_sign_in(result)
        self.signed_in = True
        self._code_label.setText("已登录")
        self._uri_label.setText(f"{account.username} · {account.uuid}")
        self._state.setText("登录成功，现在可以进入正版服务器了。")
        self._start_button.setVisible(False)
        self._close_button.setText("完成")
        self.ctx.toast.emit(f"已登录正版账号 {account.username}")
        log.info("正版登录完成：%s", account.username)

    def _on_failed(self, message: str, hint: str) -> None:
        self._set_busy(False, "")
        if self._cancel.is_set():
            return
        self._state.setText(f"{message}" + (f"\n{hint}" if hint else ""))
        if not self.signed_in:
            self._start_button.setText("重试")

    # ------------------------------------------------------------------ #
    # 小工具
    # ------------------------------------------------------------------ #
    def _set_busy(self, busy: bool, message: str) -> None:
        self._start_button.setEnabled(not busy)
        self._client_edit.setEnabled(not busy)
        if message:
            self._state.setText(message)

    def _copy_code(self) -> None:
        code = self._code_label.text()
        if code and code != "------":
            QApplication.clipboard().setText(code)
            self._state.setText("设备码已复制到剪贴板")

    def _open_verify_url(self) -> None:
        url = self._flow.verification_uri if self._flow is not None else VERIFY_URL
        QDesktopServices.openUrl(QUrl(url))

    def _show_client_help(self) -> None:
        MessageDialog.tell(
            self,
            "如何获取微软应用 ID",
            "1) 打开 Azure 门户 → Microsoft Entra ID → 应用注册 → 新注册；\n"
            "2) 账户类型选「仅此组织目录中的账户」也可，平台选「移动和桌面应用」；\n"
            "3) 在「身份验证」里把「允许公共客户端流」设为是（不需要填重定向 URI）；\n"
            "4) 回到「概述」复制「应用程序(客户端) ID」，粘贴到这里即可。\n"
            "5) API 权限里添加 Microsoft Graph → 委托 → XboxLive.signin、offline_access。",
            details=f"注册入口：{AZURE_PORTAL_URL}",
            level="info",
        )

    # ------------------------------------------------------------------ #
    def reject(self) -> None:
        self._stop_worker()
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._stop_worker()
        super().closeEvent(event)

    def _stop_worker(self) -> None:
        self._cancel.set()
        worker = self._worker
        if worker is not None and worker.isRunning():
            worker.wait(3000)
