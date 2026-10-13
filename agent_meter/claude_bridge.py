"""Install a reversible official status-line bridge; only quotas reach disk."""
from pathlib import Path
import json
import os
import shutil
import time
from .storage import atomic_json

SCRIPT = r'''const fs = require('fs');
const path = require('path');
const cp = require('child_process');
let raw = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', part => raw += part);
process.stdin.on('end', () => {
  let data = {};
  try { data = JSON.parse(raw); } catch (_) { return; }
  const limits = {};
  for (const key of ['five_hour', 'seven_day']) {
    const q = data.rate_limits?.[key];
    if (q && typeof q.used_percentage === 'number' && Number.isFinite(q.used_percentage)) {
      limits[key] = {used_percentage:q.used_percentage, resets_at:q.resets_at ?? null};
    }
  }
  try {
    const destination = path.join(__dirname, 'claude-quota.json');
    const temporary = destination + '.' + process.pid + '.tmp';
    fs.writeFileSync(temporary, JSON.stringify({rate_limits:limits, collected_at:Date.now()/1000}));
    fs.renameSync(temporary, destination);
  } catch (_) {}
  let previous;
  try { previous = JSON.parse(fs.readFileSync(path.join(__dirname, 'claude-bridge-backup.json'), 'utf8')).statusLine; } catch (_) {}
  if (previous?.type === 'command' && previous.command) {
    try {
      const bash = ['C:/Program Files/Git/bin/bash.exe', 'C:/Program Files/Git/usr/bin/bash.exe'].find(p => fs.existsSync(p));
      const options = {input:raw, encoding:'utf8', timeout:2500, windowsHide:true, stdio:['pipe','pipe','ignore']};
      const command = previous.command.trim().startsWith('"') ? '& '+previous.command : previous.command;
      const output = bash ? cp.execFileSync(bash, ['-c', previous.command], options) : cp.execFileSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', command], options);
      process.stdout.write(output);
      return;
    } catch (_) {}
  }
  const parts = Object.entries(limits).map(([key,q]) => `${key==='five_hour'?'5h':'7d'}: ${Math.round(100-q.used_percentage)}% left`);
  console.log(`[${data.model?.display_name || 'Claude'}]${parts.length?' | '+parts.join(' | '):''}`);
});
'''

def bridge_command(directory):
    node = shutil.which('node.exe') or shutil.which('node')
    if not node: raise ValueError('未找到 Node.js，无法启用 Claude 状态栏采集。')
    return f'"{Path(node).as_posix()}" "{(Path(directory)/"claude-statusline.cjs").as_posix()}"'

def install_bridge(directory, settings_path=None):
    directory = Path(directory)
    settings_path = Path(settings_path or Path.home()/'.claude/settings.json')
    command = bridge_command(directory)
    existing = json.loads(settings_path.read_text(encoding='utf-8')) if settings_path.exists() else {}
    if not isinstance(existing,dict): raise ValueError('Claude 设置格式异常，未修改。')
    current = existing.get('statusLine')
    backup = directory/'claude-bridge-backup.json'
    if current and current.get('command') == command:
        (directory/'claude-statusline.cjs').write_text(SCRIPT,encoding='utf-8')
        return 'Claude 采集已启用；新开会话并完成一次对话后会更新。'
    # Do not overwrite a previous backup; otherwise uninstall could lose the
    # user's original status-line command.
    if backup.exists(): raise ValueError('已保存的 Claude 状态栏与当前设置不同，请先停用采集后重试。')
    directory.mkdir(parents=True,exist_ok=True)
    atomic_json(backup,{'statusLine':current,'had_statusLine':'statusLine' in existing})
    stamp=time.strftime('%Y%m%d-%H%M%S')
    if settings_path.exists(): shutil.copy2(settings_path, directory/f'claude-settings-before-{stamp}.json')
    (directory/'claude-statusline.cjs').write_text(SCRIPT,encoding='utf-8')
    existing['statusLine']={'type':'command','command':command}
    atomic_json(settings_path,existing)
    return '已启用 Claude 采集；新开会话并完成一次对话后会更新。'

def uninstall_bridge(directory, settings_path=None):
    directory=Path(directory)
    settings_path=Path(settings_path or Path.home()/'.claude/settings.json')
    backup=directory/'claude-bridge-backup.json'
    if not backup.exists(): return '未启用 Claude 采集。'
    original=json.loads(backup.read_text(encoding='utf-8'))
    existing=json.loads(settings_path.read_text(encoding='utf-8'))
    current=existing.get('statusLine',{}).get('command','')
    if current!=bridge_command(directory):
        raise ValueError('Claude 状态栏已由其他工具修改，未覆盖；备份仍保留。')
    if original.get('had_statusLine'): existing['statusLine']=original.get('statusLine')
    else: existing.pop('statusLine',None)
    atomic_json(settings_path,existing)
    backup.unlink()
    return '已恢复原来的 Claude 状态栏。'
