import React, {useEffect, useRef, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {Check, ChevronDown, Clock3, Download, FolderOpen, HardDrive, Layers, Loader2, Play, RefreshCw, Settings2, Square, Terminal, Upload, X} from 'lucide-react';
import './style.css';

const statuses = {queued:'等待开始', running:'正在运行', completed:'已完成', failed:'任务失败', cancelled:'已取消', cancelling:'正在停止', interrupted:'运行中断'};
const activeStatuses = ['queued', 'running', 'cancelling'];
const needsSettings = ['landcover', 'euluc', 'events'];

async function api(path, options = {}) {
  const response = await fetch('/api' + path, options);
  const data = await response.json().catch(() => ({detail:'服务响应异常'}));
  if (!response.ok) throw Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail));
  return data;
}
const post = (path, data = {}) => api(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)});
const bytes = size => size >= 1024 ** 2 ? `${(size / 1024 ** 2).toFixed(1)} MB` : `${(size / 1024).toFixed(1)} KB`;

function DatasetSelect({catalog, selected, onChange}) {
  const [open, setOpen] = useState(false);
  const container = useRef(null);
  const firstOption = useRef(null);
  const trigger = useRef(null);
  useEffect(() => {
    const outside = event => {if (!container.current?.contains(event.target)) setOpen(false)};
    document.addEventListener('pointerdown', outside);
    return () => document.removeEventListener('pointerdown', outside);
  }, []);
  useEffect(() => {if (open) firstOption.current?.focus()}, [open]);
  const close = () => {setOpen(false); trigger.current?.focus()};
  const toggle = id => onChange(selected.includes(id) ? selected.filter(key => key !== id) : [...selected, id]);
  const names = catalog.filter(item => selected.includes(item.id)).map(item => item.name);
  return <div className="dataset-select" ref={container} onKeyDown={event => {
    if (event.key === 'Escape') {event.stopPropagation(); close()}
    if (open && ['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
      event.preventDefault();
      const choices = [...container.current.querySelectorAll('[role="menuitemcheckbox"]')];
      const current = choices.indexOf(document.activeElement);
      const index = event.key === 'Home' ? 0 : event.key === 'End' ? choices.length - 1 : (current + (event.key === 'ArrowDown' ? 1 : -1) + choices.length) % choices.length;
      choices[index]?.focus();
    }
  }}>
    <button type="button" ref={trigger} id="dataset-picker" className={'select-trigger ' + (open ? 'is-open' : '')} aria-haspopup="menu" aria-expanded={open} aria-controls="dataset-options" aria-labelledby="dataset-label dataset-summary" onClick={() => setOpen(!open)}>
      <span id="dataset-summary" className="truncate">{names.length ? names.slice(0, 2).join('、') : '选择数据'}</span>
      {names.length > 2 && <span className="selection-count">+{names.length - 2}</span>}
      <ChevronDown size={22}/>
    </button>
    {open && <div className="dataset-menu" id="dataset-options" role="menu" aria-label="数据类型" onBlur={event => {if (!event.currentTarget.parentElement.contains(event.relatedTarget)) setOpen(false)}}>
      {catalog.map((item, index) => <button type="button" key={item.id} ref={index === 0 ? firstOption : null} role="menuitemcheckbox" aria-checked={selected.includes(item.id)} className={'dataset-option ' + (selected.includes(item.id) ? 'checked' : '')} onClick={() => toggle(item.id)}>
        <span className="checkbox">{selected.includes(item.id) && <Check size={17}/>}</span><span>{item.name}</span><span className="option-format">{item.format}</span>
      </button>)}
      <div className="menu-footer"><span>{selected.length} 项</span><button type="button" onClick={close}>完成</button></div>
    </div>}
  </div>;
}

function Dialog({children, onClose}) {
  const dialog = useRef(null);
  useEffect(() => {
    const previous = document.activeElement;
    dialog.current.showModal();
    return () => {previous?.focus()};
  }, []);
  return <dialog className="settings-dialog" ref={dialog} aria-labelledby="settings-title" onCancel={event => {event.preventDefault(); onClose()}} onClick={event => {if (event.target === event.currentTarget) onClose()}}>
    <div className="dialog-content"><header className="panel-heading"><h2 id="settings-title"><Settings2 size={24}/>数据源设置</h2><button className="icon-button" aria-label="关闭数据源设置" onClick={onClose}><X size={24}/></button></header>
      <div className="dialog-body">{children}</div><footer className="dialog-footer"><button className="primary-button" onClick={onClose}>确定</button></footer>
    </div>
  </dialog>;
}

function App() {
  const [config, setConfig] = useState(null), [error, setError] = useState(''), [tab, setTab] = useState('collect');
  const [selected, setSelected] = useState(['dem']), [area, setArea] = useState(''), [areaName, setAreaName] = useState('');
  const [output, setOutput] = useState(''), [landcover, setLandcover] = useState(''), [euluc, setEuluc] = useState(''), [member, setMember] = useState('按城市划分.zip/按城市划分/重庆市.shp');
  const [eventLimit, setEventLimit] = useState(3), [eventCodes, setEventCodes] = useState('');
  const [jobs, setJobs] = useState([]), [job, setJob] = useState(null), [files, setFiles] = useState([]), [busy, setBusy] = useState('');
  const [settingsOpen, setSettingsOpen] = useState(false), [picking, setPicking] = useState('');
  const uploadRef = useRef(null), consoleRef = useRef(null);
  const catalog = config?.catalog || [];
  const label = id => catalog.find(item => item.id === id)?.name || id;
  const safe = async action => {setError(''); try {await action()} catch (exception) {setError(exception.message)}};
  const refresh = () => api('/jobs').then(setJobs).catch(exception => setError(exception.message));

  useEffect(() => {
    api('/config').then(value => {setConfig(value); setOutput(value.paths.output); setLandcover(value.paths.sources + '/landcover'); setEuluc(value.paths.sources + '/euluc')}).catch(exception => setError(exception.message));
    refresh();
  }, []);
  useEffect(() => {
    if (!job?.id) return;
    let disposed = false;
    const poll = async () => {
      try {
        const value = await api('/jobs/' + job.id);
        if (disposed) return;
        setJob(value);
        if (!activeStatuses.includes(value.status)) {
          const results = await api('/jobs/' + job.id + '/files');
          if (!disposed) {setFiles(results); refresh()}
        }
      } catch (exception) {if (!disposed) setError(exception.message)}
    };
    poll();
    const interval = activeStatuses.includes(job.status) ? setInterval(poll, 500) : null;
    return () => {disposed = true; if (interval) clearInterval(interval)};
  }, [job?.id, job?.status]);
  useEffect(() => {if (consoleRef.current) consoleRef.current.scrollTop = consoleRef.current.scrollHeight}, [job?.log]);

  const pick = async (setter, id) => {
    setPicking(id);
    await safe(async () => {const value = await post('/pick-directory'); if (value.path) setter(value.path)});
    setPicking('');
  };
  const upload = async file => {
    if (!file) return;
    setBusy('upload');
    await safe(async () => {
      const form = new FormData(); form.append('file', file);
      const value = await api('/area', {method:'POST', body:form});
      setArea(value.path); setAreaName(value.name);
      setConfig(await api('/config'));
    });
    setBusy('');
  };
  const start = async () => {
    setBusy('start');
    await safe(async () => {
      const value = await post('/jobs', {datasets:selected, area, output, landcover_source:landcover, euluc_source:euluc, euluc_member:member, event_limit:Number(eventLimit), event_codes:eventCodes.split(/[,，\s]+/).filter(Boolean)});
      setJob(value); setFiles([]); refresh();
    });
    setBusy('');
  };
  const cancel = () => safe(async () => {await post('/jobs/' + job.id + '/cancel'); setJob(await api('/jobs/' + job.id))});
  const needsArea = selected.some(id => catalog.find(item => item.id === id)?.area);
  const running = job && activeStatuses.includes(job.status);
  const total = job?.datasets.length || 0;
  const done = (job?.completed.length || 0) + (job?.failed.length || 0);
  const overallPercent = job?.progress?.overall_percent ?? (job?.status === 'completed' ? 100 : 0);
  const estimated = job?.progress?.overall_estimated ?? job?.status !== 'completed';
  const dirField = (value, setter, id, name) => <div className="path-field"><input id={id} value={value} onChange={event => setter(event.target.value)} aria-label={name} title={value}/><button type="button" className="browse-button" onClick={() => pick(setter, id)} disabled={!!picking}>{picking === id ? <Loader2 size={22} className="spin"/> : <FolderOpen size={22}/>}<span>浏览</span></button></div>;

  return <div className="app-shell">
    <header className="app-header"><a className="brand" href="#" onClick={event => {event.preventDefault(); setTab('collect')}}><span className="brand-mark"><Layers size={30}/></span><span className="brand-name">基础地理信息数据获取<small>basic geoinfo fetch</small></span></a>
      <nav aria-label="主导航">{[['collect', Layers, '数据采集'], ['history', Clock3, '任务记录'], ['sources', HardDrive, '数据源与缓存']].map(([id, Icon, title]) => <button type="button" key={id} className={'nav-item ' + (tab === id ? 'active' : '')} aria-current={tab === id ? 'page' : undefined} onClick={() => {setTab(id); setError(''); if (id === 'history') refresh()}}><Icon size={23}/><span>{title}</span></button>)}</nav>
      <span className={'connection ' + (config ? 'online' : '')}><i/>{config ? '已连接' : '未连接'}</span>
    </header>

    {error && <div className="error-banner" role="alert"><span>{error}</span><button type="button" className="icon-button" aria-label="关闭错误提示" onClick={() => setError('')}><X size={24}/></button></div>}

    <main className="workspace">
      {tab === 'collect' && <div className="collection-grid">
        <section className="panel config-panel" aria-labelledby="collect-title"><header className="panel-heading"><h1 id="collect-title"><Settings2 size={25}/>采集设置</h1></header>
          <div className="collection-form">
            <div className="form-field"><label id="dataset-label">数据类型<span className="field-meta">{selected.length} 项</span></label><DatasetSelect catalog={catalog} selected={selected} onChange={setSelected}/></div>
            <div className="form-field"><label htmlFor="existing-area">目标范围<span className="field-meta">{needsArea ? '必填' : '可选'}</span></label><div className="area-field"><select id="existing-area" value={area} title={areaName || area} onChange={event => {setArea(event.target.value); setAreaName(event.target.value ? event.target.selectedOptions[0].text : '')}}><option value="">选择范围文件</option>{config?.boundaries.map(boundary => <option key={boundary.path} value={boundary.path}>{boundary.name}</option>)}</select><button type="button" className="secondary-button upload-button" onClick={() => uploadRef.current.click()} disabled={busy === 'upload'}>{busy === 'upload' ? <Loader2 size={22} className="spin"/> : <Upload size={22}/>}上传</button></div>
              <input hidden ref={uploadRef} type="file" accept=".geojson" aria-label="上传范围文件" onChange={event => {upload(event.target.files[0]); event.target.value = ''}}/>
            </div>
            <div className="form-field"><label htmlFor="output-directory">保存位置</label>{dirField(output, setOutput, 'output-directory', '结果保存目录')}</div>
            {selected.some(id => needsSettings.includes(id)) && <button type="button" className="settings-button" onClick={() => setSettingsOpen(true)}><FolderOpen size={23}/>数据源设置<Settings2 size={22}/></button>}
          </div>
          <div className="collect-actions"><button type="button" className="primary-button start-button" onClick={start} disabled={!config || !selected.length || (needsArea && !area) || busy === 'start' || running}>{busy === 'start' ? <Loader2 size={24} className="spin"/> : <Play size={23} fill="currentColor"/>}{running ? '任务进行中' : '开始采集'}</button></div>
        </section>

        <div className="activity-column"><section className="panel log-panel" aria-labelledby="log-title"><header className="panel-heading"><h2 id="log-title"><Terminal size={25}/>运行日志</h2><span className={'job-badge ' + (job?.status || '')}>{job ? statuses[job.status] : '等待任务'}</span>{running && <button type="button" className="stop-button" onClick={cancel} disabled={job.status === 'cancelling'}><Square size={17}/>停止</button>}</header>
            {job && <div className="progress-row"><span>{job.current ? label(job.current) + ' · ' : ''}总进度</span><div className={'progress-track ' + (running ? 'estimating' : '')} role="progressbar" aria-label="数据总进度" aria-valuenow={overallPercent} aria-valuemin={0} aria-valuemax={100} aria-valuetext={`${estimated ? '约 ' : ''}${overallPercent.toFixed(1)}%`}><i style={{width:`${overallPercent}%`}}/></div><span className="progress-percent">{estimated ? '约 ' : ''}{overallPercent.toFixed(1)}%</span><span className="task-count">{done}/{total}</span></div>}
            <pre className="log-content" ref={consoleRef} role="log" aria-label="运行日志内容">{job?.log || '暂无日志'}</pre>
          </section>
          <section className="panel results-panel" aria-labelledby="results-title"><header className="panel-heading"><h2 id="results-title"><Download size={25}/>采集结果</h2><span className="field-meta">{files.length} 个文件</span></header>
            <div className="file-list">{files.length ? files.map(file => <a className="result-file" key={file.name} href={'/api/jobs/' + job.id + '/download/' + file.name.split('/').map(encodeURIComponent).join('/')}><span title={file.name}>{file.name}</span><small>{bytes(file.bytes)}</small><Download size={21}/></a>) : <span className="empty-label">暂无结果</span>}</div>
            {job && <div className="result-path" title={job.output}><FolderOpen size={20}/><span>{job.output}</span></div>}
          </section>
        </div>
      </div>}

      {tab === 'history' && <section className="panel history-panel"><header className="panel-heading"><h1><Clock3 size={26}/>任务记录</h1><button type="button" className="secondary-button" onClick={refresh}><RefreshCw size={22}/>刷新</button></header>
        <div className="history-table">{jobs.length ? <table><thead><tr><th>数据类型</th><th>创建时间</th><th>状态</th><th><span className="sr-only">操作</span></th></tr></thead><tbody>{jobs.map(value => <tr key={value.id}><td><span className="history-datasets" title={value.datasets.map(label).join('、')}>{value.datasets.map(label).join('、')}</span></td><td>{new Date(value.created_at).toLocaleString('zh-CN', {hour12:false})}</td><td><span className={'job-badge ' + value.status}>{statuses[value.status]}</span></td><td><button type="button" className="text-button" onClick={() => safe(async () => {setFiles([]); setTab('collect'); const detail = await api('/jobs/' + value.id); setJob(detail); if (!activeStatuses.includes(detail.status)) setFiles(await api('/jobs/' + value.id + '/files'))})}>查看</button></td></tr>)}</tbody></table> : <div className="empty-state">暂无任务</div>}</div>
      </section>}

      {tab === 'sources' && <section className="panel sources-panel"><header className="panel-heading"><h1><HardDrive size={26}/>数据源与缓存</h1></header><div className="directory-list">{[['源数据', config?.paths.sources], ['缓存', config?.paths.cache], ['默认成果', config?.paths.output]].map(([title, path]) => <div className="directory-row" key={title}><FolderOpen size={28}/><div><h2>{title}</h2><code>{path || '—'}</code></div></div>)}</div></section>}
    </main>

    {settingsOpen && <Dialog onClose={() => setSettingsOpen(false)}>
      {selected.includes('landcover') && <fieldset><legend>土地覆盖</legend><div className="form-field"><label htmlFor="landcover-source">源目录</label>{dirField(landcover, setLandcover, 'landcover-source', '土地覆盖源目录')}</div></fieldset>}
      {selected.includes('euluc') && <fieldset><legend>城市用地</legend><div className="form-field"><label htmlFor="euluc-source">源目录</label>{dirField(euluc, setEuluc, 'euluc-source', '城市用地源目录')}</div><div className="form-field"><label htmlFor="euluc-member">ZIP / Shapefile 路径</label><input id="euluc-member" value={member} onChange={event => setMember(event.target.value)}/></div></fieldset>}
      {selected.includes('events') && <fieldset><legend>灾害事件地图</legend><div className="form-field"><label htmlFor="event-limit">最新事件数量</label><input id="event-limit" type="number" min="1" max="100" value={eventLimit} onChange={event => setEventLimit(event.target.value)}/></div><div className="form-field"><label htmlFor="event-codes">指定事件编号</label><input id="event-codes" value={eventCodes} onChange={event => setEventCodes(event.target.value)} placeholder="EMSR927, EMSR925"/></div></fieldset>}
    </Dialog>}
  </div>;
}

createRoot(document.getElementById('root')).render(<App/>);
