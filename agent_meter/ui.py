import copy
import json
import sys
import time
import threading
from pathlib import Path
from PySide6.QtCore import Qt, QTimer, Signal, QObject, QRunnable, QThreadPool, QMimeData, QPoint, QDateTime, QSize
from PySide6.QtGui import QColor, QPainter, QPen, QFont, QDrag, QIcon, QPixmap, QAction, QPalette, QPainterPath
from PySide6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QFrame, QDialog, QTabWidget, QFormLayout, QCheckBox, QComboBox,
    QSlider, QSpinBox, QLineEdit, QDialogButtonBox, QMessageBox, QProgressBar,
    QSystemTrayIcon, QMenu, QDoubleSpinBox, QDateTimeEdit)
from .model import PROVIDERS, Snapshot, effective_status, merge_snapshot, percent, reset_text
from .storage import validate_proxy, provider_network
from .providers import collect
from .claude_bridge import install_bridge, uninstall_bridge
from .connections import connection_description, USAGE_URLS, open_local_cursor, codex_login
from .claude_oauth import begin_login, claude_client, safe_login_error
from .doubao import start_connection, DoubaoError

MIME = 'application/x-agent-meter-provider'
FONT_FAMILY = 'Helvetica Neue' if sys.platform == 'darwin' else 'Segoe UI'

def clear(layout):
    while layout.count():
        item=layout.takeAt(0)
        if item.widget():
            item.widget().hide()
            item.widget().deleteLater()
        elif item.layout(): clear(item.layout())

def label(text, size=12, color=None, bold=False):
    w=QLabel(text)
    w.setFont(QFont(FONT_FAMILY,size, QFont.Weight.DemiBold if bold else QFont.Weight.Normal))
    if color: w.setStyleSheet('color:'+color+'; background:transparent;')
    w.setTextFormat(Qt.TextFormat.PlainText)
    return w

def button(text, tip, callback, width=None):
    w=QPushButton(text)
    if text in ('⚙','↗','↻','−','×','●','○'):
        w.setFont(QFont('Apple Symbols' if sys.platform == 'darwin' else 'Segoe UI Symbol',10))
    w.setToolTip(tip); w.setAccessibleName(tip)
    if width: w.setFixedWidth(width)
    w.clicked.connect(callback)
    return w

def app_icon():
    return QIcon(str(Path(__file__).resolve().parent/'assets/app-icon.png'))

def app_logo(size):
    logo=QLabel(); logo.setFixedSize(size,size)
    logo.setPixmap(app_icon().pixmap(size,size))
    logo.setAccessibleName('Agent Meter')
    logo.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    return logo

def short_quota_name(name):
    return name.replace(' 小时', 'h').replace(' 天', 'd').replace(' 分钟', 'm')

def quota_tooltip(snapshot):
    lines = [snapshot.message]
    lines.extend(f'{q.name}：剩余 {percent(q)} · {reset_text(q.reset_at)}' for q in snapshot.quotas)
    return '\n'.join(lines)

def pin_icon(pinned,color):
    # Draw directly so the small title-bar icon does not depend on emoji fonts.
    pixmap=QPixmap(48,48); pixmap.setDevicePixelRatio(2); pixmap.fill(Qt.GlobalColor.transparent)
    painter=QPainter(pixmap); painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    if not pinned:
        painter.translate(12,12); painter.rotate(35); painter.translate(-12,-12)
    pen=QPen(QColor(color),1.6); pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin); pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen); painter.setBrush(QColor(color) if pinned else Qt.BrushStyle.NoBrush)
    shape=QPainterPath(); shape.moveTo(8,3)
    for x,y in [(16,3),(16,6),(14,6),(14,11),(17,15),(17,16),(7,16),(7,15),(10,11),(10,6),(8,6)]:
        shape.lineTo(x,y)
    shape.closeSubpath(); painter.drawPath(shape); painter.drawLine(12,16,12,22); painter.end()
    return QIcon(pixmap)

class Signals(QObject):
    result=Signal(str,object)

class Collector(QRunnable):
    def __init__(self, provider, settings, directory):
        super().__init__()
        self.provider=provider; self.settings=copy.deepcopy(settings); self.directory=directory
        self.signals=Signals()
        self.auth_generation=claude_client(directory).generation if provider=='claude' else None
    def run(self):
        result=collect(self.provider,self.settings,self.directory)
        try: self.signals.result.emit(self.provider,result)
        except RuntimeError: pass  # Application has already shut down.

class LoginSignals(QObject):
    url=Signal(str)
    status=Signal(str)
    finished=Signal(bool,str)

class LoginJob(QRunnable):
    def __init__(self,network):
        super().__init__(); self.network=network; self.cancelled=threading.Event(); self.signals=LoginSignals()
    def run(self):
        try: success,message=codex_login(self.network,self.cancelled,self.signals.url.emit,self.signals.status.emit)
        except Exception: success,message=False,'无法启动登录，请检查官方组件和代理设置。'
        try: self.signals.finished.emit(success,message)
        except RuntimeError: pass

class ClaudeLoginJob(QRunnable):
    def __init__(self,client,flow,code,network):
        super().__init__()
        self.client=client; self.flow=flow; self.code=code; self.network=network
        self.cancelled=flow.cancelled; self.signals=LoginSignals()
    def run(self):
        try:
            self.client.exchange(self.code,self.flow,self.network)
            success,message=True,'登录成功 · 正在读取套餐额度…'
        except Exception as error: success,message=False,safe_login_error(error)
        finally: self.code=''
        try: self.signals.finished.emit(success,message)
        except RuntimeError: pass

class DoubaoConnectJob(QRunnable):
    def __init__(self,network):
        super().__init__(); self.network=copy.deepcopy(network); self.signals=LoginSignals()
    def run(self):
        try: success,message=True,start_connection(self.network)
        except DoubaoError as error: success,message=False,str(error)
        except Exception: success,message=False,'无法启动豆包，请检查客户端安装和网络设置。'
        try: self.signals.finished.emit(success,message)
        except RuntimeError: pass

class Surface(QWidget):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner=owner; self.start=None
    def paintEvent(self,event):
        p=QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c=QColor(self.owner.colors['surface']); c.setAlpha(round(255*self.owner.store.settings['opacity']/100))
        p.setBrush(c); p.setPen(QPen(QColor(self.owner.colors['border']),1))
        p.drawRoundedRect(self.rect().adjusted(1,1,-1,-1),18,18)
    def mousePressEvent(self,event):
        if event.button()==Qt.MouseButton.LeftButton: self.start=event.globalPosition().toPoint()-self.owner.pos()
    def mouseMoveEvent(self,event):
        if self.start is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.owner.move(event.globalPosition().toPoint()-self.start)
    def mouseReleaseEvent(self,event):
        if self.start is not None:
            self.start=None; self.owner.save_position()

class Tile(QWidget):
    def __init__(self, owner, provider):
        super().__init__()
        self.owner=owner; self.provider=provider; self.press=None; self.drop_side=None
        self.setFixedSize(156,156); self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        c=owner.colors
        v=QVBoxLayout(self); v.setContentsMargins(12,10,12,9); v.setSpacing(4)
        h=QHBoxLayout(); h.setSpacing(7)
        avatar=label(PROVIDERS[provider][2],14,c['accent'],True); avatar.setFixedWidth(22)
        h.addWidget(avatar)
        names=QVBoxLayout(); names.setSpacing(0)
        names.addWidget(label(PROVIDERS[provider][0],10,c['text'],True))
        names.addWidget(label(PROVIDERS[provider][1],8,c['muted']))
        h.addLayout(names); h.addStretch(); v.addLayout(h)
        self.content=QVBoxLayout(); self.content.setSpacing(4); v.addLayout(self.content)
        self.content_key=None
        self.update_snapshot()
    def update_snapshot(self):
        s=self.owner.snapshots[self.provider]; q=s.primary; status=self.owner.snapshot_status(s)
        c=self.owner.colors; threshold=self.owner.store.settings['warning_threshold']
        self.setAccessibleName(PROVIDERS[self.provider][0]+'，'+quota_tooltip(s)+'，点击查看详情，拖动排序；方向键调整顺序')
        self.setToolTip(quota_tooltip(s)+'\n点击查看详情 · 拖动方格改变顺序')
        multi=len(s.quotas)>1; key=(multi,min(len(s.quotas),3) if multi else 1)
        if key!=self.content_key:
            self.setUpdatesEnabled(False)
            clear(self.content); self.quota_widgets=[]; self.window_caption=None
            for _ in range(key[1]):
                name=label('',8,c['muted']) if multi else None
                value=label('',11 if multi else 25,c['text'],True)
                if multi:
                    row=QHBoxLayout(); row.setSpacing(3); row.addWidget(name); row.addStretch(); row.addWidget(value)
                    self.content.addLayout(row)
                else: self.content.addWidget(value)
                meter=QProgressBar(); meter.setRange(0,1000); meter.setTextVisible(False); meter.setFixedHeight(4)
                self.content.addWidget(meter); self.quota_widgets.append((name,value,meter))
            if not multi:
                self.window_caption=label('',8,c['muted']); self.content.addWidget(self.window_caption)
            self.caption=label('',8,c['muted']); self.content.addWidget(self.caption); self.content.addStretch(0)
            self.content_key=key
            for child in self.findChildren(QWidget): child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            self.setUpdatesEnabled(True)
        for (name,value,meter),quota in zip(self.quota_widgets,s.quotas[:3] or [None]):
            warn=quota is not None and quota.remaining<threshold
            value.setText(percent(quota)); value.setStyleSheet(f'color:{c["warning"] if warn else c["text"]};background:transparent;')
            meter.setValue(round(quota.remaining*10) if quota else 0)
            style=f'QProgressBar {{background:{c["track"]};border:0;border-radius:2px;}} QProgressBar::chunk {{background:{c["warning"] if warn else c["accent"]};border-radius:2px;}}'
            if meter.styleSheet()!=style: meter.setStyleSheet(style)
            if name:
                name.setText(name.fontMetrics().elidedText(short_quota_name(quota.name),Qt.TextElideMode.ElideRight,80))
        if multi:
            if len(s.quotas)>3: caption=f'另有 {len(s.quotas)-3} 项 · 点击查看'
            elif status in ('stale','error'): caption='旧数据 · '+s.message
            else: caption=reset_text(q.reset_at)
        else:
            if status in ('stale','error'): caption='旧数据' if q else '连接失败'
            elif status=='manual': caption='手动录入'
            else: caption=q.name if q else '等待额度' if status=='waiting' else '等待连接'
            self.window_caption.setText(self.window_caption.fontMetrics().elidedText(caption,Qt.TextElideMode.ElideRight,132))
            self.window_caption.setStyleSheet(f'color:{c["warning"] if status in ("stale","error") else c["muted"]};background:transparent;')
            caption=reset_text(q.reset_at) if q and status not in ('stale','error') else s.message
        self.caption.setText(self.caption.fontMetrics().elidedText(caption,Qt.TextElideMode.ElideRight,132))
        self.caption.setStyleSheet(f'color:{c["warning"] if status in ("stale","error") else c["muted"]};background:transparent;')
    def paintEvent(self,event):
        p=QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c=self.owner.colors
        p.setBrush(QColor(c['tile'])); p.setPen(QPen(QColor(c['border']),1))
        p.drawRoundedRect(self.rect().adjusted(1,1,-1,-1),12,12)
        if self.hasFocus():
            p.setBrush(Qt.BrushStyle.NoBrush); p.setPen(QPen(QColor(c['accent']),2))
            p.drawRoundedRect(self.rect().adjusted(2,2,-2,-2),11,11)
        if self.drop_side is not None:
            p.setPen(QPen(QColor(c['accent']),3))
            x=2 if self.drop_side=='before' else self.width()-3
            p.drawLine(x,12,x,self.height()-12)
    def mousePressEvent(self,event):
        if event.button()==Qt.MouseButton.LeftButton:
            self.press=event.position().toPoint(); self.setFocus()
    def mouseMoveEvent(self,event):
        if self.press is None or not event.buttons() & Qt.MouseButton.LeftButton: return
        if (event.position().toPoint()-self.press).manhattanLength()<QApplication.startDragDistance(): return
        drag=QDrag(self); mime=QMimeData(); mime.setData(MIME,self.provider.encode())
        drag.setMimeData(mime); drag.setPixmap(self.grab()); drag.setHotSpot(self.press)
        self.press=None; self.owner.drag_active=True
        try: drag.exec(Qt.DropAction.MoveAction)
        finally:
            self.owner.drag_active=False
            if self.owner.render_pending:
                self.owner.render_pending=False
                QTimer.singleShot(0,self.owner.render)
    def mouseReleaseEvent(self,event):
        if self.press is not None:
            self.press=None; self.owner.show_detail(self.provider)
    def dragEnterEvent(self,event):
        if event.mimeData().hasFormat(MIME): event.acceptProposedAction()
    def dragMoveEvent(self,event):
        self.drop_side='before' if event.position().x()<self.width()/2 else 'after'
        self.update(); event.acceptProposedAction()
    def dragLeaveEvent(self,event): self.drop_side=None; self.update()
    def dropEvent(self,event):
        source=bytes(event.mimeData().data(MIME)).decode()
        side='before' if event.position().x()<self.width()/2 else 'after'
        self.drop_side=None
        self.owner.reorder(source,self.provider,side); event.acceptProposedAction()
    def keyPressEvent(self,event):
        delta={Qt.Key.Key_Left:-1,Qt.Key.Key_Up:-1,Qt.Key.Key_Right:1,Qt.Key.Key_Down:1}.get(event.key())
        if delta is not None:
            order=self.owner.visible_agents(); index=order.index(self.provider); target=index+delta
            if 0<=target<len(order): self.owner.reorder(self.provider,order[target],'before' if delta<0 else 'after')
        elif event.key() in (Qt.Key.Key_Return,Qt.Key.Key_Space): self.owner.show_detail(self.provider)
        else: super().keyPressEvent(event)

class SettingsDialog(QDialog):
    def __init__(self,owner):
        super().__init__(owner,Qt.WindowType.Dialog|Qt.WindowType.WindowStaysOnTopHint)
        self.owner=owner; self.config=copy.deepcopy(owner.store.settings)
        self.setWindowTitle('Agent Meter · 设置'); self.setMinimumWidth(470)
        self.setStyleSheet(owner.dialog_style())
        body=QVBoxLayout(self); tabs=QTabWidget(); body.addWidget(tabs)
        display=QWidget(); form=QFormLayout(display); form.setSpacing(12)
        agents=QWidget(); agent_boxes=QGridLayout(agents); agent_boxes.setContentsMargins(0,0,0,0)
        self.agent_visibility={}
        for i,p in enumerate(self.config['order']):
            box=QCheckBox(PROVIDERS[p][0]); box.setChecked(p in self.config['enabled_agents'])
            self.agent_visibility[p]=box; agent_boxes.addWidget(box,i//2,i%2)
            box.toggled.connect(lambda checked,p=p:self.validate_agents(p,checked))
        form.addRow('显示的 Agent（至少 1 项）',agents)
        form.addRow('',label('取消勾选后隐藏该 Agent，并暂停自动采集和提醒。',9,owner.colors['muted']))
        focus=QWidget(); boxes=QHBoxLayout(focus); boxes.setContentsMargins(0,0,0,0)
        self.focus={}
        for p in self.config['order']:
            box=QCheckBox(PROVIDERS[p][0]); box.setChecked(p in self.config['mini_ids']); self.focus[p]=box
            boxes.addWidget(box); box.toggled.connect(lambda checked,p=p:self.validate_focus(p,checked))
        form.addRow('迷你条重点（1–3 项）',focus)
        self.sync_focus()
        self.layout=QComboBox(); self.layout.addItems(['紧凑方格','横向方格']); self.layout.setCurrentIndex(self.config['layout']=='dock')
        form.addRow('正常窗口',self.layout)
        self.opacity=QSlider(Qt.Orientation.Horizontal); self.opacity.setRange(55,100); self.opacity.setValue(self.config['opacity'])
        self.opacity_label=QLabel(str(self.opacity.value())+'%'); self.opacity.valueChanged.connect(lambda n:self.opacity_label.setText(str(n)+'%'))
        opacity_row=QHBoxLayout(); opacity_row.addWidget(self.opacity); opacity_row.addWidget(self.opacity_label)
        form.addRow('背景不透明度',opacity_row)
        self.theme=QComboBox(); self.theme.addItems(['跟随系统','浅色','深色']); self.theme.setCurrentIndex(['system','light','dark'].index(self.config['theme']))
        form.addRow('主题',self.theme)
        self.pinned=QCheckBox('始终置顶'); self.pinned.setChecked(self.config['pinned']); form.addRow('',self.pinned)
        self.startup=QCheckBox('登录时启动'); self.startup.setChecked(owner.autostart_enabled()); form.addRow('',self.startup)
        self.interval=QSpinBox(); self.interval.setRange(30,600); self.interval.setSuffix(' 秒'); self.interval.setValue(self.config['refresh_seconds'])
        form.addRow('在线额度刷新间隔',self.interval)
        self.alerts=QCheckBox('首次低于阈值或从高额度降到阈值时提醒'); self.alerts.setChecked(self.config['notifications']); form.addRow('',self.alerts)
        self.threshold=QSpinBox(); self.threshold.setRange(1,50); self.threshold.setSuffix('%'); self.threshold.setValue(self.config['warning_threshold']); form.addRow('剩余额度阈值',self.threshold)
        form.addRow('',label('拖动方格排序；迷你条跟随同一顺序。\nCtrl + Alt + M 显示或隐藏窗口。',9,owner.colors['muted']))
        tabs.addTab(display,'显示')
        network=QWidget(); net_form=QFormLayout(network); net_form.setSpacing(12)
        net=self.config['network']
        self.proxy_mode=QComboBox(); self.proxy_mode.addItems(['使用系统代理','填写代理地址'])
        self.proxy_mode.setCurrentIndex(net['mode']=='custom')
        self.proxy_url=QLineEdit(net['url']); self.proxy_url.setPlaceholderText('http://127.0.0.1:7890')
        self.proxy_url.setEnabled(net['mode']=='custom')
        self.proxy_mode.currentIndexChanged.connect(lambda n:self.proxy_url.setEnabled(n==1))
        net_form.addRow('共享代理',self.proxy_mode); net_form.addRow('代理地址',self.proxy_url)
        choices=QWidget(); choices_layout=QGridLayout(choices); choices_layout.setContentsMargins(0,0,0,0)
        self.proxy_agents={}
        for i,p in enumerate(self.config['order']):
            box=QCheckBox(PROVIDERS[p][0]); box.setChecked(p in net['agents'])
            if p=='doubao': box.setToolTip('用于独立额度查询；勾选后使用共享代理')
            choices_layout.addWidget(box,i//2,i%2); self.proxy_agents[p]=box
        net_form.addRow('使用代理的 Agent',choices)
        explanation=label('勾选的 Agent 共用此代理；未勾选的 Agent 直连。\n支持 HTTP / HTTPS / SOCKS5。保存后立即刷新。\n外部浏览器、已运行的客户端使用自身网络设置。\n豆包的代理选择在“启动豆包并连接”时生效；\n修改后需自行退出豆包，再通过本工具启动。',9,owner.colors['muted']); explanation.setWordWrap(True)
        net_form.addRow(explanation); tabs.addTab(network,'网络')
        sources=QWidget(); sources_form=QVBoxLayout(sources)
        info=label('优先复用本机登录；缺少登录时可打开对应的登录入口。\n登录完成后刷新额度。各平台的接入能力可能不同。',10,owner.colors['text']); info.setWordWrap(True); sources_form.addWidget(info)
        for p in self.config['order']:
            row=QHBoxLayout(); row.addWidget(label(PROVIDERS[p][0],11,owner.colors['text'],True)); row.addStretch()
            row.addWidget(button('连接管理','查看状态并登录',lambda p=p:owner.show_connection(p))); sources_form.addLayout(row)
        sources_form.addWidget(label('可选：已使用 Claude Code 的电脑可启用状态栏采集。',9,owner.colors['muted']))
        sources_form.addWidget(button('启用 Claude 采集','安装并备份原状态栏配置',lambda:self.bridge(True)))
        sources_form.addWidget(button('停用 Claude 采集','恢复原状态栏配置',lambda:self.bridge(False)))
        self.bridge_status=label('',9,owner.colors['muted']); self.bridge_status.setWordWrap(True); sources_form.addWidget(self.bridge_status)
        sources_form.addWidget(button('打开本地数据目录','设置、额度快照与 Claude 状态栏备份',lambda:owner.open_data_directory()))
        sources_form.addStretch(); tabs.addTab(sources,'接入')
        self.error=label('',9,owner.colors['warning']); self.error.setWordWrap(True); body.addWidget(self.error)
        actions=QDialogButtonBox(QDialogButtonBox.StandardButton.Save|QDialogButtonBox.StandardButton.Cancel)
        actions.button(QDialogButtonBox.StandardButton.Save).setText('保存并刷新')
        actions.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        actions.accepted.connect(self.save); actions.rejected.connect(self.reject); body.addWidget(actions)
    def validate_focus(self,p,checked):
        count=sum(b.isChecked() for b in self.focus.values())
        if count<1 or count>3:
            self.focus[p].blockSignals(True); self.focus[p].setChecked(not checked); self.focus[p].blockSignals(False)
    def validate_agents(self,p,checked):
        if not any(box.isChecked() for box in self.agent_visibility.values()):
            box=self.agent_visibility[p]; box.blockSignals(True); box.setChecked(True); box.blockSignals(False)
        self.sync_focus()
    def sync_focus(self):
        visible=[p for p,box in self.agent_visibility.items() if box.isChecked()]
        selected=[p for p in visible if self.focus[p].isChecked()][:3] or visible[:1]
        for p,box in self.focus.items():
            box.blockSignals(True); box.setEnabled(p in visible); box.setChecked(p in selected); box.blockSignals(False)
    def bridge(self,enable):
        try:
            result=(install_bridge if enable else uninstall_bridge)(self.owner.store.directory)
            self.bridge_status.setText(result); self.owner.refresh(['claude'])
        except (OSError,ValueError) as e: self.bridge_status.setText(str(e))
    def save(self):
        config=self.config
        # These may change through a connection dialog while Settings is open.
        for key in ('manual','order','position','mini'):
            config[key]=copy.deepcopy(self.owner.store.settings[key])
        net={'mode':['system','custom'][self.proxy_mode.currentIndex()], 'url':self.proxy_url.text().strip(),
             'agents':[p for p,box in self.proxy_agents.items() if box.isChecked()]}
        if net['mode']=='custom' and net['agents']:
            try: validate_proxy(net['url'])
            except ValueError as e: self.error.setText(str(e)); return
        config['network']=net
        config.update(enabled_agents=[p for p,b in self.agent_visibility.items() if b.isChecked()],
                      mini_ids=[p for p,b in self.focus.items() if b.isChecked()],layout=['compact','dock'][self.layout.currentIndex()],
                      opacity=self.opacity.value(),pinned=self.pinned.isChecked(),theme=['system','light','dark'][self.theme.currentIndex()],
                      refresh_seconds=self.interval.value(),notifications=self.alerts.isChecked(),warning_threshold=self.threshold.value())
        try:
            self.owner.set_autostart(self.startup.isChecked())
            self.owner.store.settings=config; self.owner.store.save()
        except OSError: self.error.setText('无法保存设置，请检查本地数据目录权限。'); return
        self.owner.apply_settings(); self.owner.render(); self.owner.refresh(); self.accept()

class MeterWindow(QWidget):
    def __init__(self,store,auto_collect=True):
        super().__init__()
        self.store=store; self.auto_collect=auto_collect; self.snapshots={p:Snapshot(p) for p in PROVIDERS}; self.snapshots.update(store.load_cache())
        self.busy={}; self.alerted=set(); self.stale_refresh_attempted=set()
        self.detail_dialogs={}; self.connection_dialogs={}; self.settings_dialog=None; self.quitting=False
        self.drag_active=False; self.render_pending=False; self.claude_mtime=None; self.last_redraw=time.monotonic()
        self.setWindowTitle('Agent Meter'); self.setWindowIcon(app_icon())
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        outer=QVBoxLayout(self); outer.setContentsMargins(8,8,8,8)
        self.surface=Surface(self); outer.addWidget(self.surface)
        self.body=QVBoxLayout(self.surface); self.body.setContentsMargins(14,12,14,12); self.body.setSpacing(10)
        self.api_timer=QTimer(self); self.api_timer.timeout.connect(self.refresh)
        self.local_timer=QTimer(self); self.local_timer.timeout.connect(self.tick)
        if auto_collect: self.local_timer.start(5000)
        self.tray=QSystemTrayIcon(app_icon(),self); self.tray.setToolTip('Agent Meter · Agent 套餐额度')
        menu=QMenu()
        for title,action in [('显示 / 隐藏',self.toggle_visible),('切换迷你条',self.toggle_mini),('刷新用量',self.refresh),('设置',self.show_settings),('退出',self.quit)]:
            menu.addAction(title,action)
        self.tray.setContextMenu(menu); self.tray.activated.connect(self.tray_activated); self.tray.show()
        self.apply_settings(); self.render(); self.restore_position()
        if auto_collect: QTimer.singleShot(100,self.refresh)
    def colors_for_theme(self):
        theme=self.store.settings['theme']
        dark=theme=='dark' or theme=='system' and QApplication.palette().color(QPalette.ColorRole.Window).lightness()<128
        return {'surface':'#20242c','text':'#ecedf2','muted':'#a1a9b7','border':'#383f4b','tile':'#292e38','track':'#3b4250','accent':'#80c9b7','warning':'#ffb271','button':'#303642'} if dark else {'surface':'#f5f7fa','text':'#263141','muted':'#697587','border':'#dce2e9','tile':'#ffffff','track':'#e5eaf0','accent':'#327e6d','warning':'#ac5d16','button':'#eaf0f5'}
    def dialog_style(self):
        c=self.colors
        return f'''QDialog,QTabWidget::pane,QWidget {{background:{c['surface']};color:{c['text']};font-family:"{FONT_FAMILY}";font-size:12px;}}
        QLabel {{background:transparent;}} QPushButton {{background:{c['button']};border:1px solid {c['border']};border-radius:6px;padding:7px 10px;}}
        QPushButton:hover {{border-color:{c['accent']};}} QLineEdit,QSpinBox,QDoubleSpinBox,QComboBox,QDateTimeEdit {{background:{c['tile']};border:1px solid {c['border']};border-radius:4px;padding:5px;}}
        QTabBar::tab {{padding:8px 16px;}} QTabBar::tab:selected {{color:{c['accent']};}}'''
    def apply_settings(self):
        self.colors=self.colors_for_theme()
        flags=Qt.WindowType.FramelessWindowHint|Qt.WindowType.Tool
        if self.store.settings['pinned']: flags |= Qt.WindowType.WindowStaysOnTopHint
        visible=self.isVisible(); self.setWindowFlags(flags)
        if visible: self.show()
        c=self.colors
        self.setStyleSheet(f'QPushButton {{background:transparent;color:{c["text"]};border:0;border-radius:6px;padding:5px;min-height:21px;}} QPushButton:hover {{background:{c["button"]};}} QPushButton:focus {{border:1px solid {c["accent"]};}} QLabel {{background:transparent;}}')
        if self.auto_collect: self.api_timer.start(self.store.settings['refresh_seconds']*1000)
        for dialogs in (self.detail_dialogs,self.connection_dialogs):
            for p,dialog in list(dialogs.items()):
                if p not in self.store.settings['enabled_agents']: dialog.close()
        self.surface.update()
    def visible_agents(self):
        return [p for p in self.store.settings['order'] if p in self.store.settings['enabled_agents']]
    def mini_button_data(self,p):
        s=self.snapshots[p]; cfg=self.store.settings; c=self.colors
        name='Claude' if p=='claude' else PROVIDERS[p][0]
        values=' · '.join(f'{short_quota_name(q.name)} {percent(q)}' for q in s.quotas[:2])
        if len(s.quotas)>2: values+=f' +{len(s.quotas)-2}'
        text=name+' '+(values or '—')
        if self.snapshot_status(s) in ('error','stale'): text+=' ·'
        style=f'QPushButton {{background:{c["tile"]};border:1px solid {c["border"]};padding:4px 7px;}}'
        if any(q.remaining<cfg['warning_threshold'] for q in s.quotas): style+=f'QPushButton {{color:{c["warning"]};}}'
        return text,PROVIDERS[p][0]+' · '+quota_tooltip(s),style
    def update_data(self,providers=None):
        self.last_redraw=time.monotonic()
        providers=self.visible_agents() if providers is None else providers
        for p in providers:
            if self.store.settings['mini']:
                b=self.mini_buttons.get(p)
                if b:
                    text,tip,style=self.mini_button_data(p)
                    b.setText(b.fontMetrics().elidedText(text,Qt.TextElideMode.ElideRight,b.width()-14))
                    b.setToolTip(tip); b.setAccessibleName(tip)
                    if b.styleSheet()!=style: b.setStyleSheet(style)
            elif p in self.tiles: self.tiles[p].update_snapshot()
            dialog=self.detail_dialogs.get(p)
            if dialog and dialog.isVisible(): self.fill_detail(dialog,p)
        if self.footer_status:
            active=any(p in self.busy for p in self.visible_agents())
            self.footer_status.setText('正在读取额度…' if active else '后台自动更新')
    def render(self):
        if self.drag_active:
            self.render_pending=True; return
        self.last_redraw=time.monotonic()
        clear(self.body); cfg=self.store.settings; c=self.colors
        self.mini_buttons={}; self.footer_status=None
        if cfg['mini']:
            self.body.setContentsMargins(10,6,10,6)
            row=QHBoxLayout(); row.setSpacing(5)
            row.addWidget(app_logo(18))
            for p in self.visible_agents():
                if p not in cfg['mini_ids']: continue
                text,tip,style=self.mini_button_data(p)
                b=button(text,tip,lambda p=p:self.show_detail(p)); b.setStyleSheet(style)
                b.setFixedWidth(max(152,b.sizeHint().width())); self.mini_buttons[p]=b
                row.addWidget(b)
            row.addWidget(button('⚙','配置迷你条',self.show_settings,28)); row.addWidget(button('↗','展开用量窗口',self.toggle_mini,28))
            self.body.addLayout(row)
        else:
            self.body.setContentsMargins(14,12,14,12)
            header=QHBoxLayout(); header.setSpacing(4)
            header.addWidget(app_logo(22))
            title=label('Agent Meter',11,c['text'],True); title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            header.addWidget(title); header.addStretch()
            pin=button('','取消置顶' if cfg['pinned'] else '置顶窗口',self.toggle_pin,26)
            pin.setIcon(pin_icon(cfg['pinned'],c['accent'] if cfg['pinned'] else c['muted']))
            pin.setIconSize(QSize(16,16)); pin.setCheckable(True); pin.setChecked(cfg['pinned'])
            header.addWidget(pin)
            header.addWidget(button('⚙','设置',self.show_settings,28)); header.addWidget(button('×','隐藏到托盘',self.hide,28))
            self.body.addLayout(header)
            grid=QGridLayout(); grid.setSpacing(8); columns=4 if cfg['layout']=='dock' else 2
            self.tiles={}
            for i,p in enumerate(self.visible_agents()):
                tile=Tile(self,p); self.tiles[p]=tile; grid.addWidget(tile,i//columns,i%columns)
            self.body.addLayout(grid)
            footer=QHBoxLayout(); footer.setSpacing(3)
            active=any(p in self.busy for p in self.visible_agents())
            text='正在读取额度…' if active else '后台自动更新'
            status=label(text,8,c['muted']); status.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            status.setFixedWidth(120); self.footer_status=status
            footer.addWidget(status); footer.addStretch(); footer.addWidget(button('↻','刷新用量',self.refresh,28)); footer.addWidget(button('−','收起到迷你条',self.toggle_mini,28))
            self.body.addLayout(footer)
        # Clear old fixed geometry before asking Qt to measure the new mode.
        # Otherwise a newly inserted mini row can inherit the full-grid bounds.
        self.setMinimumSize(0,0); self.setMaximumSize(16777215,16777215)
        self.body.invalidate(); self.body.activate(); self.surface.updateGeometry()
        self.layout().invalidate(); self.layout().activate()
        self.setFixedSize(self.layout().sizeHint()); self.ensure_on_screen()
        for p,dialog in list(self.detail_dialogs.items()):
            if dialog.isVisible(): self.fill_detail(dialog,p)
    def snapshot_status(self,snapshot):
        return effective_status(snapshot,refresh_seconds=self.store.settings['refresh_seconds'])
    def refresh(self,providers=None):
        if self.quitting or not self.auto_collect: return
        if not isinstance(providers,(list,tuple)): providers=self.visible_agents()
        for p in providers:
            if p not in self.store.settings['enabled_agents']: continue
            if p in self.busy: continue
            job=Collector(p,self.store.settings,self.store.directory)
            job.signals.result.connect(self.received); self.busy[p]=job
            QThreadPool.globalInstance().start(job)
        self.update_data([])
    def received(self,p,fresh):
        old=self.snapshots[p]; job=self.busy.pop(p,None)
        if p not in self.store.settings['enabled_agents']: return
        if job and p in ('cursor','codex','claude') and (provider_network(job.settings,p)!=provider_network(self.store.settings,p)
                or (p=='claude' and job.auth_generation!=claude_client(self.store.directory).generation)):
            self.refresh([p]); return
        self.snapshots[p]=merge_snapshot(old,fresh)
        s=self.snapshots[p]
        # A failed request (or a response containing an expired window) must
        # wait for scheduled/manual polling instead of retrying on every tick.
        if s.status=='live' and self.snapshot_status(s)=='live': self.stale_refresh_attempted.discard(p)
        else: self.stale_refresh_attempted.add(p)
        if self.snapshot_status(s)=='live' and s.primary:
            low=s.primary.remaining<self.store.settings['warning_threshold']
            if low and p not in self.alerted and self.store.settings['notifications']:
                self.tray.showMessage('Agent Meter · 额度提醒',f'{PROVIDERS[p][0]} 剩余 {percent(s.primary)}，{reset_text(s.primary.reset_at)}',QSystemTrayIcon.MessageIcon.Warning,5000)
                self.alerted.add(p)
            elif not low: self.alerted.discard(p)
        self.store.save_cache(self.snapshots); self.update_data([p])
        dialog=self.connection_dialogs.get(p)
        if dialog and dialog.isVisible() and not getattr(dialog,'login_job',None) and not getattr(dialog,'oauth_flow',None):
            dialog.status_label.setText(self.snapshots[p].message)
    def tick(self):
        try: mtime=(self.store.directory/'claude-quota.json').stat().st_mtime_ns
        except OSError: mtime=None
        if mtime!=self.claude_mtime and 'claude' in self.store.settings['enabled_agents']:
            self.claude_mtime=mtime; self.refresh(['claude'])
        elif time.monotonic()-self.last_redraw>=30:
            self.update_data()
        self.refresh_stale()
    def refresh_stale(self):
        if self.quitting or not self.auto_collect: return
        providers=[p for p in self.visible_agents() if p not in self.busy
                   and p not in self.stale_refresh_attempted and self.snapshots[p].status!='manual'
                   and self.snapshot_status(self.snapshots[p])=='stale']
        if providers:
            self.stale_refresh_attempted.update(providers)
            self.refresh(providers)
    def reorder(self,source,target,side):
        order=self.store.settings['order']
        if source not in order or target not in order or source==target: return
        next_order=[p for p in order if p!=source]
        next_order.insert(next_order.index(target)+(side=='after'),source)
        self.store.settings['order']=next_order; self.store.save(); self.render()
        if hasattr(self,'tiles') and source in self.tiles: self.tiles[source].setFocus()
    def toggle_mini(self):
        self.store.settings['mini']=not self.store.settings['mini']; self.store.save(); self.render()
        self.show(); self.raise_()
    def toggle_pin(self):
        self.store.settings['pinned']=not self.store.settings['pinned']; self.store.save(); self.apply_settings(); self.render()
    def show_settings(self):
        if self.settings_dialog and self.settings_dialog.isVisible(): self.settings_dialog.raise_(); return
        self.settings_dialog=SettingsDialog(self); self.settings_dialog.show()
    def show_detail(self,p):
        if p in self.detail_dialogs and self.detail_dialogs[p].isVisible(): self.detail_dialogs[p].raise_(); return
        dialog=QDialog(self,Qt.WindowType.Dialog|Qt.WindowType.WindowStaysOnTopHint)
        dialog.setWindowTitle(PROVIDERS[p][0]+' · 套餐额度'); dialog.setMinimumWidth(380)
        dialog.setStyleSheet(self.dialog_style()); dialog.body=QVBoxLayout(dialog)
        self.detail_dialogs[p]=dialog; self.fill_detail(dialog,p); dialog.show()
    def fill_detail(self,dialog,p):
        c=self.colors; s=self.snapshots[p]; state=self.snapshot_status(s)
        source='来源：'+(s.source or '尚未读取')
        if s.collected_at: source+='\n采集：'+time.strftime('%Y-%m-%d %H:%M:%S',time.localtime(s.collected_at))
        message='旧数据 · '+s.message if state=='stale' else s.message
        key=(tuple(bool(q.detail) for q in s.quotas),tuple(c.items()))
        if getattr(dialog,'detail_key',None)==key:
            dialog.message_label.setText(message)
            dialog.message_label.setStyleSheet(f'color:{c["warning"] if state in ("error","stale") else c["muted"]};background:transparent;')
            for widgets,q in zip(dialog.quota_widgets,s.quotas):
                name,value,meter,reset,detail=widgets
                name.setText(q.name); value.setText('剩余 '+percent(q)); meter.setValue(round(q.remaining*10))
                reset.setText(reset_text(q.reset_at))
                if detail: detail.setText(q.detail)
            dialog.source_label.setText(source)
            return
        dialog.setUpdatesEnabled(False); clear(dialog.body); dialog.quota_widgets=[]
        dialog.body.addWidget(label(PROVIDERS[p][0]+'   '+PROVIDERS[p][1],14,c['text'],True))
        msg=label(message,10,c['warning'] if state in ('error','stale') else c['muted']); msg.setWordWrap(True); dialog.body.addWidget(msg)
        dialog.message_label=msg
        for q in s.quotas:
            row=QHBoxLayout(); name=label(q.name,11,c['text']); value=label('剩余 '+percent(q),13,c['text'],True)
            row.addWidget(name); row.addStretch(); row.addWidget(value); dialog.body.addLayout(row)
            meter=QProgressBar(); meter.setRange(0,1000); meter.setValue(round(q.remaining*10)); meter.setTextVisible(False); meter.setFixedHeight(6)
            meter.setStyleSheet(f'QProgressBar {{border:0;background:{c["track"]};}} QProgressBar::chunk {{background:{c["accent"]};}}'); dialog.body.addWidget(meter)
            reset=label(reset_text(q.reset_at),9,c['muted']); dialog.body.addWidget(reset); detail=None
            if q.detail:
                detail=label(q.detail,9,c['muted']); detail.setWordWrap(True); dialog.body.addWidget(detail)
            dialog.quota_widgets.append((name,value,meter,reset,detail))
        dialog.source_label=label(source,9,c['muted']); dialog.body.addWidget(dialog.source_label)
        actions=QHBoxLayout(); actions.addWidget(button('刷新','刷新当前工具',lambda:self.refresh([p])))
        if p=='doubao': actions.addWidget(button('手动录入','录入套餐权益余额',lambda:self.manual_dialog(p)))
        actions.addWidget(button('连接管理','查看登录与接入状态',lambda:self.show_connection(p)))
        if p in USAGE_URLS: actions.addWidget(button('用量页面','打开官方页面',lambda:self.open_url(USAGE_URLS[p])))
        actions.addWidget(button('关闭','关闭详情',dialog.close)); dialog.body.addLayout(actions)
        dialog.detail_key=key; dialog.adjustSize(); dialog.setUpdatesEnabled(True)
    def show_connection(self,p):
        existing=self.connection_dialogs.get(p)
        if existing and existing.isVisible(): existing.raise_(); return
        dialog=QDialog(self,Qt.WindowType.Dialog|Qt.WindowType.WindowStaysOnTopHint)
        dialog.setWindowTitle(PROVIDERS[p][0]+' · 连接管理'); dialog.setMinimumWidth(420); dialog.setStyleSheet(self.dialog_style())
        body=QVBoxLayout(dialog); self.connection_dialogs[p]=dialog
        body.addWidget(label(PROVIDERS[p][0]+'   '+PROVIDERS[p][1],14,self.colors['text'],True))
        description=label(connection_description(p),10,self.colors['muted']); description.setWordWrap(True); body.addWidget(description)
        status=label(self.snapshots[p].message or '正在检查本机登录',10,self.colors['text']); status.setWordWrap(True); body.addWidget(status)
        if p=='codex':
            start=button('浏览器 OAuth 登录','由 Codex 官方组件完成认证',lambda:self.start_codex_login(dialog,status,start)); body.addWidget(start)
            dialog.auth_url=''
            dialog.oauth_copy=button('复制登录链接','浏览器未打开时，复制官方授权链接并手动粘贴到浏览器',
                lambda:QApplication.clipboard().setText(dialog.auth_url) if dialog.auth_url else None)
            dialog.oauth_copy.hide(); body.addWidget(dialog.oauth_copy)
        elif p=='cursor':
            def launch():
                if open_local_cursor(): status.setText('请在 Cursor 客户端登录，然后点击“刷新连接”。')
                else: self.open_url('https://cursor.com/downloads'); status.setText('未找到 Cursor，请安装官方客户端并登录。')
            body.addWidget(button('打开 Cursor 登录','在官方客户端完成登录',launch))
        elif p=='claude':
            self.add_claude_login(dialog,body,status)
            body.addWidget(button('打开官方用量页','通过 Claude 官方网页查看套餐额度',lambda:self.open_url(USAGE_URLS[p])))
            body.addWidget(button('手动录入额度','尚未连接时临时录入',lambda:self.manual_dialog(p)))
            body.addWidget(button('状态栏采集设置','已安装官方 CLI 时的可选采集方式',self.show_settings))
        else:
            body.addWidget(button('复用本机登录并刷新','读取本机已登录的豆包会话，无需启动桌面版',lambda:self.refresh(['doubao'])))
            start=button('启动豆包并连接','启动已退出的桌面版并复用当前登录',lambda:self.start_doubao_connection(dialog,status,start))
            body.addWidget(start)
            hint=label('独立查询无需保持豆包运行，代理设置立即用于下次刷新。\n桌面版连接为备用方式，不会自动关闭豆包。',9,self.colors['muted']); hint.setWordWrap(True); body.addWidget(hint)
            body.addWidget(button('打开豆包网页','打开豆包官方页面',lambda:self.open_url(USAGE_URLS[p])))
            body.addWidget(button('手动录入额度','录入桌面版套餐权益',lambda:self.manual_dialog(p)))
        actions=QHBoxLayout()
        actions.addWidget(button('刷新连接','重新读取本机登录和额度',lambda:self.refresh([p])))
        actions.addWidget(button('关闭','关闭连接管理',dialog.close)); body.addLayout(actions)
        dialog.status_label=status; dialog.show()
    def start_doubao_connection(self,dialog,status,start):
        if getattr(dialog,'login_job',None): return
        job=DoubaoConnectJob(provider_network(self.store.settings,'doubao'))
        dialog.login_job=job; start.setEnabled(False); status.setText('正在检查豆包连接…')
        def finished(success,message):
            dialog.login_job=None
            if not dialog.isVisible(): return
            start.setEnabled(True); status.setText(message)
            if success:
                QTimer.singleShot(2000,lambda:self.refresh(['doubao']))
                QTimer.singleShot(7000,lambda:self.refresh(['doubao']))
        job.signals.finished.connect(finished,Qt.ConnectionType.QueuedConnection)
        QThreadPool.globalInstance().start(job)
    def add_claude_login(self,dialog,body,status):
        dialog.oauth_flow=None; dialog.login_job=None
        start=button('浏览器 OAuth 登录','无需安装 Claude Code，在浏览器中授权',lambda:begin())
        body.addWidget(start)
        hint=label('在浏览器完成授权后，粘贴页面显示的授权码。浏览器使用自身代理设置。',10,self.colors['muted'])
        hint.setWordWrap(True); body.addWidget(hint)
        code=QLineEdit(); code.setPlaceholderText('粘贴授权码或完整回调地址')
        code.setEchoMode(QLineEdit.EchoMode.Password); code.setEnabled(False); body.addWidget(code)
        submit=button('完成登录','交换授权码并加密保存令牌',lambda:finish()); submit.setEnabled(False); body.addWidget(submit)
        def cancel():
            if dialog.oauth_flow: dialog.oauth_flow.cancelled.set()
            code.clear()
        def begin():
            cancel(); dialog.oauth_flow=begin_login(); code.setEnabled(True); submit.setEnabled(True)
            start.setText('重新打开授权页面'); status.setText('等待浏览器授权 · 授权码 10 分钟内有效。')
            self.open_url(dialog.oauth_flow.url); code.setFocus()
        def finish():
            if dialog.login_job or not dialog.oauth_flow: return
            if not code.text().strip(): status.setText('请先粘贴浏览器返回的授权码。'); return
            job=ClaudeLoginJob(claude_client(self.store.directory),dialog.oauth_flow,
                code.text(),provider_network(self.store.settings,'claude'))
            code.clear(); code.setEnabled(False); submit.setEnabled(False); start.setEnabled(False)
            forget.setEnabled(False); dialog.login_job=job; status.setText('正在完成授权…')
            def finished(success,message):
                dialog.login_job=None
                if not dialog.isVisible(): return
                start.setEnabled(True); forget.setEnabled(True); status.setText(message)
                if success:
                    dialog.oauth_flow=None; start.setText('重新登录 / 切换账号')
                    self.snapshots['claude']=Snapshot('claude',message=message)
                    self.store.save_cache(self.snapshots); self.refresh(['claude'])
                else:
                    cancel(); dialog.oauth_flow=None; start.setText('重新开始 OAuth 登录')
            job.signals.finished.connect(finished,Qt.ConnectionType.QueuedConnection); QThreadPool.globalInstance().start(job)
        def remove():
            cancel(); dialog.oauth_flow=None; code.setEnabled(False); submit.setEnabled(False)
            try: claude_client(self.store.directory).forget()
            except Exception: status.setText('无法移除登录信息，请稍后重试。'); return
            self.snapshots['claude']=Snapshot('claude',message='已移除本工具授权；本机客户端登录不受影响。')
            self.store.save_cache(self.snapshots); self.render(); status.setText(self.snapshots['claude'].message)
            start.setText('浏览器 OAuth 登录')
        forget=button('移除本工具授权','删除本工具保存的 Claude 令牌；本机客户端仍可复用',remove)
        body.addWidget(forget); dialog.finished.connect(lambda _:cancel())
        code.returnPressed.connect(finish)
        dialog.oauth_start=start; dialog.oauth_code=code; dialog.oauth_submit=submit; dialog.oauth_forget=forget
    def start_codex_login(self,dialog,status,start):
        if getattr(dialog,'login_job',None): return
        job=LoginJob(provider_network(self.store.settings,'codex')); dialog.login_job=job; start.setEnabled(False)
        status.setText('正在启动官方登录…')
        job.signals.url.connect(lambda url:self.open_codex_auth_url(dialog,status,url)); job.signals.status.connect(status.setText)
        def finished(success,message):
            dialog.login_job=None; dialog.auth_url=''; dialog.oauth_copy.hide()
            start.setEnabled(True); status.setText(message)
            if success: self.refresh(['codex'])
        job.signals.finished.connect(finished)
        dialog.finished.connect(lambda _:job.cancelled.set())
        QThreadPool.globalInstance().start(job)
    def open_codex_auth_url(self,dialog,status,url):
        if not dialog.isVisible(): return
        dialog.auth_url=url; dialog.oauth_copy.show()
        if not self.open_url(url):
            status.setText('浏览器未自动打开；请复制登录链接并在浏览器中完成授权。')
    def manual_dialog(self,p):
        dialog=QDialog(self,Qt.WindowType.Dialog|Qt.WindowType.WindowStaysOnTopHint); dialog.setWindowTitle('手动录入 · '+PROVIDERS[p][0]); dialog.setStyleSheet(self.dialog_style())
        form=QFormLayout(dialog); old=self.store.settings.get('manual',{}).get(p,{})
        name=QLineEdit(old.get('label','套餐权益')); amount=QDoubleSpinBox(); amount.setRange(0,100); amount.setValue(old.get('remaining',100)); amount.setSuffix('%')
        known=QCheckBox('已知重置时间'); date=QDateTimeEdit(QDateTime.currentDateTime().addDays(1)); date.setCalendarPopup(True); date.setDisplayFormat('yyyy-MM-dd HH:mm'); date.setEnabled(False); known.toggled.connect(date.setEnabled)
        form.addRow('权益名称',name); form.addRow('剩余比例',amount); form.addRow('',known); form.addRow('重置时间',date)
        note=label('手动数据会标注来源与录入时间。',9,self.colors['muted']); form.addRow(note)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Save|QDialogButtonBox.StandardButton.Cancel); buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject); form.addRow(buttons)
        if dialog.exec()==QDialog.DialogCode.Accepted:
            reset=date.dateTime().toSecsSinceEpoch() if known.isChecked() else None
            if reset and reset<=time.time(): QMessageBox.information(self,'重置时间','请填写未来的重置时间。'); return
            self.store.settings['manual'][p]={'remaining':amount.value(),'label':name.text().strip() or '套餐权益','collected_at':time.time(),'reset_at':reset}
            self.store.save(); self.refresh([p])
    def restore_position(self):
        position=self.store.settings.get('position')
        if isinstance(position,list) and len(position)==2 and all(isinstance(x,int) for x in position): self.move(*position)
        else:
            screen=QApplication.primaryScreen().availableGeometry(); self.move(screen.right()-self.width()-24,screen.top()+80)
        self.ensure_on_screen()
    def ensure_on_screen(self):
        screen=QApplication.screenAt(self.pos()) or QApplication.primaryScreen(); bounds=screen.availableGeometry()
        self.move(max(bounds.left(),min(self.x(),bounds.right()-self.width()+1)),max(bounds.top(),min(self.y(),bounds.bottom()-self.height()+1)))
    def save_position(self):
        self.store.settings['position']=[self.x(),self.y()]; self.store.save()
    def toggle_visible(self):
        if self.isVisible(): self.hide()
        else: self.show(); self.raise_(); self.activateWindow()
    def tray_activated(self,reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,QSystemTrayIcon.ActivationReason.DoubleClick): self.toggle_visible()
    def closeEvent(self,event):
        if self.quitting: event.accept()
        else: event.ignore(); self.hide()
    def quit(self):
        for dialog in self.connection_dialogs.values():
            if getattr(dialog,'login_job',None): dialog.login_job.cancelled.set()
            if getattr(dialog,'oauth_flow',None): dialog.oauth_flow.cancelled.set()
        self.quitting=True; self.api_timer.stop(); self.local_timer.stop(); self.save_position(); self.tray.hide(); QApplication.quit()
    def open_data_directory(self):
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl
        self.store.directory.mkdir(parents=True,exist_ok=True); QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.store.directory)))
    def open_url(self,url):
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl
        return QDesktopServices.openUrl(QUrl(url))
    def autostart_enabled(self):
        if sys.platform=='darwin':
            from .platform_support import mac_startup_enabled
            return mac_startup_enabled()
        if sys.platform!='win32': return False
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,r'Software\Microsoft\Windows\CurrentVersion\Run') as key:
                return bool(winreg.QueryValueEx(key,'AgentMeter')[0])
        except OSError: return False
    def set_autostart(self,enabled):
        if sys.platform=='darwin':
            from .platform_support import set_mac_startup
            set_mac_startup(enabled); return
        if sys.platform!='win32': return
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,r'Software\Microsoft\Windows\CurrentVersion\Run') as key:
            if enabled:
                command=f'"{sys.executable}"' if getattr(sys,'frozen',False) else f'"{Path(sys.executable).with_name("pythonw.exe")}" "{Path(__file__).resolve().parents[1]/"run.py"}"'
                winreg.SetValueEx(key,'AgentMeter',0,winreg.REG_SZ,command)
            else:
                try: winreg.DeleteValue(key,'AgentMeter')
                except FileNotFoundError: pass
