import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import tempfile
import unittest
from unittest.mock import patch
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QProgressBar
from PySide6.QtGui import QFontDatabase
from agent_meter.ui import MeterWindow, SettingsDialog
from agent_meter.storage import Store
from agent_meter.model import Snapshot, Quota
from agent_meter.doubao import parse_quota

class WindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])
        # The offscreen Windows plugin has no system font database.
        QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')
        QFontDatabase.addApplicationFont('C:/Windows/Fonts/segoeui.ttf')
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.store=Store(self.tmp.name)
        self.window=MeterWindow(self.store,auto_collect=False)
        self.window.show(); self.app.processEvents()
    def tearDown(self):
        self.window.quitting=True; self.window.tray.hide(); self.window.close(); self.window.deleteLater(); self.app.processEvents(); self.tmp.cleanup()
    def test_mini_one_row_all_counts_and_mode_size_switch(self):
        self.store.settings['mini']=True
        for ids in [['cursor'],['cursor','codex'],['cursor','codex','claude']]:
            self.store.settings['mini_ids']=ids; self.window.render(); self.app.processEvents()
            buttons=[b for b in self.window.findChildren(QPushButton) if b.isVisible()]
            self.assertEqual(len(buttons),len(ids)+2)
            centers=[b.geometry().center().y() for b in buttons]
            self.assertLessEqual(max(centers)-min(centers),1)
            self.assertLess(self.window.height(),80)
            self.assertGreater(self.window.width(),150)
            self.assertLess(self.window.width(),650)
        self.store.settings['mini']=False; self.store.settings['layout']='dock'; self.window.render(); self.app.processEvents()
        self.assertEqual(len(self.window.tiles),4)
        self.assertGreater(self.window.width(),600)
        self.assertLess(self.window.height(),280)
        self.window.grab().save('native-dock.png')
        self.store.settings['layout']='compact'; self.window.render(); self.app.processEvents()
        self.assertGreater(self.window.height(),300); self.assertLess(self.window.width(),400)
        self.window.grab().save('native-compact.png')
        self.window.toggle_mini(); self.app.processEvents(); self.window.grab().save('native-mini.png')
    def test_order_is_shared_with_mini_and_saved(self):
        self.window.reorder('cursor','codex','before')
        self.assertEqual(self.store.settings['order'],['doubao','cursor','codex','claude'])
        self.assertEqual(Store(self.tmp.name).settings['order'],self.store.settings['order'])
        self.store.settings['mini']=True; self.store.settings['mini_ids']=['codex','cursor']; self.window.render(); self.app.processEvents()
        text=[b.text() for b in self.window.findChildren(QPushButton) if b.isVisible()]
        self.assertTrue(text[0].startswith('Cursor')); self.assertTrue(text[1].startswith('Codex'))
    def test_refresh_updates_widgets_without_rebuilding_or_moving_windows(self):
        self.window.snapshots['codex']=Snapshot('codex','live','已连接',quotas=[Quota('5 小时',98),Quota('7 天',70)])
        self.window.render(); self.window.show_detail('codex'); self.app.processEvents()
        tile=self.window.tiles['codex']; widgets=list(tile.quota_widgets)
        detail=self.window.detail_dialogs['codex']; detail_widgets=list(detail.quota_widgets)
        geometry=self.window.geometry(); detail_geometry=detail.geometry()
        self.window.auto_collect=True
        with patch.object(self.window,'render',side_effect=AssertionError('quota refresh rebuilt the window')), \
             patch('agent_meter.ui.QThreadPool.globalInstance'):
            self.window.refresh(['codex'])
            self.window.received('codex',Snapshot('codex','live','已连接',quotas=[Quota('5 小时',87),Quota('7 天',65)]))
            self.window.last_redraw-=31; self.window.tick(); self.app.processEvents()
        self.window.auto_collect=False
        self.assertIs(self.window.tiles['codex'],tile)
        self.assertEqual(tile.quota_widgets,widgets); self.assertEqual(detail.quota_widgets,detail_widgets)
        self.assertEqual(widgets[0][2].value(),870); self.assertEqual(detail_widgets[1][2].value(),650)
        self.assertEqual(self.window.geometry(),geometry); self.assertEqual(detail.geometry(),detail_geometry)

    def test_new_quota_windows_and_mini_refresh_keep_window_geometry(self):
        tile=self.window.tiles['codex']; geometry=self.window.geometry()
        self.window.received('codex',Snapshot('codex','live','已连接',quotas=[Quota('5 小时',80),Quota('7 天',60)]))
        self.app.processEvents()
        self.assertIs(self.window.tiles['codex'],tile); self.assertEqual(self.window.geometry(),geometry)
        self.assertEqual(len(tile.quota_widgets),2)
        self.store.settings['mini']=True; self.store.settings['mini_ids']=['codex']
        self.window.render(); self.app.processEvents()
        button=self.window.mini_buttons['codex']; geometry=self.window.geometry(); button_geometry=button.geometry()
        with patch.object(self.window,'render',side_effect=AssertionError('mini refresh rebuilt the window')):
            self.window.received('codex',Snapshot('codex','live','已连接',quotas=[Quota('5 小时',100),Quota('7 天',9)]))
            self.app.processEvents()
        self.assertIs(self.window.mini_buttons['codex'],button)
        self.assertEqual(self.window.geometry(),geometry); self.assertEqual(button.geometry(),button_geometry)
        self.assertIn('9%',button.toolTip())
    def test_stale_after_sleep_refreshes_once_and_failure_waits_for_next_poll(self):
        self.store.settings['enabled_agents']=['codex']; self.window.auto_collect=True
        self.window.snapshots['codex']=Snapshot('codex','live','已连接',collected_at=1000,quotas=[Quota('7 天',80,5000)])
        with patch('agent_meter.model.time.time',return_value=1400), \
             patch('agent_meter.ui.QThreadPool.globalInstance') as pool:
            self.window.tick(); self.window.tick()
            self.assertEqual(pool.return_value.start.call_count,1)
            self.window.received('codex',Snapshot('codex','error','断网'))
            for _ in range(3): self.window.tick()
            self.assertEqual(pool.return_value.start.call_count,1)
            self.assertEqual(self.window.snapshots['codex'].collected_at,1000)
            self.window.refresh()  # The periodic timer uses this same entrypoint.
            self.assertEqual(pool.return_value.start.call_count,2)
            self.window.received('codex',Snapshot('codex','live','已连接',collected_at=1400,quotas=[Quota('7 天',75,5000)]))
            self.window.tick(); self.assertEqual(pool.return_value.start.call_count,2)
            with patch('agent_meter.model.time.time',return_value=1800): self.window.tick()
            self.assertEqual(pool.return_value.start.call_count,3)
        self.window.auto_collect=False

    def test_expired_response_does_not_loop_and_hidden_manual_agents_are_not_retried(self):
        self.store.settings['enabled_agents']=['codex','cursor']; self.window.auto_collect=True
        self.window.snapshots['codex']=Snapshot('codex','live','已连接',collected_at=1400,quotas=[Quota('7 天',80,1399)])
        self.window.snapshots['cursor']=Snapshot('cursor','manual','手动录入',collected_at=1000,quotas=[Quota('月度',50,1399)])
        self.window.snapshots['claude']=Snapshot('claude','stale','旧数据',collected_at=1000,quotas=[Quota('7 天',50)])
        with patch('agent_meter.model.time.time',return_value=1400), \
             patch('agent_meter.ui.QThreadPool.globalInstance') as pool:
            self.window.tick()
            self.assertEqual(pool.return_value.start.call_count,1)
            self.assertEqual(pool.return_value.start.call_args.args[0].provider,'codex')
            self.window.received('codex',Snapshot('codex','live','已连接',collected_at=1400,quotas=[Quota('7 天',80,1399)]))
            for _ in range(3): self.window.tick()
            self.assertEqual(pool.return_value.start.call_count,1)
        self.window.auto_collect=False
    def test_configuration_bounds_and_saving(self):
        dialog=SettingsDialog(self.window); dialog.show(); self.app.processEvents()
        self.assertEqual(dialog.interval.value(),300)
        self.window.auto_collect=True; self.window.apply_settings()
        self.assertEqual(self.window.api_timer.interval(),300000)
        self.window.auto_collect=False; self.window.api_timer.stop()
        for box in dialog.focus.values(): box.setChecked(True)
        self.assertEqual(sum(b.isChecked() for b in dialog.focus.values()),3)
        for box in dialog.focus.values(): box.setChecked(False)
        self.assertEqual(sum(b.isChecked() for b in dialog.focus.values()),1)
        # Do not touch the real Windows login-start registry in UI tests.
        self.window.set_autostart=lambda value:None
        self.store.settings['manual']['claude']={'remaining':40,'collected_at':100}
        dialog.opacity.setValue(75); dialog.save(); self.app.processEvents()
        self.assertEqual(Store(self.tmp.name).settings['opacity'],75)
        self.assertEqual(Store(self.tmp.name).settings['manual']['claude']['remaining'],40)

    def test_shared_proxy_selection_saved_for_all_agents(self):
        dialog=SettingsDialog(self.window); self.window.set_autostart=lambda _:None
        dialog.proxy_mode.setCurrentIndex(1); dialog.proxy_url.setText('socks5h://localhost:1080')
        for p,box in dialog.proxy_agents.items(): box.setChecked(p in ('codex','claude'))
        dialog.save(); self.app.processEvents()
        self.assertEqual(Store(self.tmp.name).settings['network'],{
            'mode':'custom','url':'socks5h://localhost:1080','agents':['codex','claude']})
    def test_hidden_agents_are_saved_omitted_from_mini_and_not_polled(self):
        dialog=SettingsDialog(self.window); self.window.set_autostart=lambda _:None
        dialog.agent_visibility['claude'].setChecked(False)
        dialog.agent_visibility['doubao'].setChecked(False)
        self.assertFalse(dialog.focus['claude'].isEnabled())
        self.window.auto_collect=True
        with patch('agent_meter.ui.QThreadPool.globalInstance') as pool:
            dialog.save(); self.app.processEvents()
            polled={call.args[0].provider for call in pool.return_value.start.call_args_list}
        self.window.auto_collect=False; self.window.api_timer.stop()
        self.assertEqual(polled,{'cursor','codex'})
        self.assertEqual(set(Store(self.tmp.name).settings['enabled_agents']),{'cursor','codex'})
        self.assertEqual(set(self.window.tiles),{'cursor','codex'})
        self.store.settings['mini']=True; self.window.render(); self.app.processEvents()
        texts=[b.text() for b in self.window.findChildren(QPushButton) if b.isVisible()]
        self.assertFalse(any('Claude' in text or '豆包' in text for text in texts))
        self.store.settings['mini']=False
        restore=SettingsDialog(self.window)
        restore.agent_visibility['claude'].setChecked(True); restore.save(); self.app.processEvents()
        self.assertIn('claude',Store(self.tmp.name).settings['enabled_agents'])
        self.assertIn('claude',self.window.tiles)

    def test_at_least_one_agent_remains_visible(self):
        dialog=SettingsDialog(self.window)
        for box in dialog.agent_visibility.values(): box.setChecked(False)
        self.assertEqual(sum(box.isChecked() for box in dialog.agent_visibility.values()),1)
        self.assertEqual(sum(box.isChecked() for box in dialog.focus.values()),1)
    def test_connection_management_available_for_every_agent(self):
        for p in self.store.settings['order']:
            self.window.show_connection(p); self.app.processEvents()
            dialog=self.window.connection_dialogs[p]
            self.assertTrue(dialog.isVisible()); self.assertTrue(dialog.status_label.text())
            dialog.close()

    def test_codex_oauth_link_can_be_copied_when_browser_does_not_open(self):
        self.window.show_connection('codex'); self.app.processEvents()
        dialog=self.window.connection_dialogs['codex']
        url='https://auth.openai.com/oauth/authorize?state=test'
        with patch.object(self.window,'open_url',return_value=False) as browser:
            self.window.open_codex_auth_url(dialog,dialog.status_label,url)
        browser.assert_called_once_with(url)
        self.assertTrue(dialog.oauth_copy.isVisible())
        self.assertIn('浏览器未自动打开',dialog.status_label.text())
        dialog.oauth_copy.click()
        self.assertEqual(QApplication.clipboard().text(),url)

    def test_doubao_bound_visible_in_detail_and_single_row_mini(self):
        self.window.snapshots['doubao']=parse_quota({'code':0,'groups':[{'windows':[
            {'window_type':2,'used_percent':0,'less_than_one_percent':True}]}]})
        self.window.show_detail('doubao'); self.app.processEvents()
        from PySide6.QtWidgets import QLabel
        texts=[x.text() for x in self.window.detail_dialogs['doubao'].findChildren(QLabel)]
        self.assertIn('剩余 >99%',texts); self.assertIn('已用 <1%',texts)
        self.window.detail_dialogs['doubao'].close()
        self.store.settings['mini']=True; self.store.settings['mini_ids']=['doubao','codex','claude']
        self.window.render(); self.app.processEvents()
        buttons=[b for b in self.window.findChildren(QPushButton) if b.isVisible()]
        self.assertTrue(any('>99%' in b.text() for b in buttons))
        centers=[b.geometry().center().y() for b in buttons]
        self.assertLessEqual(max(centers)-min(centers),1)

    def test_multiple_quota_windows_visible_on_tile_and_mini(self):
        self.window.snapshots['claude']=Snapshot('claude','live','已连接',quotas=[
            Quota('5 小时',23),Quota('7 天',81),Quota('Review · 1 小时',46)])
        self.window.render(); self.app.processEvents()
        tile=self.window.tiles['claude']
        texts=[widget.text() for widget in tile.findChildren(QLabel)]
        self.assertIn('5h',texts); self.assertIn('7d',texts)
        self.assertIn('23%',texts); self.assertIn('81%',texts); self.assertIn('46%',texts)
        self.assertEqual(len(tile.findChildren(QProgressBar)),3)
        self.store.settings['mini']=True; self.store.settings['mini_ids']=['claude']
        self.window.render(); self.app.processEvents()
        mini=[button.text() for button in self.window.findChildren(QPushButton) if button.isVisible()]
        self.assertTrue(any('5h23%' in text and '7d81%' in text and '+1' in text for text in mini))

    def test_cursor_mini_keeps_both_pools_compact_and_stable(self):
        self.store.settings['mini']=True; self.store.settings['mini_ids']=['cursor']
        self.window.snapshots['cursor']=Snapshot('cursor','live','已连接',quotas=[
            Quota('其他模型 · 月度',5),Quota('Cursor / Auto 模型 · 月度',76)])
        self.window.render(); self.app.processEvents()
        button=self.window.mini_buttons['cursor']; geometry=self.window.geometry()
        self.assertEqual(button.text(),'Cursor 其他5% Auto76%')
        self.assertLessEqual(button.width(),180)
        self.assertIn('其他模型 · 月度',button.toolTip())
        self.assertIn('Cursor / Auto 模型 · 月度',button.toolTip())
        self.window.received('cursor',Snapshot('cursor','live','已连接',quotas=[
            Quota('其他模型 · 月度',100),Quota('Cursor / Auto 模型 · 月度',100)]))
        self.app.processEvents()
        self.assertEqual(button.text(),'Cursor 其他100% Auto100%')
        self.assertEqual(self.window.geometry(),geometry)

    def test_claude_oauth_start_close_and_success_controls(self):
        class ImmediatePool:
            def start(self,job): job.run()
        with patch.object(self.window,'open_url') as browser:
            self.window.show_connection('claude'); self.app.processEvents()
            dialog=self.window.connection_dialogs['claude']
            self.assertFalse(dialog.oauth_submit.isEnabled())
            dialog.oauth_start.click(); flow=dialog.oauth_flow
            self.assertTrue(browser.call_args.args[0].startswith('https://claude.ai/oauth/authorize?'))
            dialog.oauth_code.setText('code#'+flow.state)
            with patch('agent_meter.ui.QThreadPool.globalInstance',return_value=ImmediatePool()), patch('agent_meter.claude_oauth.ClaudeClient.exchange',return_value=True):
                dialog.oauth_submit.click(); self.app.processEvents()
            self.assertEqual(dialog.oauth_code.text(),'')
            self.assertIsNone(dialog.login_job); self.assertIsNone(dialog.oauth_flow)
            self.assertIn('登录成功',dialog.status_label.text())
            dialog.oauth_start.click(); pending=dialog.oauth_flow
            dialog.oauth_code.setText('private-code'); dialog.close(); self.app.processEvents()
            self.assertTrue(pending.cancelled.is_set()); self.assertEqual(dialog.oauth_code.text(),'')

if __name__=='__main__': unittest.main()
