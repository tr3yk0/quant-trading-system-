const $ = (id) => document.getElementById(id);
window.__settingsDirty = false;
window.__settingsPageActive = false;


function setTxt(id, v){ const el = document.getElementById(id); if(el) el.textContent = v; }
function setHtml(id, v){ const el = document.getElementById(id); if(el) el.innerHTML = v; }
function setCls(id, v){ const el = document.getElementById(id); if(el) el.className = v; }

const fmt = (n, digits = 2) => typeof n === 'number' && Number.isFinite(n) ? n.toLocaleString(undefined, { maximumFractionDigits: digits }) : '—';
const pct = (n) => typeof n === 'number' ? `${(n * 100).toFixed(0)}%` : '—';
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (m) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[m]));

/* ═══════════════════════════════════════════════════════════════
   Institutional performance layer — cache, abort, loading, pager
   ═══════════════════════════════════════════════════════════════ */
const ApiCache = {
  store: new Map(),
  inflight: new Map(),
  hits: 0,
  misses: 0,
  staleHits: 0,
  evictions: 0,
  maxEntries: 8192,
  // Longer TTL for slow-moving reference data; state remains near-realtime.
  ttlFor(url){
    if(url.includes('/api/state')) return 1000;
    if(url.includes('/api/market/registry')) return 15000;
    if(url.includes('/api/universe/detail')) return 8000;
    if(url.includes('/api/universe/results')) return 5000;
    if(url.includes('/api/mt5/rates')) return 3000;
    if(url.includes('/api/symbol-catalog')) return 60000;
    if(url.includes('/api/performance')) return 3000;
    if(url.includes('/api/providers')) return 20000;
    if(url.includes('/api/health')) return 5000;
    return 3000;
  },
  staleFor(url){
    if(url.includes('/api/state')) return 8000;
    if(url.includes('/api/market/registry')) return 60000;
    if(url.includes('/api/universe/detail')) return 30000;
    return 15000;
  },
  key(url, opts){ return url + (opts && opts.method ? '|' + opts.method : ''); },
  _entry(url){
    const entry = this.store.get(url);
    if(!entry) return null;
    if(Date.now() > entry.staleUntil){ this.store.delete(url); return null; }
    this.store.delete(url); this.store.set(url, entry); // LRU touch
    return entry;
  },
  get(url){
    const entry = this._entry(url);
    if(!entry || Date.now() > entry.expires) return null;
    this.hits++;
    return entry.data;
  },
  getStale(url){
    const entry = this._entry(url);
    if(!entry || Date.now() > entry.staleUntil) return null;
    this.staleHits++;
    return entry.data;
  },
  set(url, data, ttl){
    const base = ttl ?? this.ttlFor(url);
    const jittered = base * (0.95 + Math.random() * 0.1);
    this.store.delete(url);
    this.store.set(url, { data, expires: Date.now() + jittered, staleUntil: Date.now() + jittered + this.staleFor(url) });
    while(this.store.size > this.maxEntries){
      const oldest = this.store.keys().next().value;
      this.store.delete(oldest);
      this.evictions++;
    }
  },
  invalidate(prefix){
    for(const k of [...this.store.keys()]){
      if(!prefix || k.includes(prefix)) this.store.delete(k);
    }
  },
  stats(){ return { hits: this.hits, misses: this.misses, staleHits: this.staleHits, evictions: this.evictions, size: this.store.size, inflight: this.inflight.size }; }
};

const Loading = {
  count: 0,
  bar: null,
  timer: null,
  active: new Map(),
  regions: new Map(),
  show(key='request', {silent=false, region=null}={}){
    this.active.set(key, (this.active.get(key)||0)+1);
    if(region){
      const node = document.getElementById(region);
      const host = node?.closest('[data-loading-region]') || node?.parentElement;
      if(host){
        const n = this.regions.get(region) || {count:0,host,indicator:null};
        n.count++;
        if(!n.indicator){ n.indicator=document.createElement('span'); n.indicator.className='region-loader'; n.indicator.setAttribute('role','status'); n.indicator.textContent='Loading…'; host.appendChild(n.indicator); }
        host.classList.add('region-loading'); host.setAttribute('aria-busy','true');
        this.regions.set(region,n);
      }
    }
    // Silent background refreshes never activate the global bar; focused panels may still
    // show their own region indicator, leaving the rest of the application interactive.
    if(silent) return;
    this.count++;
    if(!this.bar) this.bar = document.getElementById('globalLoading');
    if(this.bar){
      this.bar.setAttribute('aria-hidden','false');
      this.bar.dataset.pending = String(this.count);
      this.bar.dataset.stage = String(key).includes('/api/state') ? 'refreshing-state' : 'loading-data';
      const label=this.bar.querySelector('#globalLoadingLabel');
      if(label) label.textContent=String(key).includes('/api/state')?'Refreshing live state':'Loading data';
    }
    if(!this.timer){
      this.timer = setTimeout(()=>{
        this.timer = null;
        if(this.count > 0 && this.bar) this.bar.classList.add('show');
      }, 120);
    }
  },
  hide(key='request', {silent=false, region=null}={}){
    const current = this.active.get(key)||0;
    if(current <= 1) this.active.delete(key); else this.active.set(key,current-1);
    if(region){
      const n=this.regions.get(region);
      if(n){ n.count=Math.max(0,n.count-1); if(!n.count){n.indicator?.remove();n.host.classList.remove('region-loading');n.host.removeAttribute('aria-busy');this.regions.delete(region);} }
    }
    if(silent) return;
    this.count = Math.max(0, this.count - 1);
    if(this.bar) this.bar.dataset.pending = String(this.count);
    if(this.count === 0){
      if(this.timer){ clearTimeout(this.timer); this.timer = null; }
      if(this.bar){ this.bar.classList.remove('show'); this.bar.setAttribute('aria-hidden','true'); delete this.bar.dataset.stage; }
    }
  }
};

let _stateAbort = null;
window.__apiStaleActive = false;

async function get(url, { useCache = true, force = false, signal, retries = 2, silent = false, region = null } = {}){
  const cacheKey = ApiCache.key(url, { method: 'GET' });
  if(useCache && !force){
    const cached = ApiCache.get(url);
    if(cached != null) return cached;
    // Share identical in-flight GETs so polling and detail panels do not stampede.
    if(ApiCache.inflight.has(cacheKey)) return ApiCache.inflight.get(cacheKey);
  }
  ApiCache.misses++;
  Loading.show(url, {silent, region});
  const promise = (async () => {
    let timeoutId = null;
    let localCtrl = null;
    const localSig = signal || (localCtrl = new AbortController()).signal;
    try{
      if(!signal){
        const ms = url.includes('/api/state') ? 8000 : 20000;
        timeoutId = setTimeout(() => { try { localCtrl.abort(); } catch(_){} }, ms);
      }
      let lastError = null;
      const maxAttempts = Math.max(1, Math.min(3, Number(retries)||1));
      for(let attempt=0; attempt<maxAttempts; attempt++){
        try{
          const response = await fetch(url, { cache: 'no-store', signal: localSig, headers: { 'Accept':'application/json' } });
          if(!response.ok){
            const body = await response.text().catch(()=> '');
            let detail = null;
            try { detail = body ? JSON.parse(body) : null; } catch(_) {}
            const message = detail && typeof detail === 'object' ? (detail.error || detail.message || detail.detail) : null;
            const error = new Error(String(message || body || response.statusText || ('HTTP '+response.status)));
            error.detail = detail;
            error.status = response.status;
            const retryableStatus = [408,425,429].includes(response.status) || response.status >= 500;
            if(!retryableStatus || attempt + 1 >= maxAttempts) throw error;
            lastError = error;
          }else{
            const data = await response.json();
            if(useCache) ApiCache.set(url, data);
            window.__apiStaleActive = false;
            return data;
          }
        }catch(error){
          lastError = error;
          if(error && (error.name === 'AbortError' || localSig?.aborted)) throw error;
          if(error && Number(error.status) > 0 && Number(error.status) < 500 && ![408,425,429].includes(Number(error.status))) throw error;
          if(attempt + 1 >= maxAttempts) break;
        }
        const delay = Math.min(900, 120 * (2 ** attempt)) * (0.75 + Math.random() * 0.5);
        await new Promise(resolve => setTimeout(resolve, delay));
      }
      throw lastError || new Error('Request failed');
    }catch(error){
      const stale = useCache ? ApiCache.getStale(url) : null;
      if(stale != null){
        window.__apiStaleActive = true;
        // A stale snapshot is display-only; order authorization always happens server-side.
        return (stale && typeof stale === 'object' && !Array.isArray(stale)) ? { ...stale, _stale_cache: true } : stale;
      }
      throw error;
    }finally{
      if(timeoutId) clearTimeout(timeoutId);
      if(useCache && ApiCache.inflight.get(cacheKey) === promise) ApiCache.inflight.delete(cacheKey);
      Loading.hide(url, {silent, region});
    }
  })();
  if(useCache) ApiCache.inflight.set(cacheKey, promise);
  return promise;
}

async function post(url, body, {retries=null, silent=false, region=null}={}){
  Loading.show(url, {silent, region});
  // Only settings is a retry-safe POST: all other POSTs can place, modify, or close orders.
  const attempts = retries == null ? (url === '/api/settings' ? 3 : 1) : Math.max(1, Math.min(3, Number(retries)||1));
  try{
    let lastError = null;
    for(let attempt=0; attempt<attempts; attempt++){
      try{
        const r = await fetch(url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'Accept':'application/json' },
          body: JSON.stringify(body ?? {}),
          cache: 'no-store'
        });
        let data = null;
        try { data = await r.json(); } catch(_) { data = null; }
        if(!r.ok){
          const msg = (data && (data.error || data.message)) || (data && JSON.stringify(data)) || r.statusText || ('HTTP '+r.status);
          const err = Error(typeof msg === 'string' ? msg : JSON.stringify(msg)); err.status=r.status;
          const retryableStatus = [408,425,429].includes(r.status) || r.status >= 500;
          if(!retryableStatus || attempt + 1 >= attempts) throw err;
          lastError=err;
        } else {
          ApiCache.invalidate('/api/state'); ApiCache.invalidate('/api/settings'); ApiCache.invalidate('/api/research/experiments');
          if(url.includes('registry')) ApiCache.invalidate('/api/market/registry');
          if(url === '/api/settings') { ApiCache.invalidate('/api/universe/page'); ApiCache.invalidate('/api/symbol-catalog'); }
          if(url === '/api/news/refresh') ApiCache.invalidate('/api/news');
          window.__apiStaleActive = false;
          return data;
        }
      }catch(err){
        lastError=err;
        const status=Number(err?.status)||0;
        if(status===0 && err instanceof TypeError && /fetch/i.test(String(err.message||''))){
          lastError = new Error(`The browser could not reach ${url}. Verify the dashboard server is still running at ${window.location.origin}, then check /api/health. Original error: ${err.message||'network failure'}`);
        }
        if((status>0 && status<500 && ![408,425,429].includes(status)) || attempt + 1 >= attempts) throw lastError;
      }
      await new Promise(resolve=>setTimeout(resolve, Math.min(1200,150*(2**attempt))*(0.75+Math.random()*0.5)));
    }
    throw lastError || new Error('POST request failed');
  } finally { Loading.hide(url, {silent, region}); }
}

function toast(message){
  const t = $('toast');
  if(!t) return;
  t.textContent = message;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2200);
}

/* Client-side table pagination */
const Pagers = {
  universe: { page: 1, pageSize: 25 },
  registry: { page: 1, pageSize: 30 },
  positions: { page: 1, pageSize: 20 },
  orders: { page: 1, pageSize: 20 },
  events: { page: 1, pageSize: 30 },
};

function paginate(rows, key){
  const p = Pagers[key] || { page: 1, pageSize: 25 };
  const total = rows.length;
  const pages = Math.max(1, Math.ceil(total / p.pageSize));
  if(p.page > pages) p.page = pages;
  if(p.page < 1) p.page = 1;
  const start = (p.page - 1) * p.pageSize;
  return {
    slice: rows.slice(start, start + p.pageSize),
    page: p.page,
    pages,
    total,
    pageSize: p.pageSize,
    start: total ? start + 1 : 0,
    end: Math.min(start + p.pageSize, total),
  };
}

function renderPager(containerId, key, onChange){
  const el = $(containerId);
  if(!el) return;
  const p = Pagers[key];
  if(!p || !p._meta){ el.innerHTML = ''; return; }
  const m = p._meta;
  el.innerHTML = `
    <div class="pager-info">Showing ${m.start}–${m.end} of ${m.total}</div>
    <div class="pager-btns">
      <button type="button" data-act="first" ${m.page <= 1 ? 'disabled' : ''}>«</button>
      <button type="button" data-act="prev" ${m.page <= 1 ? 'disabled' : ''}>‹</button>
      <span style="padding:0 8px;font-size:12px;color:var(--text-secondary)">Page ${m.page} / ${m.pages}</span>
      <button type="button" data-act="next" ${m.page >= m.pages ? 'disabled' : ''}>›</button>
      <button type="button" data-act="last" ${m.page >= m.pages ? 'disabled' : ''}>»</button>
      <label class="pager-size-label">Rows <select data-act="size" aria-label="Rows per page"><option ${p.pageSize===10?'selected':''}>10</option><option ${p.pageSize===20?'selected':''}>20</option><option ${p.pageSize===25?'selected':''}>25</option><option ${p.pageSize===50?'selected':''}>50</option><option ${p.pageSize===100?'selected':''}>100</option></select></label>
    </div>`;
  const sizeControl=el.querySelector('select[data-act="size"]');
  if(sizeControl) sizeControl.onchange=()=>{p.pageSize=Math.max(1,Number(sizeControl.value)||25);p.page=1;if(typeof onChange==='function')onChange();};
  el.querySelectorAll('button').forEach(btn => {
    btn.onclick = () => {
      if(btn.dataset.act === 'first') p.page = 1;
      if(btn.dataset.act === 'prev') p.page = Math.max(1, p.page - 1);
      if(btn.dataset.act === 'next') p.page = Math.min(m.pages, p.page + 1);
      if(btn.dataset.act === 'last') p.page = m.pages;
      if(typeof onChange === 'function') onChange();
    };
  });
}

function updatePerfBadge(){
  const el = $('perfBadge');
  if(!el) return;
  const s = ApiCache.stats();
  el.textContent = `cache ${s.hits}h/${s.misses}m · ${s.size} keys${s.inflight?` · ${s.inflight} in flight`:''}${window.__apiStaleActive?' · STALE FALLBACK':''}`;
  el.classList.toggle('danger-text', !!window.__apiStaleActive);
}

function debounce(fn, ms){
  let t;
  return function(...args){
    clearTimeout(t);
    t = setTimeout(() => fn.apply(this, args), ms);
  };
}

async function control(action, confirmation=''){try{await post('/api/control',{action,confirmation});toast(action.replaceAll('_',' ').toUpperCase());await refresh();}catch(e){toast(`ERROR: ${e.message}`);}}
function armLive(){
  if(!confirm('ARM LIVE TRADING?\n\nThis only works when TRADING_MODE=live and LIVE_TRADING_ENABLED=true are configured, MT5 is connected, and the kill switch is still ON.')) return;
  control('arm_live','ARM LIVE TRADING');
}
function disarmLive(){
  if(!confirm('DISARM LIVE TRADING and turn the kill switch ON?')) return;
  control('disarm_live');
}
function killRelease(){
  if(!confirm('Release the master kill switch?\n\nThis permits the continuous engine to submit live orders only if LIVE mode is configured and ARMED.')) return;
  control('kill_off','DISABLE KILL SWITCH');
}
function closePosition(ticket){
  if(!confirm(`Close MT5 position ${ticket}?\n\nThis sends a live market close request when live trading is configured.`)) return;
  control('close_position',`CLOSE ${ticket}`);
}
function modifyPosition(ticket, sl, tp){
  if(!confirm(`Modify SL/TP for managed position ${ticket}?\n\nThis sends a live modification request.`)) return;
  const nextSl=prompt('New SL price:', sl || ''); if(nextSl===null)return;
  const nextTp=prompt('New TP price:', tp || ''); if(nextTp===null)return;
  controlWithBody('modify_position',{ticket:Number(ticket),sl:Number(nextSl),tp:Number(nextTp),confirmation:`MODIFY ${ticket}`});
}
async function controlWithBody(action, extra={}){try{await post('/api/control',{action,...extra});toast(action.replaceAll('_',' ').toUpperCase());await refresh();}catch(e){toast(`ERROR: ${e.message}`);}}

function activePageId(){ return document.querySelector('.page.active')?.id || 'dashboard'; }

const pageTitles={dashboard:'Operations Dashboard',agents:'Agent Desk',mesh:'AI Research Mesh',market:'Market Intelligence',news:'News & Live TV',risk:'Risk & Execution',trades:'Trade Monitor',management:'Position Manager',performance:'Performance & Calibration',portfolio:'Portfolio Allocation & Risk',research:'Research Lab',backtest:'Backtest & Walk-Forward Lab',quant:'Quant & ICE',shadow:'Shadow Account',runbooks:'Run Cards',strategies:'Strategy Registry',events:'Events & Errors',settings:'System Settings'};

function goPage(page, opts={}){
  if(!page || !pageTitles[page]) page = 'dashboard';
  const target = $(page);
  if(!target){ console.warn('page missing', page); return; }
  document.querySelectorAll('.nav-item').forEach(x=>{
    x.classList.toggle('active', x.dataset.page === page);
  });
  document.querySelectorAll('.page').forEach(x=>x.classList.remove('active'));
  target.classList.add('active');
  if($('pageTitle')) $('pageTitle').textContent = pageTitles[page] || page;
  try { syncGlobalNav(page); } catch(_){}
  // hash for bookmark / back-button
  if(location.hash !== '#'+page){
    try{ history.replaceState(null,'','#'+page); }catch(e){}
  }
  // always start at top of the page content
  if(!opts.keepScroll){
    const main = document.querySelector('.main');
    if(main) main.scrollTop = 0;
    window.scrollTo({ top: 0, behavior: opts.smooth ? 'smooth' : 'auto' });
  }
  // close mobile drawer
  document.body.classList.remove('nav-open');
  // if opening agents, expand transcript for readability
  if(page === 'agents' && opts.expandChat){
    const panel = document.querySelector('.desk-transcript-panel');
    if(panel) panel.classList.add('expanded');
  }
  if(page === 'backtest'){
    try { loadBacktestDefaultsFromSettings(); } catch(_){}
    setTimeout(()=>refreshResearchExperiments().catch(()=>{}), 60);
  }
  if(page === 'research'){
    setTimeout(()=>{ if(activePageId()==='research' && typeof loadFinanceToolkitStatus==='function') loadFinanceToolkitStatus(); }, 80);
  }
  if(page === 'settings'){
    if(!window.__settingsDirty && window.__lastState?.settings) syncSettingsForm(window.__lastState.settings);
    // Defer until script initialization completes, including deep links directly to Settings.
    setTimeout(()=>ensureSymbolCatalog().then(()=>{ if(activePageId()==='settings') populateSymbolSelect(window.__lastState||{}); }).catch(()=>{}),0);
  }
  if(page === 'news') {
    if(window.__newsHubTimer) clearInterval(window.__newsHubTimer);
    setTimeout(()=>{ if(activePageId()==='news') loadNewsHub({autoRefresh:true}).catch(()=>{}); }, 80);
    window.__newsHubTimer=setInterval(()=>{ if(activePageId()==='news') loadNewsHub().catch(()=>{}); else { clearInterval(window.__newsHubTimer); window.__newsHubTimer=null; } }, 30000);
  } else if(window.__newsHubTimer) { clearInterval(window.__newsHubTimer); window.__newsHubTimer=null; }
  if(page === 'dashboard') window.__forceChartRedraw=true;
  if(page === 'portfolio') setTimeout(()=>{
    if(activePageId()==='portfolio'){
      const tab=document.querySelector('#portfolioTabs [data-portfolio-tab].active')?.dataset.portfolioTab||'overview';
      setPortfolioTab(tab);
    }
  }, 80);
  if(page === 'portfolio') {
    if(window.__portfolioMonitorTimer) clearInterval(window.__portfolioMonitorTimer);
    window.__portfolioMonitorTimer = setInterval(()=>{
      if(activePageId()==='portfolio') loadPortfolioLive(true);
      else { clearInterval(window.__portfolioMonitorTimer); window.__portfolioMonitorTimer=null; }
    }, 60000);
    setTimeout(()=>{
      if(activePageId()==='portfolio'){
        const tab=document.querySelector('#portfolioTabs [data-portfolio-tab].active')?.dataset.portfolioTab||'overview';
        setPortfolioTab(tab);
      }
    }, 80);
  } else if(window.__portfolioMonitorTimer) {
    clearInterval(window.__portfolioMonitorTimer); window.__portfolioMonitorTimer=null;
  }
  if(page === 'quant') setTimeout(()=>{
    if(activePageId()==='quant' && typeof window.ensureQuantPreview==='function') window.ensureQuantPreview();
    const activeStage = document.querySelector('#quantStageTabs .stage-tab.active')?.dataset.stage;
    if(activePageId()==='quant' && activeStage==='pricing' && typeof window.loadQuantLibStatus==='function') window.loadQuantLibStatus();
  }, 120);
  if(typeof _safeRefresh==='function') setTimeout(()=>_safeRefresh(),0);
}

// Sidebar + any in-page data-page links
document.querySelectorAll('.nav-item, [data-page]').forEach(btn=>{
  if(btn._navBound) return;
  btn._navBound = true;
  btn.addEventListener('click', (e)=>{
    const page = btn.dataset.page;
    if(!page) return;
    e.preventDefault();
    goPage(page, { smooth: true });
  });
});

// Hash deep-link on load + browser back/forward
function applyHash(){
  const h = (location.hash||'').replace(/^#/,'').trim();
  if(h && pageTitles[h]) goPage(h, { keepScroll: false });
}
window.addEventListener('hashchange', applyHash);
if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', applyHash);
else applyHash();

// Mobile menu toggle
(function bindMobileNav(){
  function bind(){
    const btn = $('navToggle');
    const backdrop = $('navBackdrop');
    if(btn && !btn._bound){
      btn._bound = true;
      btn.addEventListener('click', ()=> document.body.classList.toggle('nav-open'));
    }
    if(backdrop && !backdrop._bound){
      backdrop._bound = true;
      backdrop.addEventListener('click', ()=> document.body.classList.remove('nav-open'));
    }
  }
  if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bind);
  else bind();
})();

// Expand / collapse full agent conversation
function toggleDebateExpand(){
  const panel = document.querySelector('.desk-transcript-panel');
  if(!panel) return;
  panel.classList.toggle('expanded');
  const btn = $('debateExpandBtn');
  if(btn) btn.textContent = panel.classList.contains('expanded') ? 'COLLAPSE CHAT' : 'EXPAND FULL CHAT';
  const el = $('debateTranscript');
  if(el) el.scrollTop = el.scrollHeight;
}
window.toggleDebateExpand = toggleDebateExpand;
window.goPage = goPage;
window.refreshInstrumentRegistry = refreshInstrumentRegistry;
function sideClass(side){return String(side||'FLAT').toLowerCase();}

function renderManagement(s){
  const m=s.management||{}; const live=m.live||{}; const managed=[...(m.managed||[]), ...(live.managed||[])]; const actions=[...(m.actions||[]), ...(live.actions||[])]; const alerts=[...(m.alerts||[]), ...(live.alerts||[])];
  const set=s.settings||{};
  if($('managerMode')){ $('managerMode').textContent=set.position_manager_enabled?'MANAGER READY':'MANAGER OFF'; $('managerMode').className=set.position_manager_enabled?'live-tag':'locked-tag'; }
  if($('managedCount'))$('managedCount').textContent=`MANAGED ${managed.length}`;
  if($('managementActionCount'))$('managementActionCount').textContent=`ACTIONS ${actions.length}`;
  if($('managementAlertCount'))$('managementAlertCount').textContent=`ALERTS ${alerts.length}`;
  if($('beTrigger'))$('beTrigger').textContent=`${fmt(set.break_even_r,2)}R`;
  if($('beOffset'))$('beOffset').textContent=`${fmt(set.break_even_offset_r,2)}R`;
  if($('trailStart'))$('trailStart').textContent=`${fmt(set.trailing_start_r,2)}R`;
  if($('trailMult'))$('trailMult').textContent=`${fmt(set.trailing_atr_mult,2)}× ATR`;
  if($('partialEnabled'))$('partialEnabled').textContent=set.partial_exit_enabled?'ON':'OFF';
  if($('partialTrigger'))$('partialTrigger').textContent=`${fmt(set.partial_exit_r,2)}R`;
  if($('partialPercent'))$('partialPercent').textContent=`${fmt(set.partial_exit_percent,0)}%`;
  if($('partialStatus'))$('partialStatus').textContent=set.partial_exit_enabled?'ARMED (CONFIGURED)':'INACTIVE';
  if($('managedTable'))$('managedTable').innerHTML=managed.map(p=>`<tr><td>${esc(p.ticket)}</td><td>${esc(p.symbol)}</td><td class="${p.side==='BUY'?'position-buy':'position-sell'}"><b>${esc(p.side)}</b></td><td class="${p.r_multiple>=0?'r-positive':'r-negative'}">${fmt(p.r_multiple,2)}R</td><td>${fmt(p.entry)}</td><td>${fmt(p.initial_stop)}</td><td>${fmt(p.current_sl)}</td><td>${fmt(p.current_tp)}</td><td>${fmt(p.profit)}</td></tr>`).join('')||'<tr><td colspan="9">No positions are currently eligible for adaptive management.</td></tr>';
  const all=[...actions.map(x=>({time:new Date().toISOString(),type:'ACTION',payload:x})),...alerts.map(x=>({time:new Date().toISOString(),type:'ALERT',payload:x}))];
  if($('managementEvents'))$('managementEvents').innerHTML=all.map(e=>`<div class="log ${e.type==='ALERT'?'error':''}"><span class="time">${new Date(e.time).toLocaleTimeString()}</span><b>${esc(e.type)}</b><div class="muted" style="margin-top:4px">${esc(JSON.stringify(e.payload||{}))}</div></div>`).join('')||'<div class="log">No management actions in the latest cycle.</div>';
}
let _detectedDeskFilter = 'all';
let _latestMarketUniverseRows = [];
let _marketDeskTabs = Object.create(null);

function isDetectedMarketSetup(r){
  const status=String(r?.director_candidate_status||r?.candidate_status||'').toUpperCase();
  const side=String(r?.candidate_side||'').toUpperCase();
  return status==='CANDIDATE' && (side==='BUY'||side==='SELL') && r?.quote_fresh!==false;
}
function deskRoutesForRow(r){
  const routes=Array.isArray(r?.market_desk_routes)?r.market_desk_routes.filter(Boolean):[];
  const desk=String(r?.market_desk||'').toLowerCase();
  if(!routes.length&&desk)routes.push(desk);
  return routes.map(x=>String(x).toLowerCase());
}
function setupBelongsToDesk(r,deskId){
  if(deskId==='all')return true;
  return deskRoutesForRow(r).includes(String(deskId).toLowerCase());
}
function setDetectedDeskFilter(deskId){
  const allowed=['all','forex','stocks','crypto','cfd','convert','indices'];
  _detectedDeskFilter=allowed.includes(String(deskId))?String(deskId):'all';
  document.querySelectorAll('[data-setup-filter]').forEach(btn=>btn.classList.toggle('active',btn.dataset.setupFilter===_detectedDeskFilter));
  if(window.__lastState)renderUniverse(window.__lastState);
}
window.setDetectedDeskFilter=setDetectedDeskFilter;
function focusDetectedSetup(symbol){
  _detectedDeskFilter='all';
  document.querySelectorAll('[data-setup-filter]').forEach(btn=>btn.classList.toggle('active',btn.dataset.setupFilter==='all'));
  if(window.__lastState)renderUniverse(window.__lastState);
  const target=Array.from(document.querySelectorAll('#universeTable tr[data-universe-symbol]')).find(tr=>tr.dataset.universeSymbol===String(symbol));
  const panel=document.querySelector('.detected-setups-panel');
  (target||panel)?.scrollIntoView({behavior:'smooth',block:'center'});
  if(target){target.classList.add('setup-row-focus');setTimeout(()=>target.classList.remove('setup-row-focus'),1800);}
}
window.focusDetectedSetup=focusDetectedSetup;

function renderUniverse(s){
  const u=s.universe||{};
  const enabled=!!u.enabled;
  const rows=Array.isArray(u.results)?u.results:[];
  _latestMarketUniverseRows=rows;
  const allDetected=rows.filter(isDetectedMarketSetup).sort((a,b)=>Number(b.scanned_epoch||0)-Number(a.scanned_epoch||0));
  const serverTotal=Number(u.total_candidates ?? allDetected.length);
  if($('universeScanBadge')){
    $('universeScanBadge').textContent=enabled?'ALL-SYMBOL SCAN ON':'ACTIVE SYMBOL ONLY';
    $('universeScanBadge').className=enabled?'live-tag':'locked-tag';
  }
  if($('universeScanStatus')){
    const n=Number(u.analyzed_count||0), total=Number(u.total_available||0), p=u.scan_progress||{};
    const progressText=enabled?` · ${n.toLocaleString()} / ${total.toLocaleString()} symbols completed (${Number(p.coverage_pct||0).toFixed(1)}%) · ${Number(p.jobs_queued||u.queued_count||0)} queued · ${Number(p.jobs_running||0)} running · ${Number(p.failed_symbols||0)} failed · cap ${u.scan_limit||'—'}`:'';
    const blocked=u.blocked_reason||u.worker?.blocked_reason;
    $('universeScanStatus').textContent=(u.status||(enabled?'Scanning broker symbols':'Enable all-symbol analysis in Settings'))+progressText+(blocked?` · ${blocked}`:'');
  }
  if($('detectedSetupCount')){
    $('detectedSetupCount').textContent=`${allDetected.length.toLocaleString()} SETUP${allDetected.length===1?'':'S'}`;
    $('detectedSetupCount').className=allDetected.length?'live-tag':'locked-tag';
  }
  const table=$('universeTable');
  if(!table)return;
  const filtered=allDetected.filter(r=>setupBelongsToDesk(r,_detectedDeskFilter));
  const serverMeta=Pagers.universe._serverMeta;
  const pageData=serverMeta ? {...serverMeta,slice:filtered} : paginate(filtered,'universe');
  Pagers.universe._meta=pageData;
  renderPager('universePager','universe',()=>{ const t=$('universeTable'); if(t) t.dataset.signature=''; _safeRefresh(); });
  const visible=serverMeta ? filtered : pageData.slice;
  const signature=[_detectedDeskFilter,enabled?1:0,allDetected.length,u.status||'',...visible.map(r=>`${r.symbol||''}:${r.director_candidate_status||r.candidate_status||''}:${r.candidate_side||''}:${r.candidate_score??r.desk_score??0}:${r.quote_status||''}:${r.specialist_review_status||''}:${r.specialist_decision||''}:${deskRoutesForRow(r).join(',')}`)].join('|');
  if(table.dataset.signature===signature){
    visible.forEach(r=>{
      const tr=Array.from(table.querySelectorAll('tr[data-universe-symbol]')).find(x=>x.dataset.universeSymbol===String(r.symbol||''));
      if(tr){if(tr.children[4])tr.children[4].textContent=fmt(r.price);if(tr.children[5])tr.children[5].textContent=fmt(r.spread);}
    });
    return;
  }
  table.dataset.signature=signature;
  if(!visible.length){
    const empty=allDetected.length
      ? `No detected setups for ${_detectedDeskFilter==='all'?'this filter':_detectedDeskFilter+'.' }` 
      : (enabled?'No qualifying setup has been detected yet. The scanner is still evaluating available symbols; no inactive or WAIT rows are shown here.':'No qualifying setup has been detected for the active symbol. Enable all-symbol analysis in Settings for broader discovery.');
    table.innerHTML=`<tr class="empty-setup-row"><td colspan="9"><div class="empty-setup-state"><span class="empty-setup-icon">◎</span><b>${esc(empty)}</b><span>Only completed-bar candidates with a BUY or SELL side appear in this view. No setup is not a trading signal.</span></div></td></tr>`;
    return;
  }
  table.innerHTML=visible.map(r=>{
    const views=Array.isArray(r.agent_views)?r.agent_views:[];
    const count=Number(r.agent_count??views.length);
    const detail=views.map(a=>`<div class="universe-agent"><div><b>${esc(a.agent||'Analyst')}</b> <span class="muted">${esc(a.role||'')}</span></div><div class="${sideClass(a.side)==='buy'?'position-buy':sideClass(a.side)==='sell'?'position-sell':'muted'}">${esc(a.side||'FLAT')} · desk score ${pct(a.confidence)}</div><p>${esc(a.thesis||'No thesis supplied.')}</p>${(a.evidence||[]).length?`<ul>${a.evidence.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`:''}</div>`).join('');
    const active=!!r.primary_symbol;
    const badge=active?'<span class="live-tag">ACTIVE SYMBOL</span>':'<span class="locked-tag">RESEARCH ONLY</span>';
    const routes=deskRoutesForRow(r);
    const routeLabels=routes.map(x=>({forex:'Forex',stocks:'Stocks',crypto:'Crypto',cfd:'CFD',convert:'Convert',indices:'Indices'}[x]||x)).join(' + ')||r.market_desk||'Unmapped';
    const side=String(r.candidate_side||'').toUpperCase();
    const quoteFresh=r.quote_fresh!==false;
    const quoteClass=quoteFresh?'r-positive':'danger-text';
    const quoteLabel=quoteFresh?(r.quote_status||'Fresh; timestamp checks passed'):(r.quote_status||'Unverified / stale');
    const scanEpoch=Number(r.scanned_epoch||0);
    const review=String(r.specialist_review_status||'NOT ROUTED').replaceAll('_',' ');
    const reviewedDecision=r.specialist_decision?` · ${String(r.specialist_decision).toUpperCase()}`:'';
    const viewsContent=detail||'<div class="muted">Expand to request the latest specialist evidence for this detected setup.</div>';
    const statusClass=['DEEP_REVIEWED'].includes(String(r.candidate_status||'').toUpperCase())?'live-tag':(['REVIEW_REJECTED','BLOCKED_DATA'].includes(String(r.candidate_status||'').toUpperCase())?'danger-text':'locked-tag');
    return `<tr data-universe-symbol="${esc(r.symbol||'')}" data-scan-epoch="${scanEpoch}"><td><div class="setup-symbol-cell"><b>${esc(r.symbol||'—')}</b>${badge}</div><div class="muted setup-subline">${esc(String(r.product_type||'market').replaceAll('_',' '))}</div></td><td><b>${esc(routeLabels)}</b><div class="muted setup-subline">${esc(r.market_subuniverses?Object.values(r.market_subuniverses).slice(0,2).join(' · '):'')}</div></td><td><span class="setup-direction ${side==='BUY'?'buy':'sell'}">${side==='BUY'?'▲ BUY':'▼ SELL'}</span></td><td><div class="setup-score"><b>${pct(Number(r.candidate_score??r.desk_score??0))}</b><div><span style="width:${Math.max(0,Math.min(100,Number(r.candidate_score??r.desk_score??0)*100))}%"></span></div></div></td><td>${fmt(r.price)}</td><td>${fmt(r.spread)}</td><td class="${quoteClass}">${esc(quoteLabel)}</td><td><span class="${statusClass}">${esc(review)}${esc(reviewedDecision)}</span><div class="muted setup-subline">Director: ${esc(r.director_candidate_status||r.candidate_status||'CANDIDATE')}</div></td><td><details data-universe-detail="${esc(r.symbol||'')}" data-scan-epoch="${scanEpoch}" ${detail?'data-loaded="true"':''}><summary>${count} analyst view(s) · open</summary><div class="universe-detail-list">${viewsContent}</div></details><button class="btn btn-small setup-jump-btn" type="button" data-focus-symbol="${esc(r.symbol||'')}">Inspect setup</button><div class="muted setup-subline">Research-only · no order authority</div></td></tr>`;
  }).join('');
}

function setMarketDeskTab(deskId,tab){
  const allowedDesk=['forex','stocks','crypto','cfd','convert','indices'];
  const allowedTabs=['desk','setups','reviews'];
  if(!allowedDesk.includes(String(deskId))||!allowedTabs.includes(String(tab)))return;
  _marketDeskTabs[deskId]=tab;
  if(window.__lastState)renderMarketDesks(window.__lastState);
}
window.setMarketDeskTab=setMarketDeskTab;

function renderMarketDesks(s){
  const summary=s.market_desks||{};
  const desks=Array.isArray(summary.desks)?summary.desks:[];
  const totals=summary.totals||{};
  const grid=$('marketDeskGrid');
  const enabled=Array.isArray(summary.enabled_desks)?summary.enabled_desks:[];
  const health=$('marketDeskHealth');
  const meta=$('marketDeskMeta');
  if(health){health.textContent=`${enabled.length}/6 DESKS ENABLED`;health.className=enabled.length===6?'live-tag':'locked-tag';}
  if(meta){meta.textContent=`Ruleset ${summary.ruleset_version||'—'} · screens and qualifying candidate triggers are reported separately from live trade authority.`;}
  if($('marketDeskScannedTotal'))$('marketDeskScannedTotal').textContent=Number(totals.symbols_scanned||0).toLocaleString();
  if($('marketDeskCandidatesTotal'))$('marketDeskCandidatesTotal').textContent=Number(totals.candidate_triggers||0).toLocaleString();
  if($('marketDeskReviewsTotal'))$('marketDeskReviewsTotal').textContent=Number(totals.specialist_reviews_queued||0).toLocaleString();
  if(!grid)return;
  const number=v=>Number(v||0).toLocaleString();
  const latestRows=Array.isArray(_latestMarketUniverseRows)?_latestMarketUniverseRows:[];
  const detected=latestRows.filter(isDetectedMarketSetup).sort((a,b)=>Number(b.scanned_epoch||0)-Number(a.scanned_epoch||0));
  const labels={forex:'FOREX',stocks:'EQUITIES',crypto:'CRYPTO',cfd:'CONTRACTS',convert:'CONVERT',indices:'INDICES'};
  const marks={forex:'FX',stocks:'EQ',crypto:'₿',cfd:'CFD',convert:'CV',indices:'IX'};
  const accents={forex:'fx',stocks:'equity',crypto:'crypto',cfd:'contract',convert:'convert',indices:'index'};
  grid.innerHTML=desks.map((d,idx)=>{
    const id=String(d.desk_id||'').toLowerCase();
    if(!['forex','stocks','crypto','cfd','convert','indices'].includes(id))return '';
    const on=!!d.enabled;
    const sub=Object.entries(d.subuniverses||{}).sort((a,b)=>b[1]-a[1]).slice(0,3).map(([name,n])=>`${name.replaceAll('_',' ')} <b>${number(n)}</b>`).join(' <i>·</i> ');
    const last=d.last_symbol?`Last scan <b>${esc(d.last_symbol)}</b>`:'Awaiting first completed scan';
    const err=d.errors?`${number(d.errors)} issue${Number(d.errors)===1?'':'s'}`:'';
    const tab=_marketDeskTabs[id]||'desk';
    const deskSetups=detected.filter(r=>setupBelongsToDesk(r,id));
    const reviews=deskSetups.filter(r=>String(r.specialist_review_status||'').toUpperCase()!=='NOT_ROUTED');
    const setupHtml=deskSetups.slice(0,3).map(r=>{
      const side=String(r.candidate_side||'').toUpperCase();
      return `<div class="desk-compact-setup"><div><b>${esc(r.symbol||'—')}</b><span class="setup-direction ${side==='BUY'?'buy':'sell'}">${side==='BUY'?'▲ BUY':'▼ SELL'}</span></div><div class="desk-compact-meta"><span>Score <b>${pct(Number(r.candidate_score??r.desk_score??0))}</b></span><span>${esc(String(r.specialist_review_status||'NOT ROUTED').replaceAll('_',' '))}</span></div><p>${esc(r.thesis||'Conditions met the desk candidate screen.')}</p><button type="button" class="desk-link-button" data-focus-symbol="${esc(r.symbol||'')}">Inspect in setup list ↗</button></div>`;
    }).join('');
    const reviewHtml=reviews.slice(0,4).map(r=>`<div class="desk-review-row"><div><b>${esc(r.symbol||'—')}</b><small>${esc(String(r.candidate_side||'FLAT'))} · ${pct(Number(r.candidate_score??r.desk_score??0))}</small></div><span class="${String(r.candidate_status||'').toUpperCase()==='REVIEW_REJECTED'?'danger-text':'locked-tag'}">${esc(String(r.specialist_review_status||'QUEUED').replaceAll('_',' '))}</span></div>`).join('');
    let body='';
    if(tab==='setups')body=setupHtml||`<div class="desk-empty-state"><span>◎</span><b>No detected setup</b><small>No qualifying BUY/SELL candidate is currently routed to this desk.</small></div>`;
    else if(tab==='reviews')body=reviewHtml||`<div class="desk-empty-state"><span>≋</span><b>No specialist review queued</b><small>A review appears here only after the director detects a qualifying setup.</small></div>`;
    else body=`<p class="market-director-mandate">${esc(d.mandate||'Deterministic market screening and routing.')}</p><div class="market-director-metrics"><span><b>${number(d.symbols_scanned)}</b><small>SCREENS</small></span><span><b>${number(d.candidate_triggers)}</b><small>TRIGGERS</small></span><span><b>${number(d.specialist_reviews_queued)}</b><small>QUEUED</small></span><span><b>${number(d.specialist_reviews_completed)}</b><small>REVIEWED</small></span></div><div class="desk-last-scan"><span class="status-dot ${on?'':'off'}"></span>${last}${err?` <em>· ${esc(err)}</em>`:''}</div>${sub?`<div class="muted market-subuniverses">${sub}</div>`:''}${d.last_error?`<div class="danger-text">${esc(d.last_error)}</div>`:''}`;
    const tabLabel=tab==='desk'?'DESK OVERVIEW':tab==='setups'?'DETECTED SETUPS':'SPECIALIST REVIEWS';
    const tag=tab==='setups'?`${deskSetups.length} FOUND`:tab==='reviews'?`${reviews.length} IN REVIEW`:on?'MONITORING':'DISABLED';
    const tagClass=tab==='setups'&&deskSetups.length?'live-tag':on?'locked-tag':'locked-tag';
    return `<article class="market-director-card ${accents[id]} ${on?'':'disabled'}" data-desk-card="${id}"><div class="desk-card-accent"></div><header class="market-director-card-head"><div class="desk-identity"><span class="desk-mark">${marks[id]}</span><div><small class="desk-index">DESK 0${idx+1} · ${labels[id]}</small><h3>${esc(d.label||id)}</h3></div></div><span class="${on?'live-tag':'locked-tag'}">${on?'ONLINE':'DISABLED'}</span></header><nav class="desk-card-nav" aria-label="${esc(d.label||id)} navigation"><button type="button" class="${tab==='desk'?'active':''}" aria-selected="${tab==='desk'}" onclick="setMarketDeskTab('${id}','desk')">Overview</button><button type="button" class="${tab==='setups'?'active':''}" aria-selected="${tab==='setups'}" onclick="setMarketDeskTab('${id}','setups')">Setups <span>${deskSetups.length}</span></button><button type="button" class="${tab==='reviews'?'active':''}" aria-selected="${tab==='reviews'}" onclick="setMarketDeskTab('${id}','reviews')">Reviews <span>${reviews.length}</span></button></nav><div class="desk-card-content"><div class="desk-content-title"><span>${tabLabel}</span><span class="${tagClass}">${esc(tag)}</span></div>${body}</div></article>`;
  }).join('')||'<div class="market-empty">No market-director telemetry is available yet.</div>';
}

if(!window.__marketDirectorNavBound){
  document.addEventListener('click',event=>{
    const target=event.target.closest('[data-focus-symbol]');
    if(target){event.preventDefault();focusDetectedSetup(target.dataset.focusSymbol||'');}
  });
  window.__marketDirectorNavBound=true;
}

let _instrumentRegistryLoaded = false;
async function refreshInstrumentRegistry(force=false){
  const q=String($('instrumentRegistrySearch')?.value||'').trim();
  const desk=String($('instrumentRegistryDesk')?.value||'').trim();
  const queryKey=`${q}|${desk}`;
  if(Pagers.registry.queryKey!==queryKey){ Pagers.registry.queryKey=queryKey; Pagers.registry.page=1; }
  const params=new URLSearchParams({page_size:String(Pagers.registry.pageSize),page:String(Pagers.registry.page),q,desk});
  if(force) params.set('refresh','1');
  const summary=$('instrumentRegistrySummary');
  if(summary) summary.textContent=force?'Refreshing symbol metadata from the connected MT5 terminal…':'Loading cached broker symbol metadata…';
  try{
    const payload=await get(`/api/market/registry?${params.toString()}`);
    renderInstrumentRegistry(payload);
    _instrumentRegistryLoaded=true;
    return payload;
  }catch(e){
    if(summary) summary.textContent=`Registry unavailable: ${e.message||'request failed'}. Live entry authorization remains independent and fail-closed.`;
    throw e;
  }
}
function renderInstrumentRegistry(payload){
  const summary=payload.summary||{};
  const complete=payload.source_complete===true;
  const status=$('instrumentRegistrySummary');
  if(status){
    const desks=Object.entries(summary.by_desk||{}).map(([k,v])=>`${k}: ${v}`).join(' · ')||'No mapped instruments yet';
    status.textContent=`${Number(summary.total_active||0)} active registered · ${Number(summary.entry_metadata_ready||0)} metadata-ready for further checks · ${Number(summary.needs_review||0)} need classification review · catalogue ${complete?'count-confirmed':'not confirmed complete'} · ${desks}`;
  }
  const rows=Array.isArray(payload.symbols)?payload.symbols:[];
  const table=$('instrumentRegistryTable');
  if(!table)return;
  const total=Number(payload.total??rows.length);
  const page=Math.max(1,Number(payload.page??Pagers.registry.page));
  const pageSize=Math.max(1,Number(payload.page_size??Pagers.registry.pageSize));
  const offset=Math.max(0,Number(payload.offset??((page-1)*pageSize)));
  Pagers.registry.page=page;
  Pagers.registry.pageSize=pageSize;
  Pagers.registry._meta={slice:rows,page,pages:Math.max(1,Number(payload.pages??Math.ceil(total/pageSize)??1)),
    total,pageSize,start:total?offset+1:0,end:Math.min(offset+rows.length,total)};
  renderPager('registryPager', 'registry', () => { refreshInstrumentRegistry(false).catch(()=>{}); });
  table.innerHTML=pageData.slice.map(r=>{
    const exact=r.broker_symbol_exact||r.raw_metadata?.name||r.broker_symbol||'—';
    const reasons=Array.isArray(r.review_reasons)?[...r.review_reasons]:[];
    if(r.reason && !reasons.includes(String(r.reason))) reasons.unshift(String(r.reason));
    const entry=!!r.entry_eligible;
    return `<tr>
      <td><b>${esc(exact)}</b></td>
      <td>${esc(r.description||r.path||'—')}</td>
      <td>${esc(r.product_type||'unknown')}${r.cfd_overlay_required?' · underlying analysis required':''}</td>
      <td>${esc(r.underlying_market||r.market_desk||'unknown')}</td>
      <td class="muted" style="max-width:180px">${esc((r.notes||r.path||'').toString().slice(0,80))}</td>
      <td><span class="badge ${r.classification_status==='verified'?'badge-good':'badge-warn'}">${esc(r.classification_status||'needs_review')}</span></td>
      <td><span class="badge ${entry?'badge-good':'badge-bad'}">${entry?'Metadata ready':'Blocked / review'}</span></td>
      <td style="max-width:280px;font-size:11.5px">${esc(reasons.join(' · ')||'No registry warning; other live gates still apply.')}</td>
    </tr>`;
  }).join('')||'<tr><td colspan="7">No rows matched. If the broker symbol is missing, refresh the catalogue or search for its exact terminal name.</td></tr>';
}

// Expand a row to request its detailed analysis only once per completed scan.
// A new scan timestamp invalidates the cached content automatically.
if(!window.__universeDetailToggleBound){
  document.addEventListener('toggle', async (event)=>{
    const details=event.target;
    if(!details || details.tagName!=='DETAILS' || !details.matches('[data-universe-detail]') || !details.open) return;
    const symbol=details.dataset.universeDetail;
    const scanEpoch=details.dataset.scanEpoch||'0';
    const cacheKey=`${symbol}:${scanEpoch}`;
    window.__universeDetailCache=window.__universeDetailCache||new Map();
    const holder=details.querySelector('.universe-detail-list');
    if(!holder) return;
    if(details.dataset.loaded==='true' && details.dataset.cacheKey===cacheKey) return;
    const cached=window.__universeDetailCache.get(cacheKey);
    if(cached){holder.innerHTML=cached;details.dataset.loaded='true';details.dataset.cacheKey=cacheKey;return;}
    holder.innerHTML='<div class="muted">Loading pair-specific analyst evidence…</div>';
    try{
      const payload=await get(`/api/universe/detail?symbol=${encodeURIComponent(symbol)}`);
      const detail=payload.detail||{};
      const views=Array.isArray(detail.agent_views)?detail.agent_views:[];
      const html=views.map(a=>`<div class="universe-agent"><div><b>${esc(a.agent||'Analyst')}</b> <span class="muted">${esc(a.role||'')}</span></div><div class="${sideClass(a.side)==='buy'?'position-buy':sideClass(a.side)==='sell'?'position-sell':'muted'}">${esc(a.side||'FLAT')} · desk score ${pct(a.confidence)}</div><p>${esc(a.thesis||'No thesis supplied.')}</p>${(a.evidence||[]).length?`<ul>${a.evidence.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`:''}</div>`).join('') || '<div class="muted">No analyst-specific views were returned for this symbol.</div>';
      window.__universeDetailCache.set(cacheKey,html);
      holder.innerHTML=html;details.dataset.loaded='true';details.dataset.cacheKey=cacheKey;
    }catch(e){
      holder.innerHTML=`<div class="muted">${esc(e.message||'Pair-specific analysis is not available yet.')} The symbol may still be waiting for its scan turn.</div>`;
    }
  },true);
  window.__universeDetailToggleBound=true;
}
function syncSettingsForm(settings){
  const s=settings||{};
  const boolMap={
    analyze_all_symbols_enabled:'setAnalyzeAllSymbols',
    position_manager_enabled:'setExitManagerEnabled',
    break_even_enabled:'setBreakEvenEnabled',
    trailing_enabled:'setTrailingEnabled',
    trailing_use_structure:'setTrailingStructure',
    partial_exit_enabled:'setPartialEnabled',
    second_partial_exit_enabled:'setSecondPartialEnabled',
    peak_giveback_enabled:'setPeakGivebackEnabled',
    time_stop_enabled:'setTimeStopEnabled',
    close_if_unprotected:'setCloseUnprotected',
    ai_invalidation_enabled:'setAIInvalidation'
  };
  for(const [key,id] of Object.entries(boolMap)){
    const el=$(id); if(el && document.activeElement!==el) el.checked=!!s[key];
  }
  const directorSettings={forex:'setDeskForex',stocks:'setDeskStocks',crypto:'setDeskCrypto',cfd:'setDeskCfd',convert:'setDeskConvert',indices:'setDeskIndices'};
  const enabledDirectors=new Set(Array.isArray(s.market_desks_enabled)?s.market_desks_enabled:['forex','stocks','crypto','cfd','convert','indices']);
  for(const [desk,id] of Object.entries(directorSettings)){
    const el=$(id); if(el && document.activeElement!==el) el.checked=enabledDirectors.has(desk);
  }
  const valueMap={
    all_symbol_scan_limit:'setSymbolScanLimit', all_symbol_scan_batch_size:'setSymbolScanBatchSize', all_symbol_rescan_seconds:'setSymbolRescanSeconds', break_even_r:'setBreakEvenR',
    break_even_offset_r:'setBreakEvenOffsetR', trailing_start_r:'setTrailingStartR',
    trailing_atr_mult:'setTrailingAtrMult', partial_exit_r:'setPartialR',
    partial_exit_percent:'setPartialPercent', second_partial_exit_r:'setSecondPartialR',
    second_partial_exit_percent:'setSecondPartialPercent', peak_giveback_trigger_r:'setPeakGivebackTriggerR',
    peak_giveback_r:'setPeakGivebackR', max_hold_minutes:'setMaxHold',
    time_stop_min_progress_r:'setTimeProgressR', ai_invalidation_confidence:'setAIInvalidationConfidence',
    ai_invalidation_cycles:'setAIInvalidationCycles'
  };
  for(const [key,id] of Object.entries(valueMap)){
    const el=$(id); if(el && document.activeElement!==el && s[key]!==undefined && s[key]!==null) el.value=s[key];
  }
}
function renderPerformance(s){
  const p=s.performance||{}; const sum=p.summary||{};
  $('perfClosed').textContent=fmt(sum.closed,0);$('perfWinRate').textContent=`${fmt(sum.win_rate,1)}%`;$('perfPnl').textContent=fmt(sum.realized_pnl);$('perfAvgR').textContent=`${fmt(sum.avg_r,2)}R`;
  $('agentPerfTable').innerHTML=(p.agents||[]).map(a=>`<tr><td><b>${esc(a.agent)}</b></td><td>${fmt(a.observations,0)}</td><td>${fmt(a.directional_rate,1)}%</td><td>${fmt(a.win_rate,1)}%</td><td class="${a.pnl>=0?'r-positive':'r-negative'}">${fmt(a.pnl)}</td><td>${fmt(a.weighted_score,2)}</td></tr>`).join('')||'<tr><td colspan="6">No attribution data yet. Closed trades are required for outcome calibration.</td></tr>';
  $('recentOutcomes').innerHTML=(p.recent||[]).filter(x=>Number(x.closed)).slice(0,20).map(x=>`<div class="log"><span class="time">${new Date(x.closed_at||x.created).toLocaleString()}</span><b class="${x.outcome==='WIN'?'r-positive':x.outcome==='LOSS'?'r-negative':'perf-flat'}">${esc(x.outcome||'—')}</b> ${esc(x.symbol)} ${esc(x.decision)} · ${fmt(x.realized_r,2)}R · ${fmt(x.realized_pnl)}</div>`).join('')||'<div class="log">No closed outcomes recorded yet.</div>';
  if($('calibrationTable'))$('calibrationTable').innerHTML=(p.calibration||[]).map(b=>`<tr><td>${esc(b.bucket)}</td><td>${fmt(b.trades,0)}</td><td>${fmt(b.predicted_confidence*100,1)}%</td><td>${fmt(b.win_rate,1)}%</td></tr>`).join('')||'<tr><td colspan="4">No closed outcomes yet.</td></tr>';
}
function renderAgents(agents, status={}, mesh={}){
  const floor = mesh.debate_floor || null;
  const votes = floor?.final_votes || {};

  // Hero metrics
  if(floor){
    setTxt('deskAgreement', 'AGREEMENT ' + (floor.agreement || '—'));
    setTxt('deskRounds', 'ROUNDS ' + (floor.rounds || '—'));
    setTxt('deskUtterances', 'UTTERANCES ' + ((floor.transcript||[]).length));
    setTxt('deskFloorStatus', floor.agreement === 'WAIT' ? 'NO SETUP — DEBATE OPEN' : ('SETUP ' + floor.agreement));
    setTxt('deskFloorSub', floor.agreement_rationale || 'Multi-round interactive floor');
    setTxt('deskConsensus', pct(floor.consensus_strength ?? floor.agreement_confidence));
    setTxt('deskConsensusSub', 'Uncalibrated evidence consensus, not win probability · threshold ' + pct(floor.consensus_threshold || 0.62));
    const risk = floor.risk_debate || mesh.risk_debate || {};
    setTxt('deskRiskPref', risk.preferred || '—');
    setTxt('deskRiskSub', (risk.blockers && risk.blockers.length) ? (risk.blockers.length + ' blockers') : 'Risk committee clear');
    setTxt('deskDissentCount', String((floor.dissent||[]).length));
    setTxt('deskDissentSub', (floor.dissent||[]).length ? 'Material opposing desks' : 'No material dissent');
    setHtml('deskRationale', esc(floor.agreement_rationale || '—'));
    setHtml('deskDissentList', (floor.dissent||[]).map(d => `<div class="zone">${esc(d)}</div>`).join('') || '');
  }

  // Seats
  if($('agentCards')){
    const list = agents || [];
    $('agentCards').innerHTML = list.map(s => {
      const st = status[s.agent] || {};
      const v = votes[s.agent] || {};
      const stance = (v.stance || s.side || 'FLAT').toUpperCase();
      const conf = v.confidence != null ? v.confidence : s.confidence;
      const sideCls = stance === 'BUY' ? 'buy' : stance === 'SELL' ? 'sell' : 'flat';
      const voteReason = v.vote_exclusion_reason || '';
      return `<div class="desk-seat ${sideCls}" title="${esc(voteReason)}">
        <div class="desk-seat-head">
          <div>
            <div class="panel-kicker">${esc(s.role || 'SPECIALIST')}</div>
            <h3>${esc(s.agent)}</h3>
            <div class="desk-seat-meta">${esc(s.source || st.source || 'deterministic')} · input adequacy ${pct(s.data_quality)} · ${v.eligible_to_vote===false?'EXCLUDED FROM VOTE':'VOTE-ELIGIBLE'}</div>
            ${v.eligible_to_vote===false && voteReason ? `<div class="muted" title="${esc(voteReason)}">Reason: ${esc(voteReason)}</div>` : ''}
          </div>
          <span class="side-badge ${sideCls}">${esc(stance)}</span>
        </div>
        <div class="desk-seat-body">${esc(s.thesis || 'No thesis')}</div>
        <div class="desk-seat-foot">
          <span>Heuristic conviction ${pct(conf)} · score ${fmt(s.score,3)}</span>
          <span>${esc(st.state || 'READY')}</span>
        </div>
      </div>`;
    }).join('') || '<div class="muted">No agent reports this cycle.</div>';
  }

  // Compact summary on dashboard rail
  if($('agentSummary')){
    const list = agents || [];
    $('agentSummary').innerHTML = list.slice(0,8).map(s => {
      const v = votes[s.agent] || {};
      const stance = (v.stance || s.side || 'FLAT').toUpperCase();
      const cls = stance === 'BUY' ? 'buy' : stance === 'SELL' ? 'sell' : 'flat';
      return `<div class="agent-row"><div><div class="agent-title">${esc(s.agent)}</div><div class="agent-meta">${esc((s.thesis||'').slice(0,80))}</div></div><span class="side-badge ${cls}">${esc(stance)} · H ${pct(v.confidence!=null?v.confidence:s.confidence)}</span></div>`;
    }).join('') || '<div class="muted">Waiting for agents…</div>';
  }

  // Vote board
  if($('deskVoteBoard')){
    const entries = Object.entries(votes);
    if(entries.length){
      $('deskVoteBoard').innerHTML = entries.map(([name,v]) => {
        const st = (v.stance||'FLAT').toUpperCase();
        const col = st==='BUY'?'var(--green)':st==='SELL'?'var(--red)':'var(--muted)';
        const reason = v.vote_exclusion_reason || '';
        return `<div class="vote-cell"><span title="${esc(reason)}">${esc(name)}</span><b title="Heuristic conviction score; not a calibrated probability" style="color:${col}">${esc(st)} · H ${pct(v.confidence)} · ${v.eligible_to_vote===false?'EXCLUDED':'ELIGIBLE'}</b>${v.eligible_to_vote===false && reason ? `<small class="muted">${esc(reason)}</small>` : ''}</div>`;
      }).join('');
    } else {
      $('deskVoteBoard').innerHTML = '<div class="muted">Votes appear after debate floor completes.</div>';
    }
  }

  // Transcript
  renderDebateTranscript(floor?.transcript || []);
}

let _debatePhaseFilter = 'ALL';
function filterDebatePhase(phase){
  _debatePhaseFilter = phase;
  document.querySelectorAll('.desk-phase-tabs .chart-tf-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.phase === phase);
  });
  // re-render from last state if available
  if(window.__lastState){
    const mesh = window.__lastState.ai_mesh || window.__lastState.mesh || {};
    renderDebateTranscript((mesh.debate_floor||{}).transcript || []);
  }
}

/* Stable transcript: only re-render when content actually changes (stops blink). */
let _lastTranscriptKey = '';
function _transcriptKey(lines, filter){
  const payload = JSON.stringify([filter, lines || []]);
  // Small deterministic hash avoids storing a huge JSON key on the DOM node.
  let h = 2166136261;
  for(let i=0;i<payload.length;i++){ h ^= payload.charCodeAt(i); h = Math.imul(h, 16777619); }
  return `${(lines||[]).length}:${(h>>>0).toString(16)}:${filter}`;
}

function renderDebateTranscript(lines){
  const el = $('debateTranscript');
  if(!el) return;
  const filtered = (_debatePhaseFilter === 'ALL')
    ? (lines || [])
    : (lines || []).filter(u => (u.phase_code || u.phase || '') === _debatePhaseFilter || (_debatePhaseFilter==='SYNTHESIS' && ((u.phase_code||u.phase)==='SYNTHESIS'||(u.phase_code||u.phase)==='VOTE')));

  const key = _transcriptKey(filtered, _debatePhaseFilter);
  // Skip full DOM rebuild when nothing material changed — eliminates blink/flicker
  if(key === _lastTranscriptKey && el.dataset.hasContent === '1'){
    return;
  }
  _lastTranscriptKey = key;

  const wasNearBottom = (el.scrollHeight - el.scrollTop - el.clientHeight) < 140;
  const prevScroll = el.scrollTop;

  if(!filtered.length){
    el.innerHTML = '<div class="muted chat-empty">No discussion yet. Run a market analysis to see each specialist&#39;s view, the evidence behind it, the challenges raised, and the final decision.</div>';
    el.dataset.hasContent = '1';
    return;
  }
  const avatar = (name) => {
    const n = String(name||'?');
    const initials = n.replace(/[^A-Za-z0-9]/g,' ').trim().split(/\s+/).slice(0,2).map(w=>w[0]||'').join('').toUpperCase() || '?';
    let h = 0; for(let i=0;i<n.length;i++) h = (h*31 + n.charCodeAt(i))>>>0;
    const hues = [210,160,25,280,340,45,190];
    return { initials, hue: hues[h % hues.length] };
  };
  el.innerHTML = filtered.map((u, idx) => {
    const phase = String(u.phase_code||u.phase||'').toLowerCase();
    const stance = (u.stance_code||u.stance||'FLAT').toUpperCase();
    const stanceCls = stance==='BUY'?'stance-buy':stance==='SELL'?'stance-sell':'stance-flat';
    const isSystem = u.is_system === true;
    const av = avatar(u.speaker);
    const time = (u.timestamp||'').slice(11,19) || '';
    const body = esc(u.message||'').replace(/\n/g,'<br>');
    // Challenge and response bodies already explain the objections in plain English.
    // Suppress duplicate machine-style chips there; preserve safety blockers on the final decision.
    const chips = ['cross','rebuttal'].includes(phase) ? '' : [
      ...(u.challenges||[]).map(c => `<span class="debate-chip">${esc(c)}</span>`),
      ...(u.concessions||[]).map(c => `<span class="debate-chip ok">${esc(c)}</span>`),
    ].join('');
    return `<div class="chat-msg stable ${phase}${isSystem?' system':''}" data-idx="${idx}">
      <div class="chat-avatar" style="background:hsl(${av.hue} 55% 28%)">${av.initials}</div>
      <div class="chat-bubble">
        <div class="chat-meta">
          <span class="chat-name">${esc(u.speaker||'')}</span>
          <span class="chat-role">${esc(u.role||'')}</span>
          ${u.addressing ? `<span class="chat-to">→ To ${esc(u.addressing)}</span>` : ''}
          <span class="chat-phase">${esc(u.phase||'Discussion')} · Round ${u.round||'—'}</span>
          ${(u.resolution_label||u.resolution_status) ? `<span class="debate-chip">${esc(u.resolution_label||u.resolution_status)}</span>` : ''}
          <span class="${stanceCls} chat-stance" title="Heuristic conviction score; not a calibrated probability">${esc(u.stance_label||stance)} · Conviction ${pct(u.confidence)}</span>
          ${time ? `<span class="chat-time">${esc(time)}</span>` : ''}
        </div>
        <div class="chat-body">${body}</div>
        ${chips ? `<div class="debate-chips">${chips}</div>` : ''}
      </div>
    </div>`;
  }).join('');
  if(wasNearBottom || !el.dataset.hasContent){
    el.scrollTop = el.scrollHeight;
  } else {
    el.scrollTop = prevScroll;
  }
  el.dataset.hasContent = '1';
}

/* ── MT5-style advanced candlestick chart + indicators ─────────── */
const ChartState = { marketOpen: false, mt5Connected: false, demo: false, sessionReason: '', scaleLo: null, scaleHi: null, fixedBars: 80,
  tf: 'M5',
  candles: [],
  plan: null,
  crosshair: null,
  hoverIdx: -1,
  fetchedTf: null,
  fetchedSymbol: null,
  userSelectedSymbol: false,
  _historySeq: 0,
  _historyInFlightKey: '',
  _historyFailures: 0,
  _historyRetryAt: 0,
  _historyRetrySymbol: '',
  _historyRetryTf: '',
  _lastDataFingerprint: '',
  _historyError: '',
  indicators: {
    ema20: true,
    ema50: true,
    sma200: true,
    bb: true,
    vwap: false,
    rsi: true,
    macd: true,
    atr: false,
    // aliases matching HTML data-ind buttons
    sma: true,
    ema: true,
  },
};

function chartDataFingerprint(series){
  if(!Array.isArray(series) || !series.length) return 'empty';
  // Small rolling hash avoids allocating a large joined string on every poll.
  let hash=2166136261;
  for(const b of series){
    const values=[b.t??'',b.o,b.h,b.l,b.c,b.v??0];
    for(const value of values){
      const text=String(value);
      for(let i=0;i<text.length;i++) hash=Math.imul(hash ^ text.charCodeAt(i),16777619);
      hash=Math.imul(hash ^ 124,16777619);
    }
  }
  return `${series.length}:${series[0]?.t??''}:${series[series.length-1]?.t??''}:${hash>>>0}`;
}

async function loadChartHistory(symbol, tf, {force=false, silent=true, notify=false}={}){
  symbol=String(symbol||'').trim().toUpperCase();
  tf=String(tf||'M5').trim().toUpperCase();
  if(!symbol || !tf) return;
  const requestKey=`${symbol}:${tf}`;
  if(ChartState._historyInFlightKey===requestKey) return;
  if(!force && ChartState._historyRetrySymbol===symbol && ChartState._historyRetryTf===tf && Date.now()<ChartState._historyRetryAt) return;
  const seq=++ChartState._historySeq;
  ChartState._historyInFlightKey=requestKey;
  ChartState._lastHistoryFetchAt=Date.now();
  const previousSymbol=ChartState.fetchedSymbol;
  const previousTf=ChartState.fetchedTf;
  const hadMatchingBars=previousSymbol===symbol && previousTf===tf && Array.isArray(ChartState.candles) && ChartState.candles.length>0;
  try{
    const url=`/api/mt5/rates?symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(tf)}&count=500`;
    // Force a network read after a failure so the browser's short successful-response
    // cache cannot turn a transient empty history result into a repeated false failure.
    const data=await get(url,{silent,region:'candleCanvas',force:force||ChartState._historyFailures>0,retries:3});
    if(seq!==ChartState._historySeq || ChartState.symbol!==symbol || ChartState.tf!==tf) return;
    const responseSymbol=String(data.request_symbol||data.symbol||'').trim().toUpperCase();
    const responseTf=String(data.timeframe||tf).trim().toUpperCase();
    if(responseSymbol && responseSymbol!==symbol) throw new Error(`MT5 returned ${responseSymbol} while ${symbol} was requested.`);
    if(responseTf!==tf) throw new Error(`MT5 returned timeframe ${responseTf} while ${tf} was requested.`);
    const bars=normalizeSeries(data.bars||[]);
    if(!bars.length){
      const detail=data.error||`MT5 returned no verified ${tf} candles for ${symbol}. Open the symbol/timeframe in MT5 and check the terminal's Max bars in chart setting.`;
      throw new Error(detail);
    }
    const fingerprint=chartDataFingerprint(bars);
    const changed=ChartState.fetchedSymbol!==symbol || ChartState.fetchedTf!==tf || ChartState._lastDataFingerprint!==fingerprint;
    ChartState.candles=bars;
    ChartState.fetchedTf=tf;
    ChartState.fetchedSymbol=symbol;
    ChartState._historyError='';
    ChartState._historyFailures=0;
    ChartState._historyRetryAt=0;
    ChartState._historyRetrySymbol='';
    ChartState._historyRetryTf='';
    ChartState._lastDataFingerprint=fingerprint;
    if(changed) drawCandles();
  }catch(e){
    if(seq!==ChartState._historySeq || ChartState.symbol!==symbol || ChartState.tf!==tf) return;
    ChartState._historyError=String(e?.message||e||'MT5 history unavailable');
    const sameFailureKey=ChartState._historyRetrySymbol===symbol && ChartState._historyRetryTf===tf;
    ChartState._historyFailures=sameFailureKey ? ChartState._historyFailures+1 : 1;
    ChartState._historyRetrySymbol=symbol;
    ChartState._historyRetryTf=tf;
    ChartState._historyRetryAt=Date.now()+Math.min(30000,1500*Math.pow(2,Math.min(5,ChartState._historyFailures-1)));
    // Never mark an unsuccessful fetch as a successful symbol/timeframe result. This lets
    // the next scheduled retry recover when MT5 finishes synchronizing history.
    if(!hadMatchingBars){
      ChartState.candles=[];
      ChartState.fetchedSymbol=null;
      ChartState.fetchedTf=null;
      ChartState._lastDataFingerprint='';
      drawCandles();
    }
    if(notify) toast(`MT5 ${symbol} ${tf} history unavailable: ${ChartState._historyError}`);
  }finally{
    if(ChartState._historyInFlightKey===requestKey) ChartState._historyInFlightKey='';
  }
}

async function setChartTf(tf){
  const allowed=['M1','M2','M3','M4','M5','M6','M10','M12','M15','M20','M30','H1','H2','H3','H4','H6','H8','H12','D1','W1','MN1'];
  if(!allowed.includes(String(tf).toUpperCase())) return;
  tf=String(tf).toUpperCase();
  ChartState.scaleLo=null; ChartState.scaleHi=null; ChartState.tf=tf; ChartState._historyError='';
  document.querySelectorAll('.chart-tf-btn').forEach(b=>b.classList.toggle('active',b.dataset.tf===tf));
  const symbol=ChartState.symbol || $('chartSymbolInput')?.value || $('setSymbol')?.value || 'XAUUSD';
  if(!ChartState.symbol) ChartState.symbol=symbol;
  // Pull requested timeframe directly from MT5, including M5/M15/H1, so the chart does not
  // accidentally display the currently active system-symbol snapshot for another selection.
  await loadChartHistory(symbol,tf,{force:true,silent:false,notify:true});
}

/* ── Chart symbol autocomplete ──────────────────────────────── */
let _chartSugIndex = -1;
let _chartSugItems = [];

let _symbolCatalogRequest = null;
async function ensureSymbolCatalog(force=false){
  if(window.__symbolCatalogCache && !force) return window.__symbolCatalogCache;
  if(_symbolCatalogRequest && !force) return _symbolCatalogRequest;
  _symbolCatalogRequest=(async()=>{
    const data=await get('/api/symbol-catalog?page=1&page_size=10000',{silent:true,force,retries:2});
    window.__symbolCatalogCache=Array.isArray(data.symbols)?data.symbols:(Array.isArray(data.items)?data.items:[]);
    window.__symbolCatalogMeta={total:Number(data.total||window.__symbolCatalogCache.length),pages:Number(data.pages||1),loadedAt:Date.now()};
    return window.__symbolCatalogCache;
  })();
  try{return await _symbolCatalogRequest;}finally{_symbolCatalogRequest=null;}
}

function _symbolCatalogList(){
  const s = window.__lastState || {};
  const catalog = window.__symbolCatalogCache || s.symbol_catalog || [];
  const wl = (s.settings && s.settings.symbols) || s.watchlist_symbols || [];
  const broker = (s.broker_symbols || s.symbols_broker || []);
  const seen = new Set();
  const out = [];
  const push = (sym, name, meta) => {
    const key = String(sym || '').trim().toUpperCase();
    if(!key || seen.has(key)) return;
    seen.add(key);
    out.push({ symbol: key, name: name || key, meta: meta || '' });
  };
  (Array.isArray(wl) ? wl : []).forEach(x => push(typeof x === 'string' ? x : x?.symbol, null, 'watchlist'));
  catalog.forEach(c => push(c.symbol, c.name, c.asset_class || ''));
  (Array.isArray(broker) ? broker : []).forEach(x => push(typeof x === 'string' ? x : x?.name || x?.symbol, null, 'broker'));
  ;['XAUUSD','EURUSD','GBPUSD','USDJPY','BTCUSD','ETHUSD','US30','NAS100','US500','USOIL','XAGUSD'].forEach(x => push(x, null, ''));
  return out;
}

function onChartSymbolInput(val){
  if(!window.__symbolCatalogCache) ensureSymbolCatalog().catch(()=>{});
  const box = $('chartSymbolSuggest');
  if(!box) return;
  const q = String(val || '').trim().toUpperCase();
  _chartSugIndex = -1;
  if(!q){
    _chartSugItems = _symbolCatalogList().slice(0, 12);
  } else {
    const all = _symbolCatalogList();
    _chartSugItems = all.filter(it =>
      it.symbol.includes(q) || (it.name && String(it.name).toUpperCase().includes(q))
    ).slice(0, 14);
  }
  if(!_chartSugItems.length){
    box.innerHTML = `<div class="sug-empty">No match — press Enter to try “${esc(q)}”</div>`;
    box.hidden = false;
    return;
  }
  box.innerHTML = _chartSugItems.map((it, i) =>
    `<div class="sug-item" data-idx="${i}" onmousedown="event.preventDefault(); pickChartSymbol('${esc(it.symbol)}')">` +
    `<span class="sug-name">${esc(it.symbol)}</span>` +
    `<span class="sug-meta">${esc(it.meta || it.name || '')}</span></div>`
  ).join('');
  box.hidden = false;
}

function onChartSymbolKey(e){
  const box = $('chartSymbolSuggest');
  if(!box || box.hidden){
    if(e.key === 'Enter'){
      e.preventDefault();
      const raw = ($('chartSymbolInput')?.value || '').trim().toUpperCase();
      if(raw) pickChartSymbol(raw);
    }
    return;
  }
  if(e.key === 'ArrowDown'){
    e.preventDefault();
    _chartSugIndex = Math.min(_chartSugIndex + 1, _chartSugItems.length - 1);
    _highlightChartSug();
  } else if(e.key === 'ArrowUp'){
    e.preventDefault();
    _chartSugIndex = Math.max(_chartSugIndex - 1, 0);
    _highlightChartSug();
  } else if(e.key === 'Enter'){
    e.preventDefault();
    if(_chartSugIndex >= 0 && _chartSugItems[_chartSugIndex]){
      pickChartSymbol(_chartSugItems[_chartSugIndex].symbol);
    } else {
      const raw = ($('chartSymbolInput')?.value || '').trim().toUpperCase();
      if(raw) pickChartSymbol(raw);
    }
  } else if(e.key === 'Escape'){
    box.hidden = true;
    _chartSugIndex = -1;
  }
}

function _highlightChartSug(){
  const box = $('chartSymbolSuggest');
  if(!box) return;
  box.querySelectorAll('.sug-item').forEach((el, i) => {
    el.classList.toggle('active', i === _chartSugIndex);
  });
  const active = box.querySelector('.sug-item.active');
  if(active) active.scrollIntoView({ block: 'nearest' });
}

function pickChartSymbol(sym){
  const key = String(sym || '').trim().toUpperCase();
  if(!key) return;
  const input = $('chartSymbolInput');
  if(input) input.value = key;
  const box = $('chartSymbolSuggest');
  if(box) box.hidden = true;
  _chartSugIndex = -1;
  switchSymbol(key);
}

document.addEventListener('click', (e) => {
  const picker = document.querySelector('.chart-symbol-picker');
  const box = $('chartSymbolSuggest');
  if(!box || box.hidden) return;
  if(picker && picker.contains(e.target)) return;
  box.hidden = true;
});

function toggleIndicator(key){
  // Map HTML button keys to internal indicator flags
  const aliasMap = {
    sma: ['sma200', 'sma'],
    ema: ['ema20', 'ema50', 'ema'],
    bb: ['bb'],
    vwap: ['vwap'],
    rsi: ['rsi'],
    macd: ['macd'],
    atr: ['atr'],
    ema20: ['ema20'],
    ema50: ['ema50'],
    sma200: ['sma200'],
  };
  const keys = aliasMap[key] || (key in ChartState.indicators ? [key] : null);
  if(!keys) return;
  const next = !ChartState.indicators[keys[0]];
  keys.forEach(k => { ChartState.indicators[k] = next; });
  document.querySelectorAll('.chart-ind-btn').forEach(b=>{
    if(b.dataset.ind === key) b.classList.toggle('active', next);
  });
  drawCandles();
}

/* ── Indicator math (client-side on OHLC) ──────────────────────── */
function _closes(bars){ return bars.map(b => b.c); }
function _hlc3(bars){ return bars.map(b => (b.h + b.l + b.c) / 3); }

function smaSeries(values, period){
  const out = new Array(values.length).fill(null);
  if(period < 1 || values.length < period) return out;
  let sum = 0;
  for(let i = 0; i < values.length; i++){
    sum += values[i];
    if(i >= period) sum -= values[i - period];
    if(i >= period - 1) out[i] = sum / period;
  }
  return out;
}

function emaSeries(values, period){
  const out = new Array(values.length).fill(null);
  if(period < 1 || values.length < period) return out;
  const k = 2 / (period + 1);
  let prev = 0;
  for(let i = 0; i < period; i++) prev += values[i];
  prev /= period;
  out[period - 1] = prev;
  for(let i = period; i < values.length; i++){
    prev = values[i] * k + prev * (1 - k);
    out[i] = prev;
  }
  return out;
}

function bollinger(values, period, mult){
  mult = mult || 2;
  const mid = smaSeries(values, period);
  const upper = new Array(values.length).fill(null);
  const lower = new Array(values.length).fill(null);
  for(let i = period - 1; i < values.length; i++){
    let s = 0;
    for(let j = i - period + 1; j <= i; j++){
      const d = values[j] - mid[i];
      s += d * d;
    }
    const sd = Math.sqrt(s / period);
    upper[i] = mid[i] + mult * sd;
    lower[i] = mid[i] - mult * sd;
  }
  return { mid, upper, lower };
}

function rsiSeries(values, period){
  period = period || 14;
  const out = new Array(values.length).fill(null);
  if(values.length < period + 1) return out;
  let avgGain = 0, avgLoss = 0;
  for(let i = 1; i <= period; i++){
    const ch = values[i] - values[i - 1];
    if(ch >= 0) avgGain += ch; else avgLoss -= ch;
  }
  avgGain /= period; avgLoss /= period;
  out[period] = avgLoss === 0 ? 100 : 100 - (100 / (1 + avgGain / avgLoss));
  for(let i = period + 1; i < values.length; i++){
    const ch = values[i] - values[i - 1];
    const g = ch > 0 ? ch : 0;
    const l = ch < 0 ? -ch : 0;
    avgGain = (avgGain * (period - 1) + g) / period;
    avgLoss = (avgLoss * (period - 1) + l) / period;
    out[i] = avgLoss === 0 ? 100 : 100 - (100 / (1 + avgGain / avgLoss));
  }
  return out;
}

function macdSeries(values, fast, slow, signal){
  fast = fast || 12; slow = slow || 26; signal = signal || 9;
  const emaF = emaSeries(values, fast);
  const emaS = emaSeries(values, slow);
  const macd = values.map((_, i) => (emaF[i] != null && emaS[i] != null) ? emaF[i] - emaS[i] : null);
  // signal on non-null macd — approximate by treating nulls as skip
  const macdFill = macd.map(v => v == null ? 0 : v);
  const sig = emaSeries(macdFill, signal);
  // zero out early signal where macd was null
  for(let i = 0; i < sig.length; i++){
    if(macd[i] == null) sig[i] = null;
  }
  const hist = macd.map((v, i) => (v != null && sig[i] != null) ? v - sig[i] : null);
  return { macd, signal: sig, hist };
}

function atrSeries(bars, period){
  period = period || 14;
  const tr = new Array(bars.length).fill(0);
  for(let i = 0; i < bars.length; i++){
    if(i === 0){ tr[i] = bars[i].h - bars[i].l; continue; }
    const hl = bars[i].h - bars[i].l;
    const hc = Math.abs(bars[i].h - bars[i - 1].c);
    const lc = Math.abs(bars[i].l - bars[i - 1].c);
    tr[i] = Math.max(hl, hc, lc);
  }
  return smaSeries(tr, period);
}

function vwapSeries(bars){
  const out = new Array(bars.length).fill(null);
  let cumPV = 0, cumV = 0;
  for(let i = 0; i < bars.length; i++){
    const tp = (bars[i].h + bars[i].l + bars[i].c) / 3;
    const v = bars[i].v || 0;
    cumPV += tp * v;
    cumV += v;
    out[i] = cumV > 0 ? cumPV / cumV : tp;
  }
  return out;
}


function normalizeCandle(b){
  if(!b || typeof b !== 'object') return null;
  const o = Number(b.o ?? b.open);
  const h = Number(b.h ?? b.high);
  const l = Number(b.l ?? b.low);
  const c = Number(b.c ?? b.close);
  if(![o,h,l,c].every(Number.isFinite)) return null;
  const hi = Math.max(h, o, c);
  const lo = Math.min(l, o, c);
  return {
    t: b.t || b.time || b.timestamp || null,
    o, h: hi, l: lo, c,
    v: Number(b.v ?? b.volume ?? 0) || 0,
  };
}
function normalizeSeries(series){
  if(!Array.isArray(series)) return [];
  return series.map(normalizeCandle).filter(Boolean);
}

function draw(history, state){
  if(state?.system){
    ChartState.marketOpen = state.system.market_open === true;
    ChartState.mt5Connected = state.system.mt5_connected === true;
    ChartState.demo = state.system.demo_mode === true;
    ChartState.sessionReason = state.system.session?.reason || state.system.connection_detail || '';
  }
  const market=state?.market||{};
  const systemSymbol=String(market.symbol||'XAUUSD').toUpperCase();
  const prevSym=ChartState.symbol, prevTf=ChartState.tf;
  // A deliberate chart selection takes precedence over the system snapshot. Previously each
  // dashboard poll overwrote the selected symbol with state.market.symbol.
  const nextSym=ChartState.userSelectedSymbol && ChartState.symbol ? ChartState.symbol : systemSymbol;
  if(!ChartState.userSelectedSymbol) ChartState.symbol=nextSym;
  if(prevSym!==ChartState.symbol || prevTf!==ChartState.tf){ChartState.scaleLo=null;ChartState.scaleHi=null;}
  ChartState.plan=state?.decision?.plan||null;
  ChartState.spread=market.spread;
  const chartInp=$('chartSymbolInput');
  if(chartInp && document.activeElement!==chartInp) chartInp.value=ChartState.symbol;
  let series=[];
  if(ChartState.fetchedSymbol===ChartState.symbol && ChartState.fetchedTf===ChartState.tf){
    series=ChartState.candles||[];
    // A previous request may have failed while stale matching bars were retained. Retry
    // after bounded backoff rather than treating the failure as a permanently fetched result.
    if(ChartState._historyError && activePageId()==='dashboard' && Date.now()>=ChartState._historyRetryAt){
      loadChartHistory(ChartState.symbol,ChartState.tf,{silent:true,notify:false});
    }
  }else if(!ChartState.userSelectedSymbol && systemSymbol===ChartState.symbol){
    const snapshotSeries=normalizeSeries(market.candles?.[ChartState.tf]||[]);
    if(snapshotSeries.length){
      series=snapshotSeries;
      ChartState.candles=series;
    }else{
      // Runtime snapshots usually contain only M5/M15/H1. Other timeframes must be
      // requested explicitly instead of silently leaving the chart empty.
      series=[];
      if(activePageId()==='dashboard') loadChartHistory(ChartState.symbol,ChartState.tf,{silent:true,notify:false});
    }
  }else{
    // Do not draw old candles while the requested symbol/timeframe history is loading.
    series=[];
    if(activePageId()==='dashboard') loadChartHistory(ChartState.symbol,ChartState.tf,{silent:true,notify:false});
  }
  const fingerprint=chartDataFingerprint(series);
  const dataChanged=!!window.__forceChartRedraw || (ChartState._lastDrawnSym!==ChartState.symbol) || (ChartState._lastDrawnTf!==ChartState.tf) || (ChartState._lastDrawnFingerprint!==fingerprint);
  if(series.length){
    window.__forceChartRedraw=false;
    ChartState._lastDrawnSym=ChartState.symbol;
    ChartState._lastDrawnTf=ChartState.tf;
    ChartState._lastDrawnFingerprint=fingerprint;
    if(dataChanged && typeof chartFade==='function') chartFade(()=>drawCandles());
  }else if(dataChanged){
    window.__forceChartRedraw=false;
    ChartState._lastDrawnSym=ChartState.symbol;
    ChartState._lastDrawnTf=ChartState.tf;
    ChartState._lastDrawnFingerprint=fingerprint;
    drawCandles();
  }
  return;
}

function _strokeSeries(ctx, series, viewOffset, xBar, yMap, color, lineWidth){
  ctx.strokeStyle = color;
  ctx.lineWidth = lineWidth || 1.4;
  ctx.beginPath();
  let started = false;
  for(let i = 0; i < series.length; i++){
    const v = series[i];
    if(v == null || isNaN(v)){ started = false; continue; }
    const x = xBar(i);
    const y = yMap(v);
    if(!started){ ctx.moveTo(x, y); started = true; }
    else ctx.lineTo(x, y);
  }
  ctx.stroke();
}


function drawSessionOverlay(ctx, W, H){
  const closed = ChartState.marketOpen === false;
  const offline = ChartState.mt5Connected === false;
  if(!closed && !offline) return;
  const label = offline ? 'MT5 OFFLINE' : 'MARKET CLOSED';
  const sub = offline
    ? (ChartState.sessionReason || 'Connect MetaTrader 5 — no synthetic data is used')
    : (ChartState.sessionReason || 'Session closed — last MT5 historical bars shown');
  ctx.fillStyle = offline ? 'rgba(120, 50, 20, 0.90)' : 'rgba(180, 40, 40, 0.85)';
  ctx.fillRect(0, 0, W, 22);
  ctx.fillStyle = '#fff';
  ctx.font = 'bold 11px Inter, system-ui, sans-serif';
  ctx.fillText(label, 12, 15);
  ctx.font = '10px Inter, system-ui, sans-serif';
  ctx.fillStyle = 'rgba(255,255,255,0.85)';
  ctx.fillText(sub, 120, 15);
}


function chartFade(redrawFn){
  const wrap = document.querySelector('.chart-wrap');
  if(!wrap){ if(typeof redrawFn === 'function') redrawFn(); return; }
  wrap.classList.add('is-fading');
  wrap.classList.remove('is-ready');
  requestAnimationFrame(() => {
    setTimeout(() => {
      try { if(typeof redrawFn === 'function') redrawFn(); } catch(e){ console.error(e); }
      wrap.classList.remove('is-fading');
      wrap.classList.add('is-ready');
    }, 160);
  });
}

function drawCandles(){
  const canvas = $('candleCanvas') || $('priceChart');
  if(!canvas) return;
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;

  // Use CSS size so candles don't stretch
  const rect = canvas.getBoundingClientRect();
  const W = Math.max(320, Math.floor(rect.width || canvas.clientWidth || 640));
  const H = Math.max(360, Math.floor(rect.height || canvas.clientHeight || 420));
  if(canvas.width !== W * dpr || canvas.height !== H * dpr){
    canvas.width = W * dpr;
    canvas.height = H * dpr;
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);

  const allCandles = ChartState.candles || [];
  if(!allCandles.length){
    ctx.fillStyle = '#6b7c8f';
    ctx.font = '13px Inter, system-ui, sans-serif';
    const suffix=ChartState._historyError ? ` · ${ChartState._historyError}` : '';
    ctx.fillText(`No verified candles for ${ChartState.symbol||'—'} ${ChartState.tf||'M5'} — waiting for MT5 history${suffix}`.slice(0,180), 18, 32);
    return;
  }

  const ind = ChartState.indicators || {};
  const showRsi = !!ind.rsi;
  const showMacd = !!ind.macd;

  // Layout (main pane gets most height)
  const padL = 10, padR = 76, padT = 26, padB = 16;
  const rsiH = showRsi ? Math.floor(H * 0.13) : 0;
  const macdH = showMacd ? Math.floor(H * 0.13) : 0;
  const volH = Math.floor(H * 0.11);
  const gaps = 4 + (showRsi ? 4 : 0) + (showMacd ? 4 : 0);
  const mainH = Math.max(120, H - padT - padB - volH - rsiH - macdH - gaps);
  const mainTop = padT;
  const volTop = mainTop + mainH + 4;
  const rsiTop = volTop + volH + (showRsi ? 4 : 0);
  const macdTop = rsiTop + rsiH + (showMacd ? 4 : 0);
  const chartW = W - padL - padR;

  // Visible bars — fixed density like MT5
  const pxPerBar = 8;
  const maxBars = Math.min(allCandles.length, Math.max(40, Math.min(ChartState.fixedBars || 100, Math.floor(chartW / pxPerBar))));
  const warmup = 200;
  const computeSlice = allCandles.slice(Math.max(0, allCandles.length - maxBars - warmup));
  const viewStart = Math.max(0, computeSlice.length - maxBars);
  const view = computeSlice.slice(viewStart);
  const n = view.length;
  if(!n) return;

  // Indicators on full compute slice
  const closesAll = computeSlice.map(b => b.c);
  const ema20a = ind.ema20 ? emaSeries(closesAll, 20) : null;
  const ema50a = ind.ema50 ? emaSeries(closesAll, 50) : null;
  const sma200a = ind.sma200 ? smaSeries(closesAll, 200) : null;
  const bba = ind.bb ? bollinger(closesAll, 20, 2) : null;
  const vwapa = ind.vwap ? vwapSeries(computeSlice) : null;
  const rsia = showRsi ? rsiSeries(closesAll, 14) : null;
  const macda = showMacd ? macdSeries(closesAll, 12, 26, 9) : null;

  const cut = (arr) => arr ? arr.slice(viewStart) : null;
  const ema20 = cut(ema20a), ema50 = cut(ema50a), sma200 = cut(sma200a);
  const bb = bba ? { mid: cut(bba.mid), upper: cut(bba.upper), lower: cut(bba.lower) } : null;
  const vwap = cut(vwapa), rsi = cut(rsia);
  const macd = macda ? { macd: cut(macda.macd), signal: cut(macda.signal), hist: cut(macda.hist) } : null;

  // Y scale from OHLC of visible candles ONLY
  let lo = Infinity, hi = -Infinity, maxVol = 0;
  for(const b of view){
    if(b.l < lo) lo = b.l;
    if(b.h > hi) hi = b.h;
    if(b.v > maxVol) maxVol = b.v;
  }
  if(!Number.isFinite(lo) || !Number.isFinite(hi)){
    lo = view[0].c; hi = view[0].c;
  }
  if(hi <= lo){
    const pad0 = Math.max(Math.abs(lo) * 0.001, 0.5);
    lo -= pad0; hi += pad0;
  }
  // 6% padding
  const padY = (hi - lo) * 0.06;
  lo -= padY; hi += padY;

  // Smooth scale
  if(ChartState.scaleLo != null && ChartState.scaleHi != null){
    const a = 0.4;
    lo = ChartState.scaleLo * (1 - a) + lo * a;
    hi = ChartState.scaleHi * (1 - a) + hi * a;
  }
  ChartState.scaleLo = lo;
  ChartState.scaleHi = hi;
  const range = Math.max(hi - lo, 1e-12);

  const barGap = chartW / n;
  const bodyW = Math.max(3, Math.min(10, barGap * 0.7));
  const yPrice = (p) => mainTop + mainH - ((p - lo) / range) * mainH;
  const xBar = (i) => padL + i * barGap + barGap / 2;

  // Background
  ctx.fillStyle = document.body.classList.contains('theme-light') ? '#e7edf5' : '#111a29';
  ctx.fillRect(0, 0, W, H);

  // Grid + price labels
  ctx.font = '11px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
  const gridLines = 8;
  for(let i = 0; i <= gridLines; i++){
    const p = lo + (range * i) / gridLines;
    const y = yPrice(p);
    ctx.strokeStyle = '#15202b';
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(W - padR + 2, y); ctx.stroke();
    ctx.fillStyle = '#7a8b9c';
    ctx.textAlign = 'left';
    const digits = Math.abs(p) >= 1000 ? 2 : (Math.abs(p) >= 10 ? 3 : 5);
    ctx.fillText(p.toFixed(digits), W - padR + 6, y + 3);
  }

  // Bollinger fill (behind candles)
  if(bb){
    ctx.beginPath();
    let started = false;
    for(let i = 0; i < n; i++){
      if(bb.upper[i] == null){ started = false; continue; }
      const x = xBar(i), y = yPrice(bb.upper[i]);
      if(!started){ ctx.moveTo(x, y); started = true; }
      else ctx.lineTo(x, y);
    }
    for(let i = n - 1; i >= 0; i--){
      if(bb.lower[i] == null) continue;
      ctx.lineTo(xBar(i), yPrice(bb.lower[i]));
    }
    ctx.closePath();
    ctx.fillStyle = 'rgba(98,169,255,0.07)';
    ctx.fill();
    if(typeof _strokeSeries === 'function'){
      _strokeSeries(ctx, bb.upper, 0, xBar, yPrice, 'rgba(98,169,255,0.4)', 1);
      _strokeSeries(ctx, bb.lower, 0, xBar, yPrice, 'rgba(98,169,255,0.4)', 1);
      _strokeSeries(ctx, bb.mid, 0, xBar, yPrice, 'rgba(98,169,255,0.65)', 1);
    }
  }

  // MAs behind candles
  if(ema20 && typeof _strokeSeries === 'function') _strokeSeries(ctx, ema20, 0, xBar, yPrice, '#f0b429', 1.4);
  if(ema50 && typeof _strokeSeries === 'function') _strokeSeries(ctx, ema50, 0, xBar, yPrice, '#e67e22', 1.4);
  if(sma200 && typeof _strokeSeries === 'function') _strokeSeries(ctx, sma200, 0, xBar, yPrice, '#9b59b6', 1.5);
  if(vwap && typeof _strokeSeries === 'function') _strokeSeries(ctx, vwap, 0, xBar, yPrice, '#1abc9c', 1.3);

  // Volume bars
  for(let i = 0; i < n; i++){
    const bar = view[i];
    const x = xBar(i);
    const bull = bar.c >= bar.o;
    const vh = maxVol > 0 ? (bar.v / maxVol) * (volH - 6) : 0;
    ctx.fillStyle = bull ? 'rgba(38,166,154,0.4)' : 'rgba(239,83,80,0.4)';
    ctx.fillRect(x - bodyW / 2, volTop + volH - vh, bodyW, Math.max(1, vh));
  }
  ctx.strokeStyle = '#1c2a38';
  ctx.beginPath(); ctx.moveTo(padL, volTop); ctx.lineTo(W - padR, volTop); ctx.stroke();
  ctx.fillStyle = '#5a6b7c'; ctx.font = '10px Inter, sans-serif'; ctx.textAlign = 'left';
  ctx.fillText('Vol', padL + 2, volTop + 11);

  // ═══ CANDLESTICKS ═══
  for(let i = 0; i < n; i++){
    const bar = view[i];
    const x = xBar(i);
    const yO = yPrice(bar.o);
    const yC = yPrice(bar.c);
    const yH = yPrice(bar.h);
    const yL = yPrice(bar.l);
    const bull = bar.c >= bar.o;
    const up = '#26a69a';
    const dn = '#ef5350';
    const color = bull ? up : dn;

    // Wick (high-low)
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.15;
    ctx.beginPath();
    ctx.moveTo(x, yH);
    ctx.lineTo(x, yL);
    ctx.stroke();

    // Body (open-close)
    const top = Math.min(yO, yC);
    const bodyH = Math.max(1.5, Math.abs(yC - yO));
    if(bull){
      // hollow-style optional: filled for clarity
      ctx.fillStyle = up;
      ctx.fillRect(x - bodyW / 2, top, bodyW, bodyH);
    } else {
      ctx.fillStyle = dn;
      ctx.fillRect(x - bodyW / 2, top, bodyW, bodyH);
    }
  }

  // Trade levels (do not affect scale)
  const plan = ChartState.plan;
  function drawLevel(price, color, label){
    price = Number(price);
    if(!Number.isFinite(price)) return;
    let y, clipped = false;
    if(price > hi){ y = mainTop + 3; clipped = true; }
    else if(price < lo){ y = mainTop + mainH - 3; clipped = true; }
    else y = yPrice(price);
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.1;
    ctx.setLineDash(clipped ? [2, 3] : [5, 4]);
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(W - padR, y); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = color;
    ctx.font = '10px ui-monospace, monospace';
    ctx.textAlign = 'left';
    ctx.fillText(label + ' ' + price.toFixed(2) + (clipped ? (price > hi ? ' ▲' : ' ▼') : ''), W - padR + 5, y - 2);
  }
  if(plan){
    drawLevel(plan.entry, '#62a9ff', 'ENTRY');
    drawLevel(plan.stop, '#ef5350', 'SL');
    drawLevel(plan.target, '#26a69a', 'TP');
  }

  // Last price line
  const last = view[n - 1];
  const lastY = yPrice(last.c);
  const lastColor = last.c >= last.o ? '#26a69a' : '#ef5350';
  ctx.strokeStyle = lastColor;
  ctx.lineWidth = 1;
  ctx.setLineDash([3, 3]);
  ctx.beginPath(); ctx.moveTo(padL, lastY); ctx.lineTo(W - padR, lastY); ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = lastColor;
  ctx.fillRect(W - padR + 3, lastY - 9, padR - 6, 18);
  ctx.fillStyle = document.body.classList.contains('theme-light') ? '#e7edf5' : '#111a29';
  ctx.font = 'bold 11px ui-monospace, monospace';
  ctx.textAlign = 'center';
  const ld = Math.abs(last.c) >= 1000 ? 2 : 3;
  ctx.fillText(last.c.toFixed(ld), W - padR / 2 + 1, lastY + 4);

  // OHLC header
  ctx.fillStyle = '#9aafc0';
  ctx.font = '11px ui-monospace, monospace';
  ctx.textAlign = 'left';
  const hdr = (ChartState.symbol || '') + '  ' + (ChartState.tf || 'M5') +
    '  O ' + last.o.toFixed(ld) +
    '  H ' + last.h.toFixed(ld) +
    '  L ' + last.l.toFixed(ld) +
    '  C ' + last.c.toFixed(ld) +
    '  V ' + Math.round(last.v);
  ctx.fillText(hdr, padL + 2, 16);

  // RSI
  if(showRsi && rsi){
    const yRsi = (v) => rsiTop + rsiH - (Math.max(0, Math.min(100, v)) / 100) * rsiH;
    ctx.strokeStyle = '#1c2a38';
    ctx.beginPath(); ctx.moveTo(padL, rsiTop); ctx.lineTo(W - padR, rsiTop); ctx.stroke();
    ctx.fillStyle = 'rgba(239,83,80,0.05)';
    ctx.fillRect(padL, yRsi(100), chartW, yRsi(70) - yRsi(100));
    ctx.fillStyle = 'rgba(38,166,154,0.05)';
    ctx.fillRect(padL, yRsi(30), chartW, yRsi(0) - yRsi(30));
    ctx.setLineDash([3, 3]); ctx.strokeStyle = '#2a3a4a';
    [30, 50, 70].forEach(lv => {
      const y = yRsi(lv);
      ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(W - padR, y); ctx.stroke();
    });
    ctx.setLineDash([]);
    if(typeof _strokeSeries === 'function') _strokeSeries(ctx, rsi, 0, xBar, yRsi, '#b388ff', 1.4);
    const lastRsi = [...rsi].reverse().find(v => v != null);
    ctx.fillStyle = '#5a6b7c'; ctx.font = '10px Inter, sans-serif'; ctx.textAlign = 'left';
    ctx.fillText('RSI(14)' + (lastRsi != null ? '  ' + lastRsi.toFixed(1) : ''), padL + 2, rsiTop + 11);
  }

  // MACD
  if(showMacd && macd){
    ctx.strokeStyle = '#1c2a38';
    ctx.beginPath(); ctx.moveTo(padL, macdTop); ctx.lineTo(W - padR, macdTop); ctx.stroke();
    let mLo = Infinity, mHi = -Infinity;
    for(let i = 0; i < n; i++){
      for(const arr of [macd.macd, macd.signal, macd.hist]){
        const v = arr[i];
        if(v != null && Number.isFinite(v)){ mLo = Math.min(mLo, v); mHi = Math.max(mHi, v); }
      }
    }
    if(!Number.isFinite(mLo)){ mLo = -1; mHi = 1; }
    if(mHi === mLo){ mLo -= 1; mHi += 1; }
    const mPad = (mHi - mLo) * 0.1;
    mLo -= mPad; mHi += mPad;
    const mRange = mHi - mLo;
    const yMacd = (v) => macdTop + macdH - ((v - mLo) / mRange) * macdH;
    // hist
    for(let i = 0; i < n; i++){
      const v = macd.hist[i];
      if(v == null) continue;
      const y0 = yMacd(0), y1 = yMacd(v);
      ctx.fillStyle = v >= 0 ? 'rgba(38,166,154,0.55)' : 'rgba(239,83,80,0.55)';
      ctx.fillRect(xBar(i) - bodyW / 2, Math.min(y0, y1), bodyW, Math.max(1, Math.abs(y1 - y0)));
    }
    if(typeof _strokeSeries === 'function'){
      _strokeSeries(ctx, macd.macd, 0, xBar, yMacd, '#62a9ff', 1.3);
      _strokeSeries(ctx, macd.signal, 0, xBar, yMacd, '#f0b429', 1.2);
    }
    ctx.fillStyle = '#5a6b7c'; ctx.font = '10px Inter, sans-serif'; ctx.textAlign = 'left';
    ctx.fillText('MACD(12,26,9)', padL + 2, macdTop + 11);
  }

  // Legend
  const legend = [];
  if(ind.ema20) legend.push(['EMA20', '#f0b429']);
  if(ind.ema50) legend.push(['EMA50', '#e67e22']);
  if(ind.sma200) legend.push(['SMA200', '#9b59b6']);
  if(ind.bb) legend.push(['BB(20,2)', '#62a9ff']);
  if(ind.vwap) legend.push(['VWAP', '#1abc9c']);
  let lx = padL + 2, ly = mainTop + 14;
  ctx.font = '10px Inter, sans-serif';
  for(const [name, col] of legend){
    ctx.fillStyle = col;
    ctx.fillRect(lx, ly - 6, 10, 3);
    ctx.fillStyle = '#8a9bab';
    ctx.textAlign = 'left';
    ctx.fillText(name, lx + 14, ly);
    lx += ctx.measureText(name).width + 28;
  }

  if(typeof drawSessionOverlay === 'function') drawSessionOverlay(ctx, W, H);

  // Store for crosshair
  ChartState._layout = { padL, padR, padT, mainTop, mainH, volTop, volH, rsiTop, rsiH, macdTop, macdH, chartW, W, H, lo, hi, range, view, n, barGap, bodyW, yPrice, xBar };
}


(function bindChartInteraction(){
  function bind(){
    const c = $('candleCanvas') || $('priceChart');
    if(!c || c._chartBound) return;
    c._chartBound = true;
    c.addEventListener('mousemove', (e)=>{
      const r = c.getBoundingClientRect();
      ChartState.crosshair = { x: e.clientX - r.left, y: e.clientY - r.top };
      drawCandles();
    });
    c.addEventListener('mouseleave', ()=>{
      ChartState.crosshair = null;
      drawCandles();
    });
    // Native chart navigation: wheel zoom and shift/drag horizontal pan.
    c.addEventListener('wheel', (e)=>{
      if(!ChartState.candles?.length) return;
      e.preventDefault();
      const delta = e.deltaY < 0 ? -8 : 8;
      ChartState.fixedBars = Math.max(35, Math.min(260, (ChartState.fixedBars || 80) + delta));
      drawCandles();
    }, {passive:false});
    let dragX = null;
    c.addEventListener('pointerdown',(e)=>{ if(e.button!==0)return; dragX=e.clientX; c.setPointerCapture?.(e.pointerId); c.style.cursor='grabbing'; });
    c.addEventListener('pointerup',()=>{dragX=null;c.style.cursor='crosshair';});
    c.addEventListener('pointercancel',()=>{dragX=null;c.style.cursor='crosshair';});
    c.addEventListener('pointermove',(e)=>{
      if(dragX===null || !ChartState.candles?.length)return;
      const dx=e.clientX-dragX; dragX=e.clientX;
      if(Math.abs(dx)>1){ ChartState.fixedBars=Math.max(35,Math.min(260,(ChartState.fixedBars||80)+Math.round(-dx/8))); drawCandles(); }
    });
  }
  if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bind);
  else bind();
  let _rz;
  window.addEventListener('resize', ()=>{ clearTimeout(_rz); _rz=setTimeout(()=>{ if(ChartState.candles?.length) drawCandles(); }, 120); });
})();

function updateTerminalTape(s){
  const symbol=s.market?.symbol||s.settings?.symbol||'XAUUSD';
  const price=s.market?.price;
  const decision=s.ai_mesh?.decision||s.decision?.decision||'WAIT';
  const confidence=s.ai_mesh?.confidence??s.decision?.confidence;
  const set=(id,v)=>{const el=$(id);if(el)el.textContent=v;};
  set('tapeSymbol',symbol);set('tapePrice',typeof price==='number'?fmt(price):'—');set('tapeSpread',`SPREAD ${fmt(s.market?.spread)}`);
  // Dashboard hero metrics
  const acct = s.account || {};
  setTxt('dashEquity', fmt(acct.equity));
  setTxt('dashBalance', 'Balance ' + fmt(acct.balance));
  setTxt('dashOpenPnl', fmt(acct.profit ?? acct.floating_pnl));
  setTxt('dashMargin', acct.margin_level != null ? fmt(acct.margin_level, 1) + '%' : '—');
  setTxt('dashFreeMargin', 'Free ' + fmt(acct.free_margin));
  setTxt('dashCycle', String(s.system?.total_cycles ?? 0));
  setTxt('dashActivity', s.system?.activity || 'Waiting');
  setTxt('chartTitle', (symbol || '—') + '  ·  ' + (ChartState.tf || 'M5'));
  // Decision on dashboard
  const dec = s.decision || {};
  setTxt('dashDecision', dec.decision || 'WAIT');
  const db = $('dashDecisionBadge');
  if(db){
    const d = (dec.decision || 'WAIT').toUpperCase();
    db.textContent = d;
    db.className = 'status-pill ' + (d === 'BUY' ? 'good' : d === 'SELL' ? 'bad' : 'neutral');
  }
  if($('dashReasons')){
    const reasons = dec.reasons || [];
    $('dashReasons').innerHTML = reasons.length
      ? reasons.map(r => `<div class="log">${esc(r)}</div>`).join('')
      : '<div class="log muted">No decision reasons this cycle.</div>';
  }
  // Topbar status
  const ts = $('topbarStatus');
  if(ts){
    const st = s.system?.status || 'standby';
    ts.textContent = String(st).toUpperCase();
    ts.className = 'status-pill ' + (st === 'running' || st === 'ok' ? 'good' : st === 'error' ? 'bad' : 'neutral');
  }

  set('tapeDecision',decision);set('tapeConfidence',`DECISION SCORE ${pct(confidence)} · UNCALIBRATED`);set('tapeRisk',s.ai_mesh?.risk_gate?.approved?'GATE PASSED':'GATE ACTIVE');
  set('ribbonMode',s.system?.trading_mode||s.settings?.mode||'PAPER');
  const d=$('tapeDecision');if(d)d.className=decision==='BUY'?'tape-positive':decision==='SELL'?'tape-negative':'tape-neutral';
}

function renderMTF(s){
  updateTerminalTape(s);
  const prices=s.market?.prices||{};
  const connected=s.system?.mt5_connected===true;
  const marketOpen=s.market?.market_open===true && s.system?.market_open===true;
  const fresh=s.market?.quote_fresh;
  const age=s.market?.tick_age_seconds;
  const session=s.system?.session||s.market?.session||{};
  const sessionSource=String(session.source||'unknown').toLowerCase();
  const explicitlyClosed=['mt5_trade_mode','tick_age','bar_age','weekend_heuristic'].includes(sessionSource);
  const offsetCandidate=s.market?.tick_timestamp_offset_candidate_seconds;
  const clockDiagnostic=String(s.market?.tick_clock_diagnostic||'');
  let quoteLabel='HISTORICAL / LAST KNOWN';
  const clockStatus=String(s.market?.timestamp_integrity_status||'');
  const clockIntegrityFailed=['timestamp_offset_configuration_conflict','tick_time_fields_disagree','tick_and_bar_timestamps_in_future','tick_timestamp_in_future','bar_timestamp_in_future'].includes(clockStatus);
  if(!connected) quoteLabel='BROKER CONNECTION UNAVAILABLE';
  else if(clockIntegrityFailed) quoteLabel=`SOURCE CLOCK / TIMESTAMP CHECK REQUIRED${offsetCandidate?` · POSSIBLE ADJUSTMENT ${Math.round(Number(offsetCandidate))}s`:''}`;
  else if(fresh===false) quoteLabel=(s.market?.quote_status==='tick_timestamp_in_future'?`QUOTE TIME NEEDS CHECKING${offsetCandidate?` · POSSIBLE ADJUSTMENT ${Math.round(Number(offsetCandidate))}s`:''}`:`QUOTE IS STALE OR INVALID${age!=null && Number(age)>=0?` · ${Math.round(Number(age))}s OLD`:''}`);
  else if(!marketOpen) quoteLabel=explicitlyClosed?'MARKET CLOSED · LAST KNOWN PRICE':'MARKET SESSION NOT CONFIRMED · LAST KNOWN PRICE';
  else if(fresh===true && s.market?.realtime===true) quoteLabel='LIVE QUOTE';
  else quoteLabel='QUOTE TIME NOT YET VERIFIED';
  $('mtfGrid').innerHTML=['M5','M15','H1'].map(tf=>`<div class="tf-card"><span>${tf}</span><b>${fmt(prices[tf])}</b><small title="${esc(clockDiagnostic)}">${esc(quoteLabel)}</small></div>`).join('');
}
function renderStructure(s){
  const ote=s.ote_zone;
  const map=[['TREND',s.trend],['BOS',s.bos],['CHOCH',s.choch],['MSS',s.mss],['PREMIUM / DISCOUNT',s.premium?'PREMIUM':s.discount?'DISCOUNT':'EQUILIBRIUM'],['PRICE IN OTE',s.price_in_ote?'YES':'NO'],['SWING HIGH',fmt(s.swing_high)],['SWING LOW',fmt(s.swing_low)],['LIQUIDITY HIGH',fmt(s.liquidity_high)],['LIQUIDITY LOW',fmt(s.liquidity_low)],['FVG COUNT',String(s.fvgs?.length||0)],['BREAKER COUNT',String(s.breaker_blocks?.length||0)]];
  $('structureGrid').innerHTML=map.map(x=>`<div class="structure-stat"><span>${esc(x[0])}</span><b>${esc(x[1]||'—')}</b></div>`).join('');
  $('fvgList').innerHTML=(s.fvgs||[]).slice().reverse().map(f=>`<div class="zone"><b>${esc(f.side)}</b> <span>${fmt(f.low)} — ${fmt(f.high)}${f.mitigated?' · MITIGATED':''}</span></div>`).join('')||'<div class="zone">No FVG detected.</div>';
  $('liquidityList').innerHTML=`<div class="zone"><b>RANGE HIGH</b> <span>${fmt(s.liquidity_high)}</span></div><div class="zone"><b>RANGE LOW</b> <span>${fmt(s.liquidity_low)}</span></div>${(s.equal_highs||[]).map(v=>`<div class="zone"><b>EQUAL HIGH</b> <span>${fmt(v)}</span></div>`).join('')}${(s.equal_lows||[]).map(v=>`<div class="zone"><b>EQUAL LOW</b> <span>${fmt(v)}</span></div>`).join('')}${(s.liquidity_sweeps||[]).map(sw=>`<div class="zone"><b>CONFIRMED ${esc(sw.liquidity_side)} SWEEP</b> <span>${esc(sw.side)} · level ${fmt(sw.level)} · wick ${fmt(sw.extreme)} · bar ${esc(String(sw.index))}</span></div>`).join('')}`;
  const hypotheses=[];
  if(ote)hypotheses.push(`<div class="zone"><b>${esc(ote.side)} OTE 62–79%</b> <span>${fmt(ote.low)} — ${fmt(ote.high)} · ${s.price_in_ote?'PRICE IN ZONE':'WAIT FOR PRICE'}</span></div>`);
  (s.breaker_blocks||[]).forEach(z=>hypotheses.push(`<div class="zone"><b>${esc(z.kind||'BREAKER')}</b> <span>${esc(z.side)} ${fmt(z.low)} — ${fmt(z.high)}</span></div>`));
  (s.mitigation_blocks||[]).forEach(z=>hypotheses.push(`<div class="zone"><b>MITIGATION CANDIDATE</b> <span>${esc(z.side)} ${fmt(z.low)} — ${fmt(z.high)}</span></div>`));
  (s.balanced_price_ranges||[]).forEach(z=>hypotheses.push(`<div class="zone"><b>BPR CANDIDATE</b> <span>${fmt(z.low)} — ${fmt(z.high)}</span></div>`));
  if(Number(s.inducement_level||0)>0)hypotheses.push(`<div class="zone"><b>POTENTIAL INDUCEMENT SWING</b> <span>${esc(s.inducement_side)} ${fmt(s.inducement_level)} · hypothesis only</span></div>`);
  (s.draw_on_liquidity_targets||[]).forEach((v,i)=>hypotheses.push(`<div class="zone"><b>DRAW TARGET ${i+1}</b> <span>${esc(s.draw_on_liquidity_side)} · ${fmt(v)}</span></div>`));
  $('structureHypotheses').innerHTML=hypotheses.join('')||'<div class="zone">No additional ICT/SMC hypotheses are currently confirmed by the available candles.</div>';
}
function renderLogs(s){
  const events = Array.isArray(s.events) ? s.events : [];
  const errors = Array.isArray(s.errors) ? s.errors : [];
  const eventHtml = events.length
    ? events.slice(0, 100).map(e => {
        const t = e.time || e.ts || e.timestamp || '';
        let when = '';
        try { when = t ? new Date(t).toLocaleString() : ''; } catch(_){ when = String(t); }
        const payload = e.payload || e.data || {};
        const summary = payload.message || payload.action || payload.decision || payload.status || payload.reason || payload.detail || '';
        const extra = summary ? esc(String(summary)) : esc(JSON.stringify(payload).slice(0, 400));
        return `<div class="log"><span class="time">${esc(when)}</span><b>${esc(e.type || e.kind || 'event')}</b> ${extra}</div>`;
      }).join('')
    : '<div class="log muted">No events recorded yet. Cycle starts, scans, vetoes, and mode changes appear here.</div>';
  const errorHtml = errors.length
    ? errors.slice(0, 80).map(e => {
        const t = e.time || e.ts || '';
        let when = '';
        try { when = t ? new Date(t).toLocaleString() : ''; } catch(_){ when = String(t); }
        const msg = e.message || e.error || e.detail || JSON.stringify(e);
        const detail = e.detail && e.detail !== e.message ? `<div class="muted" style="margin-top:5px;white-space:pre-wrap">${esc(String(e.detail).slice(0, 1200))}</div>` : '';
        return `<div class="log" style="border-left:3px solid var(--bad)"><span class="time">${esc(when)}</span><b class="danger-text">ERROR</b> ${esc(String(msg))}${detail}</div>`;
      }).join('')
    : '<div class="log muted">No errors recorded.</div>';
  // Production Events & Errors page containers
  const set = (id, html) => { const el = $(id); if(el) el.innerHTML = html; };
  set('eventsLog', eventHtml);
  set('errorsLog', errorHtml);
  // Legacy / dashboard preview IDs (if present)
  set('fullEvents', eventHtml);
  set('fullErrors', errorHtml);
  set('activityStream', events.length
    ? events.slice(0, 20).map(e => {
        const t = e.time || e.ts || '';
        let when = '';
        try { when = t ? new Date(t).toLocaleTimeString() : ''; } catch(_){}
        const payload = e.payload || {};
        return `<div class="log"><span class="time">${esc(when)}</span><b>${esc(e.type||'event')}</b> ${esc(payload.message||payload.action||payload.decision||payload.status||payload.reason||'')}</div>`;
      }).join('')
    : '<div class="log muted">No events.</div>');
  set('errorPreview', errors.length
    ? errors.slice(0, 10).map(e => {
        const t = e.time || e.ts || '';
        let when = '';
        try { when = t ? new Date(t).toLocaleTimeString() : ''; } catch(_){}
        return `<div class="log"><span class="time">${esc(when)}</span><b class="danger-text">ERROR</b> ${esc(e.message||e.error||'')}</div>`;
      }).join('')
    : '<div class="log muted">No recent errors.</div>');
}
function renderPositions(s){
  const rows=s.positions||[];
  $('positionsTable').innerHTML=rows.map(p=>`<tr><td>${esc(p.ticket)}</td><td>${esc(p.symbol)}</td><td class="${p.side==='BUY'?'position-buy':'position-sell'}"><b>${esc(p.side)}</b></td><td>${fmt(p.volume)}</td><td>${fmt(p.price_open)}</td><td>${fmt(p.price_current)}</td><td>${fmt(p.sl)}</td><td>${fmt(p.tp)}</td><td class="position-profit">${fmt(p.profit)}</td><td>${(p.sl>0&&p.tp>0)?'<span class="live-tag">PROTECTED</span>':'<span class="locked-tag">FAULT</span>'}</td><td>${s.system.live_ready ? `<button class="btn btn-small btn-danger" onclick="closePosition('${esc(p.ticket)}')">CLOSE</button><button class="btn btn-small" onclick="modifyPosition('${esc(p.ticket)}',${p.sl||0},${p.tp||0})">SL/TP</button>` : '<span class="muted">LOCKED</span>'}</td></tr>`).join('')||'<tr><td colspan="11">No open MT5 positions detected.</td></tr>';
  if($('accountSyncBadge'))$('accountSyncBadge').textContent=`ACCOUNT SYNC ${s.system.account_sync||'—'}`;
  if($('positionModeTag'))$('positionModeTag').textContent=s.system.live_execution_enabled?'LIVE MANAGEMENT':'READ ONLY';
}
function renderOrders(s){
  const rows=s.orders||[];
  const el=$('ordersTable');if(!el)return;
  el.innerHTML=rows.map(o=>`<tr><td>${esc(o.created)}</td><td>${esc(o.status)}</td><td>${esc(o.symbol)}</td><td>${esc(o.side)}</td><td>${fmt(o.volume)}</td><td>${fmt(o.entry)}</td><td>${fmt(o.stop)}</td><td>${fmt(o.target)}</td><td>${esc(o.ticket||'—')}</td><td>${esc(o.message||'')}</td></tr>`).join('')||'<tr><td colspan="10">No live order lifecycle records.</td></tr>';
}
function renderLifecycle(s){
  const l=s.lifecycle||{};
  const open=l.managed_open||[]; const alerts=l.alerts||[]; const ev=l.lifecycle_events||[];
  const box=$('lifecycleAlert');
  if(box){ box.style.display=alerts.length?'block':'none'; box.innerHTML=alerts.length?`<b>PROTECTION FAULT — LIVE DISARMED</b><p>${alerts.map(a=>esc(a.message)).join('<br>')}</p>`:''; }
  const sum=$('lifecycleSummary'); if(sum){ sum.innerHTML=[['Managed positions',open.length],['Protection faults',alerts.length],['Lifecycle events',ev.length],['Closed tracked','Persistent']].map(x=>`<div><span>${esc(x[0])}</span><b>${esc(x[1])}</b></div>`).join(''); }
  const el=$('lifecycleEvents'); if(el){ el.innerHTML=ev.slice(0,15).map(e=>`<div class="log"><span class="time">${new Date(e.time).toLocaleTimeString()}</span><b>${esc(e.event)}</b> ${esc(e.symbol||'')} ${e.ticket?`#${esc(e.ticket)}`:''}<div class="muted" style="margin-top:4px">${esc(JSON.stringify(e.payload||{}))}</div></div>`).join('')||'<div class="log">No lifecycle events yet.</div>'; }
}

function renderRiskDashboard(s){
  const risk = s.risk || {};
  const acct = s.account || {};
  const sys = s.system || {};
  const dec = s.decision || {};
  setTxt('riskVerdict', risk.authorized === true ? 'AUTHORIZED' : (risk.authorized === false ? 'BLOCKED' : (dec.decision || 'STANDBY')));
  setTxt('riskDetail', risk.reason || risk.detail || (sys.kill_switch ? 'Kill switch ON' : 'Awaiting actionable plan'));
  setTxt('riskMaxPct', risk.max_risk_pct != null ? fmt(risk.max_risk_pct, 2) + '%' : (sys.max_risk_pct != null ? fmt(sys.max_risk_pct, 2) + '%' : '—'));
  setTxt('riskExposure', risk.exposure != null ? fmt(risk.exposure) : (acct.margin != null ? fmt(acct.margin) : '—'));
  setTxt('riskKill', sys.kill_switch ? 'ON' : 'OFF');
  // Account grid
  const ag = $('accountGrid');
  if(ag){
    const rows = [
      ['Equity', fmt(acct.equity)],
      ['Balance', fmt(acct.balance)],
      ['Free margin', fmt(acct.free_margin)],
      ['Margin', fmt(acct.margin)],
      ['Margin level', acct.margin_level != null ? fmt(acct.margin_level, 1) + '%' : '—'],
      ['Floating P&L', fmt(acct.profit ?? acct.floating_pnl)],
      ['Leverage', acct.leverage != null ? '1:' + acct.leverage : '—'],
      ['Currency', acct.currency || '—'],
      ['Kill switch', sys.kill_switch ? 'ENGAGED' : 'RELEASED'],
      ['Trading mode', sys.paper_trading ? 'PAPER' : 'LIVE'],
      ['MT5', sys.mt5_connected ? 'CONNECTED' : 'OFFLINE'],
      ['Live ready', sys.live_ready ? 'YES' : 'NO'],
    ];
    ag.innerHTML = rows.map(([k,v]) => `<div><span class="muted">${esc(k)}</span><div style="font-weight:600;font-family:var(--mono);margin-top:4px">${esc(String(v))}</div></div>`).join('');
  }
}

function renderTrades(s){
  if(!$('tradeTable'))return;$('tradeTable').innerHTML=(s.history||[]).slice().reverse().map(h=>`<tr><td>${new Date(h.time).toLocaleTimeString()}</td><td>${esc(s.market.symbol)}</td><td class="diag">${esc(h.decision)}</td><td>${pct(h.confidence)}</td><td>${h.risk?'APPROVED':'BLOCKED'}</td><td>${fmt(h.price)}</td><td>${fmt(h.spread)}</td></tr>`).join('')||'<tr><td colspan="7">No decision history.</td></tr>';}

function setHTMLIfChanged(id, html){
  const el = $(id);
  if(!el) return;
  if(el.dataset.renderedHTML === html) return;
  el.innerHTML = html;
  el.dataset.renderedHTML = html;
}

function renderRuntimePipeline(s){
  const p=s.pipeline_state||{};
  const stages=Array.isArray(p.stages)?p.stages:[];
  const badge=$('runtimePipelineBadge');
  if(badge){
    const liveVerified=!!p.live_order_verified;
    badge.textContent=liveVerified?'LIVE FILL VERIFIED':(p.status||'WAITING').replaceAll('_',' ');
    badge.className=liveVerified?'live-tag':(p.status==='ERROR'?'danger-text':'locked-tag');
  }
  const summary=[
    ['Cycle',p.cycle_id||'—'],['Symbol',p.symbol||s.market?.symbol||'—'],
    ['Decision',`${p.decision||'WAIT'} · ${pct(Number(p.confidence||0))}`],
    ['Risk gate',p.risk_status||'NOT EVALUATED'],['Execution',p.execution_status||'NOT ATTEMPTED'],
    ['Verified live fill',p.live_order_verified?'YES':'NO']
  ];
  setHTMLIfChanged('runtimePipelineSummary',summary.map(x=>`<div><span>${esc(x[0])}</span><b>${esc(x[1])}</b></div>`).join(''));
  const trace=$('runtimePipelineTrace');
  if(trace){
    const blocks=Array.isArray(p.blockers)?p.blockers:[];
    const items=stages.slice().reverse().map(t=>`<div class="check-row ${['BLOCKED','FAILED'].includes(String(t.status||'').toUpperCase())?'check-fail':(['PASSED','COMPLETE'].includes(String(t.status||'').toUpperCase())?'check-pass':'')}" style="display:block;margin-bottom:7px"><div style="display:flex;justify-content:space-between;gap:10px"><b>${esc(t.stage||'Stage')}</b><strong>${esc(t.status||'INFO')}</strong></div><p>${esc(t.detail||'—')}</p><span class="muted">${t.time?new Date(t.time).toLocaleTimeString():'—'}</span></div>`);
    if(blocks.length)items.unshift(`<div class="safety-box" style="margin-bottom:10px"><b>Recorded blockers</b><p>${blocks.map(esc).join('<br>')}</p></div>`);
    trace.innerHTML=items.join('')||'<div class="log">Waiting for first decision-to-execution audit cycle.</div>';
  }
}


function _pill(el, text, kind){
  if(!el) return;
  el.textContent = text || '—';
  el.className = 'status-pill ' + (kind || 'neutral');
}
function _metricCards(container, rows){
  if(!container) return;
  container.innerHTML = (rows||[]).map(([k,v]) =>
    `<div class="metric-card"><span>${esc(k)}</span><div class="metric-main">${esc(String(v))}</div></div>`
  ).join('') || '<div class="muted">—</div>';
}
async function refreshContinuousPanels(){
  try{
    const cont = await get('/api/continuous');
    const track = await get('/api/continuous/tracking').catch(()=>({recent:[]}));
    const cmap = await get('/api/conflict-map').catch(()=>({}));
    renderContinuousPanels(cont, track, cmap);
  }catch(e){
    if($('guardianDetail')) $('guardianDetail').textContent = 'Continuous API: ' + (e.message||e);
  }
}
function renderContinuousPanels(cont, track, cmap){
  const bus = (cont && cont.bus) || {};
  const health = bus.health || (bus.last_resolution||{}).data_health || {};
  const res = bus.last_resolution || {};
  const chair = res.chair || {};
  const guardian = res.guardian || health;

  const hs = String(guardian.health_status || health.health_status || '—').toUpperCase();
  _pill($('guardianPill'), hs, hs==='PASS'?'good':(hs==='ABORT'?'bad':'warn'));
  _metricCards($('guardianSummary'), [
    ['STATUS', hs],
    ['ACTION', guardian.action || health.action || '—'],
    ['CONF', fmt(Number(guardian.confidence||0), 2)],
    ['MUTED', (guardian.muted_desks||[]).length],
  ]);
  if($('guardianDetail')){
    const reasons = (guardian.reason_codes||[]).join(', ');
    $('guardianDetail').textContent = (guardian.summary||'') + (reasons?('\nReasons: '+reasons):'');
  }

  const dec = String(res.decision || chair.decision || '—').toUpperCase();
  _pill($('chairPill'), dec, dec==='WAIT'?'neutral':(dec==='BUY'?'good':'bad'));
  _metricCards($('chairSummary'), [
    ['DECISION', dec],
    ['CONF', fmt(Number(res.confidence || chair.confidence || 0), 2)],
    ['TIER', res.tier_used != null ? res.tier_used : '—'],
    ['WF VETO', res.walkforward_veto ? 'YES' : 'NO'],
  ]);
  if($('chairNarrative')){
    $('chairNarrative').textContent = res.narrative || chair.decisive_reason || (res.rationale||[]).slice(0,3).join(' · ') || '—';
  }

  const cm = cmap.status ? cmap : (res.conflict_map || bus.conflict_map || {});
  _pill($('conflictPill'), cm.status || '—', cm.status==='SPLIT'?'warn':(cm.status==='FLAT'?'neutral':'good'));
  if($('conflictTable')) $('conflictTable').textContent = cm.table || 'No conflict map yet — wait for a mesh cycle.';
  const wm = res.wait_meta || {};
  if($('waitMetaBox')){
    if(wm.p_wait_correct != null){
      $('waitMetaBox').textContent = `WAIT meta-model: P(WAIT correct)=${fmt(Number(wm.p_wait_correct),3)} · recommend_wait=${wm.recommend_wait?'YES':'NO'}`;
    } else {
      $('waitMetaBox').textContent = '';
    }
  }

  const agents = bus.agents || {};
  if($('evidenceBusList')){
    const keys = Object.keys(agents);
    if(!keys.length){
      $('evidenceBusList').innerHTML = '<div class="muted">Evidence bus empty until next analysis cycle.</div>';
    } else {
      $('evidenceBusList').innerHTML = keys.map(a=>{
        const x = agents[a]||{};
        return `<div class="log-row"><b>${esc(a)}</b> · ${esc(x.direction||'—')} · conf ${fmt(Number(x.conviction||0),2)} · q ${fmt(Number(x.data_quality||0),2)}${x.non_voting?' · NV':''}<br><span class="muted">${esc((x.thesis||'').slice(0,160))}</span></div>`;
      }).join('');
    }
  }

  const recent = (track && track.recent) || [];
  if($('decisionTrackingList')){
    if(!recent.length){
      $('decisionTrackingList').innerHTML = '<div class="muted">No tracked decisions yet.</div>';
    } else {
      $('decisionTrackingList').innerHTML = recent.slice().reverse().slice(0,15).map(d=>{
        const o = d.outcomes||{};
        return `<div class="log-row"><b>${esc(d.symbol)}</b> ${esc(d.decision)} conf ${fmt(Number(d.confidence||0),2)} · ${esc((d.ts||'').slice(0,19))}<br><span class="muted">ret5=${o.ret_5??'—'} ret15=${o.ret_15??'—'} ret60=${o.ret_60??'—'}</span></div>`;
      }).join('');
    }
  }

  const settings = (window.__lastState && window.__lastState.settings) || {};
  const hybridOn = settings.local_ai_enabled === true;
  _pill($('hybridPill'), hybridOn ? 'HYBRID ON' : 'CLASSICAL', hybridOn ? 'good' : 'neutral');
  if($('hybridSummary')){
    $('hybridSummary').textContent = hybridOn
      ? `Local AI enabled · model ${settings.local_ai_deep_model||'qwen2.5:3b'} · influence=${settings.local_ai_can_influence_decision?'on':'off'} · hybrid enrich on`
      : 'Local AI off — pure classical desks';
  }
}
async function injectHumanEvidence(){
  const status = $('injectStatus');
  try{
    const body = {
      type: $('injectType')?.value || 'news',
      content: $('injectContent')?.value || '',
      symbol: (window.__lastState && window.__lastState.settings && window.__lastState.settings.symbol) || 'XAUUSD',
    };
    if(!body.content.trim()){ toast('Enter content'); return; }
    const res = await post('/api/continuous/inject', body);
    if(status) status.textContent = 'Injected · bus v' + (res.bus_version||'?');
    if($('injectContent')) $('injectContent').value = '';
    toast('EVIDENCE INJECTED');
    await refreshContinuousPanels();
  }catch(e){
    if(status) status.textContent = e.message||String(e);
    toast('INJECT FAILED');
  }
}
window.refreshContinuousPanels = refreshContinuousPanels;
window.injectHumanEvidence = injectHumanEvidence;


function renderMesh(s){
  renderRuntimePipeline(s);
  try{ refreshContinuousPanels(); }catch(_){}

  const m=s.ai_mesh||{}; const r=m.research||{}; const rd=m.risk_debate||{};
  // Regime-weighted council is visible in shadow mode too, so operators can inspect
  // its candidate/anchor/sizing calculations before explicitly enabling active mode.
  const rc=m.regime_council||{};
  const scoutStage=(m.stage_trace||[]).find(x=>x.stage==='scout_scanner')||{};
  const scout=(scoutStage.candidate||{});
  const rcMode=String(rc.mode||'shadow').toLowerCase();
  const rcAccepted=Boolean(rc.take_trade);
  _pill($('regimeCouncilModePill'), rcMode==='active'?(rcAccepted?'COUNCIL READY · RISK GATE REQUIRED':'ACTIVE · NOT QUALIFIED'):'SHADOW · NON-VOTING', rcMode==='active'?(rcAccepted?'good':'warn'):'neutral');
  _metricCards($('regimeCouncilSummary'),[
    ['MODE',rcMode.toUpperCase()],
    ['SCOUT',rc.scout_qualified?'QUALIFIED':'NOT QUALIFIED'],
    ['PRIMARY ANCHOR',fmt(Number(rc.anchor_score||0),3)],
    ['WEIGHTED SCORE',fmt(Number(rc.final_score||0),3)],
    ['SPECIALIST NET',fmt(Number(rc.specialist_net_score||0),3)],
    ['SIZE SCALAR',`${fmt(Number(rc.size_multiplier??1),2)}×`],
  ]);
  const rcDetails=[
    ['PRIMARY EDGE',`${String(rc.side||'FLAT')} · Regime ${fmt(Number(rc.regime_score||0),2)} / Structure ${fmt(Number(rc.structure_score||0),2)}`,rc.rationale||'Waiting for valid regime and structure reports.'],
    ['SCOUT SCREEN',scout.qualified?'BASELINE QUALIFIED':'BASELINE NOT QUALIFIED',(scout.reasons||[]).slice(0,3).join(' · ')||scout.error||'No scout evidence has been recorded yet.'],
    ['SECONDARY DESKS',rc.specialist_disagreement?'MATERIAL DISAGREEMENT':'No material directional conflict detected',`Entropy/uncertainty proxy ${fmt(Number(rc.entropy??.5),2)} · VPA proxy ${rc.vpa_score==null?'unavailable':fmt(Number(rc.vpa_score),2)}. Grouped by family to avoid duplicate votes.`],
  ];
  setHTMLIfChanged('regimeCouncilDetails',rcDetails.map(([k,v,d])=>`<div class="council-detail-card"><span>${esc(k)}</span><b>${esc(v)}</b><p>${esc(d||'—')}</p></div>`).join('')+((rc.adjustments||[]).length?`<div class="council-detail-card"><span>SIZE / UNCERTAINTY ADJUSTMENTS</span><b>${esc(`${fmt(Number(rc.size_multiplier??1),2)}× size scalar`)}</b>${rc.adjustments.map(a=>`<div class="council-adjustment">${esc(a)}</div>`).join('')}</div>`:''));
  if($('meshProvider'))$('meshProvider').textContent='CLASSICAL ONLY · RULE FLOOR';
  if($('meshConsensus'))$('meshConsensus').textContent=`FINAL ${m.decision||'WAIT'}`;
  if($('meshStageCount'))$('meshStageCount').textContent=`STAGES ${(m.stage_trace||[]).length}`;
  if($('bullCase'))$('bullCase').textContent=r.bull_case||'No bull case returned';
  if($('bearCase'))$('bearCase').textContent=r.bear_case||'No bear case returned';
  if($('researchWinner'))$('researchWinner').textContent=r.winner||'FLAT';
  if($('researchConfidence'))$('researchConfidence').textContent=pct(r.confidence);
  if($('researchRationale'))$('researchRationale').textContent=r.rationale||'—';
  if($('riskPreferred'))$('riskPreferred').textContent=rd.preferred||'—';
  if($('riskScore'))$('riskScore').textContent=pct(rd.risk_score);
  setHTMLIfChanged('riskDebateGrid',[['AGGRESSIVE',rd.aggressive],['NEUTRAL',rd.neutral],['CONSERVATIVE',rd.conservative]].map(x=>`<div class="debate-item"><b>${esc(x[0])}</b><span>${esc(x[1]||'—')}</span></div>`).join(''));
  const gate=m.risk_gate||{}; const checks=[...(gate.checks||[])]; const brokerChecks=s.execution_diagnostics||[];
  if($('meshGateBadge')){ const ok=gate.approved && (!brokerChecks.some(x=>x.passed===false && ['ORDER_SEND','VOLUME','MARGIN'].includes(x.code))); $('meshGateBadge').textContent=ok?'APPROVED':(gate.reason||s.last_execution?.status||'BLOCKED'); $('meshGateBadge').className=ok?'live-tag':'locked-tag'; }
  setHTMLIfChanged('riskChecks',[...checks,...brokerChecks].map(c=>`<div class="check-row ${c.passed?'check-pass':'check-fail'}"><div><b>${esc(c.label)}</b><span>${esc(c.code)}</span></div><strong>${c.passed?'PASS':'BLOCK'}</strong><p>${esc(c.detail)}</p></div>`).join('')||'<div class="muted">No risk/broker checks were evaluated because there was no actionable plan.</div>');
  setHTMLIfChanged('meshTrace',(m.stage_trace||[]).map(t=>`<div class="log"><span class="time">${esc(t.stage||'')}</span><b>${esc(t.agent||t.status||'STAGE')}</b><div class="muted" style="margin-top:4px">${esc(JSON.stringify(t))}</div></div>`).join('')||'<div class="log">No orchestration trace.</div>');
  const floorTranscript=((m.debate_floor||{}).transcript)||[];
  let transcript=[];
  if(floorTranscript.length){
    transcript=floorTranscript.map(u=>({
      speaker:u.speaker||'Trading Desk', role:u.role||'Specialist desk',
      source:`${u.phase||'Discussion'} · Round ${u.round||'—'}`, 
      text:u.message||'', evidence:[],
      objections:['CROSS','REBUTTAL'].includes(u.phase_code||u.phase) ? [] : [...(u.challenges||[]).map(x=>`Review point: ${x}`),...(u.concessions||[]).map(x=>`Concession: ${x}`)]
    }));
  } else {
    (m.reports||[]).forEach(a=>{transcript.push({speaker:a.agent||'ANALYST',role:a.role||'Specialist analyst',source:a.source||m.provider||'unknown',text:a.thesis||'No thesis',evidence:a.evidence||[],objections:a.invalidations||[]});});
    if(r.bull_case||r.bear_case){transcript.push({speaker:'BULL RESEARCHER',role:'Constructive case',source:m.provider||'unknown',text:r.bull_case||'No bull case',evidence:[],objections:[]});transcript.push({speaker:'BEAR RESEARCHER',role:'Adversarial case',source:m.provider||'unknown',text:r.bear_case||'No bear case',evidence:[],objections:[]});transcript.push({speaker:'RESEARCH MANAGER',role:`Decision: ${r.winner||'FLAT'} · confidence ${fmt(r.confidence,2)}`,source:m.provider||'unknown',text:r.rationale||'No rationale returned',evidence:[],objections:[]});}
    if(rd.aggressive||rd.neutral||rd.conservative){transcript.push({speaker:'RISK REVIEW',role:`Preferred: ${rd.preferred||'—'} · score ${fmt(rd.risk_score,2)}`,source:'risk debate',text:`Aggressive view: ${rd.aggressive||'—'}
Neutral view: ${rd.neutral||'—'}
Conservative view: ${rd.conservative||'—'}`,evidence:[],objections:rd.blockers||[]});}
  }
  setHTMLIfChanged('agentDiscussion',transcript.map(t=>`<div class="log"><span class="time">${esc(t.source)} · ${esc(t.role)}</span><b>${esc(t.speaker)}</b><div style="white-space:pre-wrap;margin-top:5px">${esc(t.text)}</div>${t.evidence.length?`<div class="muted" style="margin-top:5px">Evidence: ${esc(t.evidence.join(' · '))}</div>`:''}${t.objections.length?`<div class="muted" style="margin-top:4px">Objections / invalidations: ${esc(t.objections.join(' · '))}</div>`:''}</div>`).join('')||'<div class="log">Waiting for the first agent cycle.</div>');
  const learning=m.adaptive_learning||{}; const lp=m.learning_prediction||{};
  const advanced=learning.institutional_ml||{}; const advancedRun=learning.institutional_ml_last_evaluation||{};
  const iml=lp.institutional_ml||{}; const imlModel=iml.model||{}; const imlAnalogs=iml.analogs||{}; const imlSpace=iml.feature_space||{};
  const anyPromoted=Boolean(learning.promoted||advanced.model_promoted);
  const learningMode=learning.enabled===false&&!advanced.enabled?'DISABLED':(learning.mode==='error'?'LEARNING ERROR':(anyPromoted?'MODEL PROMOTED · RESEARCH ONLY':'SHADOW / VALIDATING'));
  if($('adaptiveLearningMode')){$('adaptiveLearningMode').textContent=learningMode;$('adaptiveLearningMode').className=(anyPromoted?'live-tag':'locked-tag');}
  const learningRows=[
    ['Baseline research model',learning.model_version||lp.model_version||'baseline-v1'],
    ['Recorded forecasts',fmt(learning.observations??0,0)],
    ['Finalized trade labels',fmt(learning.labeled_observations??lp.labeled_samples??0,0)],
    ['Baseline candidate holdout Brier',learning.candidate_brier==null?'—':fmt(learning.candidate_brier,4)],
    ['Baseline incumbent holdout Brier',learning.incumbent_brier==null?'—':fmt(learning.incumbent_brier,4)],
    ['Advanced ML stack',advanced.available===false?'OPTIONAL ML UNAVAILABLE':(advanced.model_promoted?'PROMOTED · RESEARCH ONLY':'COLLECTING / VALIDATING')],
    ['Advanced ML champion',advanced.model_family?`${advanced.model_family} · ${advanced.model_version||''}`:'No validated multi-model champion'],
    ['Advanced ML finalized samples',`${fmt(advanced.labeled_observations??0,0)} / ${fmt(advanced.minimum_required??160,0)}`],
    ['Advanced ML holdout Brier',advanced.candidate_holdout_brier==null?'—':fmt(advanced.candidate_holdout_brier,4)],
    ['Advanced ML latest evaluation',advancedRun.note||advanced.last_note||'No chronological model search yet'],
    ['Current candidate model score',imlModel.score==null?'—':fmt(imlModel.score,4)+' · uncalibrated research score'],
    ['Current candidate analog assessment',imlAnalogs.assessment||'Available when an actionable candidate has enough history'],
    ['Historical pattern avoidance hint',imlSpace.avoidance_hint||'Awaiting a candidate-side research snapshot'],
    ['PCA explained variance',Array.isArray(imlSpace.explained_variance_ratio)?imlSpace.explained_variance_ratio.map(x=>fmt(x*100,1)+'%').join(' / '):'—'],
    ['Execution authority','LOCKED — deterministic risk controls only'],
    ['Status',learning.note||learning.error||(learning.recorded?'Point-in-time forecast persisted':'Learner remains in shadow mode')],
    ...(learning.agent_attribution||[]).slice(0,4).map(a=>[`Attribution · ${a.agent} / ${a.regime}`,`n=${a.n} · signed contribution ${fmt(a.avg_contribution,3)} · positive-PnL rate ${pct(a.hit_rate)}`])
  ];
  setHTMLIfChanged('adaptiveLearningSummary',learningRows.map(x=>`<div class="check-row"><div><b>${esc(x[0])}</b><span>${esc(String(x[1]))}</span></div><strong>${x[0]==='Execution authority'?'LOCKED':'INFO'}</strong></div>`).join(''));
  if($('discussionMode')){$('discussionMode').textContent='CLASSICAL EVIDENCE + RULE FLOOR';$('discussionMode').className='locked-tag';}
}

async function queueImprovementTest(){
  try{await post('/api/control',{action:'accept_improvement_for_test'});toast('IMPROVEMENT QUEUED FOR REPLAY');await refresh();}
  catch(e){toast(`ERROR: ${e.message}`);}
}
async function uploadShadow(){
  const f=$('shadowFile')?.files?.[0]; if(!f){toast('SELECT A CSV FIRST');return;}
  try{
    const buf=await f.arrayBuffer();
    let binary=''; const bytes=new Uint8Array(buf); const chunk=0x8000;
    for(let i=0;i<bytes.length;i+=chunk) binary+=String.fromCharCode(...bytes.subarray(i,i+chunk));
    const b64=btoa(binary);
    await post('/api/shadow/upload',{name:f.name,content_base64:b64});
    $('shadowMsg').textContent='Journal analysed';
    toast('SHADOW ACCOUNT UPDATED'); await refresh();
  }catch(e){$('shadowMsg').textContent=e.message;toast(`ERROR: ${e.message}`);}
}
function renderResearch(s){
  const r = s.research || {};
  setTxt('researchStrategy', r.strategy_id || r.strategy || r.production_strategy || '—');
  setTxt('researchSharpe', r.trade_sharpe != null ? fmt(r.trade_sharpe, 2) : (r.sharpe != null ? fmt(r.sharpe, 2) : '—'));
  setTxt('researchWinRate', r.win_rate != null ? fmt(r.win_rate, 1) + '%' : '—');
  setTxt('researchHealth', r.health || r.status || '—');
  const badge = $('researchHealthBadge');
  if(badge){
    const h = String(r.health || r.status || 'unknown').toLowerCase();
    badge.textContent = (r.health || r.status || 'HEALTH UNKNOWN').toString().toUpperCase();
    badge.className = 'status-pill ' + (h.includes('ok') || h.includes('good') || h.includes('healthy') ? 'good' : h.includes('warn') ? 'warn' : 'neutral');
  }
  const body = $('researchBody');
  if(body){
    const lines = [];
    lines.push(`Mode: classical numerical research · offline RL is non-voting`);
    if(r.message) lines.push(r.message);
    if(r.detail) lines.push(r.detail);
    if(r.last_eval) lines.push('Last eval: ' + r.last_eval);
    if(r.notes) lines.push(r.notes);
    if(Array.isArray(r.events)) r.events.slice(0, 8).forEach(e => lines.push(typeof e === 'string' ? e : JSON.stringify(e)));
    body.innerHTML = lines.length
      ? lines.map(x => `<div class="log">${esc(String(x))}</div>`).join('')
      : `<div class="log muted">Research lab awaits closed trade outcomes and offline evaluation windows. Unvalidated models cannot authorize live orders.</div>`;
  }
}
function renderShadow(s){
  const sh = s.shadow || {};
  const p = sh.profile || sh.summary || {};
  const body = $('shadowBody');
  if(!body) return;
  const trades = p.trades ?? p.closed ?? 0;
  const wr = p.win_rate_pct ?? p.win_rate;
  const exp = p.expectancy;
  const streak = p.max_loss_streak;
  body.innerHTML = `
    <div class="panel-head"><div><div class="panel-kicker">SHADOW / PAPER TRACKING</div><h2>Hypothesis vs market outcomes</h2></div>
      <span class="status-pill ${sh.source ? 'good' : 'neutral'}">${esc(sh.source || 'NO DATA YET')}</span></div>
    <div class="hero-grid" style="margin-top:12px">
      <div class="metric-card"><span>SHADOW TRADES</span><div class="metric-main">${fmt(trades,0)}</div><small>Paper / uploaded</small></div>
      <div class="metric-card"><span>WIN RATE</span><div class="metric-main">${wr != null ? fmt(wr,1)+'%' : '—'}</div><small>Closed only</small></div>
      <div class="metric-card"><span>EXPECTANCY</span><div class="metric-main">${exp != null ? fmt(exp,3) : '—'}</div><small>Per trade</small></div>
      <div class="metric-card"><span>MAX LOSS STREAK</span><div class="metric-main">${streak != null ? fmt(streak,0) : '—'}</div><small>Risk of ruin signal</small></div>
    </div>
    <p class="muted" style="margin-top:12px">Shadow account compares agent hypotheses to outcomes without risking capital. Upload history or run paper fills to populate this desk. Classical / RL research desks write here; they cannot authorize live orders until validated.</p>
    ${(sh.notes || sh.message) ? `<div class="log" style="margin-top:8px">${esc(sh.notes || sh.message)}</div>` : ''}
  `;
}
function renderRunbooks(s){
  const list = $('runbooksList');
  if(!list) return;
  const cards = s.run_cards || s.runbooks || [];
  if(!cards.length){
    list.innerHTML = `<div class="panel"><div class="panel-head"><div><div class="panel-kicker">RUN CARDS</div><h2>No cycle cards yet</h2></div></div>
      <p class="muted">Each analysis cycle records a run card: symbol, decision, consensus strength, blockers, and execution outcome. Cards appear after continuous monitoring completes cycles with MT5 connected.</p>
      <div class="log">Pipeline stages: MARKET_DATA → AGENTS → DEBATE_FLOOR → RISK → EXECUTION → CYCLE_COMPLETE</div></div>`;
    return;
  }
  list.innerHTML = cards.slice(0, 30).map(c => {
    const dec = (c.decision || c.agreement || 'WAIT').toUpperCase();
    const cls = dec === 'BUY' ? 'good' : dec === 'SELL' ? 'bad' : 'neutral';
    return `<div class="log"><span class="time">${esc(c.created || c.ts || c.time || '')}</span>
      <span class="badge badge-${cls === 'good' ? 'good' : cls === 'bad' ? 'bad' : 'neutral'}">${esc(dec)}</span>
      <b>${esc(c.symbol || '—')}</b>
      · conf ${fmt(c.confidence ?? c.agreement_confidence, 2)}
      · ${esc(c.status || c.execution_status || 'recorded')}
      <div class="muted setup-subline">${esc((c.summary || c.rationale || c.blocker || '').toString().slice(0, 180))}</div></div>`;
  }).join('');
}


async function askCopilot(){
  const q=$('researchQuestion')?.value?.trim(); if(!q){toast('ENTER A RESEARCH QUESTION');return;}
  $('copilotAnswer').textContent='Researching…';
  try{const r=await post('/api/research/ask',{question:q});$('copilotAnswer').textContent=r.answer||'No answer';}
  catch(e){$('copilotAnswer').textContent=`ERROR: ${e.message}`;}
}
function renderStrategies(s){
  const list = $('strategiesList');
  if(!list) return;
  const rows = s.strategies || [];
  if(!rows.length){
    list.innerHTML = `<div class="panel"><div class="panel-head"><div><div class="panel-kicker">STRATEGY REGISTRY</div><h2>Classical / RL / rule strategies</h2></div></div>
      <p class="muted">Registered production and research strategies appear here. The runtime is <b>classical-only</b> for live authorization: numerical specialists, scoring matrices, offline RL (non-voting), and hard risk gates. No generative LLM authorizes orders.</p>
      <div class="log">Expected entries: momentum · mean-reversion · structure · session liquidity · offline Q-policy (research)</div></div>`;
    return;
  }
  list.innerHTML = `<div class="table-wrap"><table><thead><tr><th>ID</th><th>NAME</th><th>FAMILY</th><th>STATUS</th><th>NOTES</th></tr></thead><tbody>
    ${rows.map(r => `<tr><td><b>${esc(r.id || r.strategy_id || '—')}</b></td><td>${esc(r.name || r.title || '—')}</td>
      <td>${esc(r.family || r.kind || 'classical')}</td>
      <td><span class="badge ${r.status === 'production' || r.active ? 'badge-good' : 'badge-neutral'}">${esc(r.status || (r.active ? 'active' : 'research'))}</span></td>
      <td class="muted">${esc((r.notes || r.description || '').toString().slice(0, 100))}</td></tr>`).join('')}
    </tbody></table></div>`;
}


function backtestPayload(){
  const optionalNumber = (id) => { const raw = $(id)?.value; return raw === '' || raw == null ? undefined : Number(raw); };
  const riskPct = Number($('btRisk')?.value || 0.5);
  let mode = String($('btOptimizeMode')?.value || 'grid');
  if(mode === 'walk_forward') mode = 'walkforward';
  return {
    symbol: String($('btSymbol')?.value || $('tapeSymbol')?.textContent || 'XAUUSD').trim().toUpperCase(),
    timeframe: 'M5',
    bars: Number($('btBars')?.value || 3000),
    csv: String($('btCsv')?.value || '').trim() || undefined,
    execution_model: String($('btExecutionModel')?.value || 'ohlc'),
    ticks_csv: String($('btTicksCsv')?.value || '').trim() || undefined,
    equity: Number($('btEquity')?.value || 10000),
    risk_fraction: riskPct / 100,
    strategy_id: String($('btStrategy')?.value || 'trend_following'),
    objective: String($('btObjective')?.value || 'composite'),
    max_trials: Number($('btTrials')?.value || 40),
    folds: Number($('btFolds')?.value || 4),
    is_ratio: Number($('btIsRatio')?.value || 0.7),
    mode: mode,
    spread_price: String($('btExecutionModel')?.value || 'ohlc') === 'ticks' ? undefined : optionalNumber('btSpread'),
    slippage_price: optionalNumber('btSlippage'),
    commission_bps: Number($('btCommission')?.value || 0),
    include_trades: true,
  };
}
function loadBacktestDefaultsFromSettings(){
  const st = (window.__lastState && window.__lastState.settings) || {};
  const bt = st.backtest || st;
  const set = (id, v) => { const el=$(id); if(el && v!=null && v!=='') el.value = v; };
  set('btSymbol', bt.bt_symbol || bt.symbol || $('setBtSymbol')?.value);
  set('btStrategy', bt.bt_strategy || $('setBtStrategy')?.value);
  set('btBars', bt.bt_bars || $('setBtBars')?.value);
  set('btEquity', bt.bt_equity || $('setBtEquity')?.value);
  set('btRisk', bt.bt_risk_pct || $('setBtRisk')?.value);
  set('btExecutionModel', bt.bt_execution_model || $('setBtExecutionModel')?.value);
  set('btObjective', bt.bt_objective || $('setBtObjective')?.value);
  set('btTrials', bt.bt_max_trials || $('setBtTrials')?.value);
  set('btFolds', bt.bt_folds || $('setBtFolds')?.value);
  set('btIsRatio', bt.bt_is_ratio || $('setBtIsRatio')?.value);
  set('btCommission', bt.bt_commission_bps || $('setBtCommission')?.value);
  if(typeof toast==='function') toast('Backtest defaults loaded');
}
window.loadBacktestDefaultsFromSettings = loadBacktestDefaultsFromSettings;

function setBacktestBusy(busy, message){
  const state=$('backtestRunState'); if(state) state.textContent=busy?'RUNNING…':'READY';
  const msg=$('backtestMessage'); if(msg) msg.textContent=message|| (busy?'Historical replay running…':'');
  document.querySelectorAll('#backtest .control-row button').forEach(b=>{b.disabled=busy;});
}
function renderBacktestMetrics(m){
  const cards=$('backtestMetricCards'); if(!cards)return;
  const costStress=(m.cost_sensitivity||[]).find(x=>Number(x.cost_multiplier)===2.0);
  const values=[
    ['TRADES',fmt(Number(m.trades||0),0),'Win rate '+pct(Number(m.win_rate||0))],
    ['NET P&L',fmt(Number(m.net_pnl||0)), 'Gross '+fmt(Number(m.gross_pnl||0))+' · modeled costs '+fmt(Number(m.estimated_costs||0))],
    ['TRADE-R SHARPE PROXY',fmt(Number(m.sharpe||0),3),'Not a time-bar annualized ratio'],
    ['MAX DRAWDOWN',fmt(Number(m.max_drawdown_pct||0),2)+'%','Equity peak-to-trough'],
    ['NET P&L AT 2× COST',costStress?fmt(Number(costStress.net_pnl||0)):'—','Same trade list; costs doubled only'],
    ['COST DRAG',fmt(Number(m.cost_drag_pct_initial_equity||0),2)+'%','Of starting equity'],
  ];
  cards.innerHTML=values.map(([a,b,c])=>`<div class="metric-card"><span>${esc(a)}</span><div class="metric-main">${esc(b)}</div><small>${esc(c)}</small></div>`).join('');
}
function renderBacktestOutput(data, kind){
  const root=$('backtestResult'); if(!root)return;
  if(data.error){root.innerHTML=`<div class="safety-box"><b>Backtest failed</b><p>${esc(data.error)}</p></div>`;return;}
  if(kind==='all'){
    const results=data.results||{};
    const rows=Object.entries(results).map(([sid,m])=>`<tr><td>${esc(sid)}</td><td>${esc(String(m.trades??0))}</td><td>${esc(pct(Number(m.win_rate||0)))}</td><td>${esc(fmt(Number(m.net_pnl||0)))}</td><td>${esc(fmt(Number(m.sharpe||0),3))}</td><td>${esc(fmt(Number(m.max_drawdown_pct||0),2))}%</td><td>${esc(fmt(Number(m.estimated_costs||0)))}</td></tr>`).join('');
    root.innerHTML=`<p>Source: <b>${esc(data.data_source||'—')}</b> · ${esc(String(data.bars||0))} M5 bars. Metrics are net of the cost assumptions in each run.</p><div class="table-wrap"><table><thead><tr><th>STRATEGY</th><th>TRADES</th><th>WIN RATE</th><th>NET P&amp;L</th><th>TRADE-R SHARPE PROXY</th><th>MAX DD</th><th>COSTS</th></tr></thead><tbody>${rows}</tbody></table></div>`;
    return;
  }
  if(kind==='optimization'){
    const folds=data.folds||[]; const trials=data.trials||[];
    const list=folds.length?`<div class="table-wrap"><table><thead><tr><th>FOLD</th><th>IS RANGE</th><th>OOS RANGE</th><th>IS SCORE</th><th>OOS SCORE</th><th>OOS P&amp;L</th></tr></thead><tbody>${folds.map(f=>`<tr><td>${esc(String(f.fold))}</td><td>${esc(String(f.is_start))}–${esc(String(f.is_end))}</td><td>${esc(String(f.oos_start))}–${esc(String(f.oos_end))}</td><td>${esc(fmt(Number(f.is_score||0),3))}</td><td>${esc(fmt(Number(f.oos_score||0),3))}</td><td>${esc(fmt(Number(f.oos_metrics?.net_pnl||0)))}</td></tr>`).join('')}</tbody></table></div>`:'';
    const best=data.recommended_params||data.best_params||{};
    const isWalkForward=Array.isArray(data.folds);
    const scoreLine=isWalkForward
      ? `Average in-sample score: ${fmt(Number(data.avg_is_score||0),3)} · Average OOS score: ${fmt(Number(data.avg_oos_score||0),3)}`
      : `Best parameter-search score: ${fmt(Number(data.best_score||0),3)}. This is an in-sample selection score, not out-of-sample evidence.`;
    root.innerHTML=`<p class="muted">Source: ${esc(data.data_source||'MT5/CSV source')} · ${esc(String(data.bars||'—'))} M5 bars</p><div class="safety-box"><b>${data.robust===true?'Walk-forward marked robust':data.robust===false?'Walk-forward NOT robust':'Optimization result'}</b><p>${esc((data.notes||[]).join(' · ')||'Parameter search complete. Validate independently before using these parameters.')}</p></div><p><b>Selected parameters:</b> ${esc(JSON.stringify(best))}</p><p>${esc(scoreLine)}</p>${list}${trials.length?`<p>Trials evaluated: ${esc(String(data.n_trials||trials.length))}. Showing top ${Math.min(trials.length,10)}:</p><pre>${esc(JSON.stringify(trials.slice(0,10).map(t=>({score:t.score,params:t.params,metrics:{trades:t.metrics?.trades,net_pnl:t.metrics?.net_pnl,sharpe:t.metrics?.sharpe}})),null,2))}</pre>`:''}`;
    return;
  }
  renderBacktestMetrics(data);
  const notes=(data.notes||[]).map(x=>`<li>${esc(x)}</li>`).join('');
  const cost=data.params?.cost_model||{};
  const trades=data.trade_log||[];
  const costRows=(data.cost_sensitivity||[]).map(x=>`<tr><td>${esc(fmt(Number(x.cost_multiplier||0),1))}×</td><td>${esc(fmt(Number(x.net_pnl||0)))}</td><td>${esc(fmt(Number(x.return_pct_initial_equity||0),2))}%</td></tr>`).join('');
  const costSensitivity=costRows?`<h3>Transaction-cost sensitivity</h3><p>Same fixed trade list with modeled costs multiplied; signals and position sizing are not rerun.</p><div class="table-wrap"><table><thead><tr><th>COST MULTIPLIER</th><th>NET P&amp;L</th><th>RETURN ON STARTING EQUITY</th></tr></thead><tbody>${costRows}</tbody></table></div>`:'';
  root.innerHTML=`<p>Source: <b>${esc(data.data_source||'MT5/CSV source not returned')}</b> · ${esc(String(data.bars||0))} M5 bars · Execution model: <b>${esc(data.execution_model||data.params?.execution_model||'ohlc_bar_simulation')}</b> · Strategy: ${esc(data.params?.strategy_id||$('btStrategy')?.value||'router')}</p><div class="safety-box"><b>Cost assumptions</b><p>Spread: ${esc(String(cost.spread_price??'—'))} (${esc(cost.spread_source||'—')}); slippage per side: ${esc(String(cost.slippage_price_per_side??'—'))} (${esc(cost.slippage_source||'—')}); commission per side: ${esc(String(cost.commission_bps_per_side??'—'))} bps (${esc(cost.commission_source||'—')}).</p><p>${esc(cost.warning||'Historical spread/fill quality may not be present in OHLC data.')}</p></div>${notes?`<ul>${notes}</ul>`:''}<p>Win rate ${esc(pct(Number(data.win_rate||0)))} · Profit factor ${esc(fmt(Number(data.profit_factor||0),3))} · Expectancy ${esc(fmt(Number(data.expectancy||0)))} · Avg R ${esc(fmt(Number(data.avg_r||0),3))}</p>${costSensitivity}${trades.length?`<h3>Recent trades (up to ${trades.length})</h3><div class="table-wrap"><table><thead><tr><th>ENTRY TIME</th><th>SIDE</th><th>ENTRY</th><th>EXIT</th><th>NET P&amp;L</th><th>COST</th><th>EXIT REASON</th></tr></thead><tbody>${trades.slice(-100).reverse().map(t=>`<tr><td>${esc(t.entry_time||'')}</td><td>${esc(t.side||'')}</td><td>${esc(fmt(Number(t.entry||0)))}</td><td>${esc(fmt(Number(t.exit_price||0)))}</td><td>${esc(fmt(Number(t.pnl||0)))}</td><td>${esc(fmt(Number(t.estimated_costs||0)))}</td><td>${esc(t.exit_reason||'')}</td></tr>`).join('')}</tbody></table></div>`:''}`;
}
async function executeBacktest(kind){
  const msg=$('backtestMessage');
  try{
    const body=backtestPayload();
    if(!body.symbol || body.bars<100 || body.equity<=0 || body.risk_fraction<=0 || body.commission_bps<0) throw Error('Check symbol, bar count, equity, risk, and fee inputs.');
    setBacktestBusy(true, body.execution_model==='ticks'?'Loading historical quotes and replaying executable bid/ask fills…':(kind==='optimization'?'Walk-forward / parameter validation running…':'Historical OHLC backtest running…'));
    let data;
    if(kind==='optimization'){
      data=await post('/api/backtest/optimize',{...body,mode:body.mode||'grid'});
      renderBacktestOutput(data,'optimization');
    }else{
      data=await post('/api/backtest/strategy',{...body,all:kind==='all'});
      if(kind==='all') renderBacktestOutput(data,'all'); else renderBacktestOutput(data,'single');
    }
    setBacktestBusy(false, data.data_source?`Completed using ${data.data_source}. Inspect the cost and data-quality assumptions before interpreting performance.`:'Completed. Inspect notes and out-of-sample validation before using results.');
    if (data.experiment_ledger_warning) { const note=$('backtestMessage'); if(note) note.textContent += ' · '+data.experiment_ledger_warning; }
    refreshResearchExperiments().catch(()=>{});
  }catch(e){setBacktestBusy(false,'Backtest request failed: '+e.message);const root=$('backtestResult');if(root)root.innerHTML=`<div class="safety-box"><b>Request failed</b><p>${esc(e.message)}</p></div>`;}
}
function runBacktest(){executeBacktest('single');}
function runBacktestAll(){executeBacktest('all');}
function runBacktestOptimize(mode){const x=$('btOptimizeMode');if(x)x.value=mode;executeBacktest('optimization');}

window.__researchExperimentOffset = window.__researchExperimentOffset || 0;
async function refreshResearchExperiments(nextOffset){
  const status=$('researchExperimentStatus'), body=$('researchExperimentBody'), pager=$('researchExperimentPager');
  if(!body)return;
  if(Number.isFinite(Number(nextOffset)))window.__researchExperimentOffset=Math.max(0,Number(nextOffset));
  const limit=40, offset=window.__researchExperimentOffset||0;
  if(status)status.textContent='Loading saved research experiments…';
  try{
    const data=await get(`/api/research/experiments?limit=${limit}&offset=${offset}`,{force:true});
    const rows=data.items||[];
    if(!rows.length){body.innerHTML='<tr><td colspan="8" class="muted">No saved experiments on this page. Run a Backtest Lab or Finance Strategy Research experiment.</td></tr>';if(status)status.textContent=data.total?'No experiments on this page.':'No saved experiments. Successful research runs will appear here.';if(pager)pager.innerHTML=data.total?`<button class="btn btn-ghost" type="button" onclick="refreshResearchExperiments(${Math.max(0,offset-limit)})">Previous</button>`:'';return;}
    body.innerHTML=rows.map(x=>{
      const m=x.metrics||{};
      const result=Number.isFinite(Number(m.net_pnl))?fmt(Number(m.net_pnl)):Number.isFinite(Number(m.best_score))?fmt(Number(m.best_score),3):Number.isFinite(Number(m.total_return))?pct(Number(m.total_return)):'—';
      return `<tr><td>${esc(String(x.created_at||'').replace('T',' ').replace('Z',''))}</td><td><b>${esc(x.experiment_id||'')}</b><small class="muted" style="display:block">${esc(x.kind||'')}</small></td><td>${esc(x.symbol||'—')} / ${esc(x.timeframe||'—')}</td><td>${esc(x.strategy_id||'—')}<small class="muted" style="display:block">${esc(x.execution_model||'')}</small></td><td>${esc(String(x.bars??0))}</td><td>${esc(result)}</td><td><span class="badge badge-neutral">${esc(x.validation_tier||'RESEARCH_ONLY')}</span></td><td><button class="btn btn-ghost" type="button" data-experiment-id="${esc(x.experiment_id||'')}">Details</button></td></tr>`;
    }).join('');
    body.querySelectorAll('button[data-experiment-id]').forEach(button=>button.addEventListener('click',()=>viewResearchExperiment(button.dataset.experimentId)));
    if(status)status.textContent=`Showing ${offset+1}–${offset+rows.length} of ${data.total} saved experiment(s). Results are research-only and never auto-promote strategies.`;
    if(pager)pager.innerHTML=`<button class="btn btn-ghost" type="button" ${data.has_previous?'':'disabled'} onclick="refreshResearchExperiments(${Math.max(0,offset-limit)})">Previous</button><span class="muted">Page ${Math.floor(offset/limit)+1} of ${Math.max(1,Math.ceil(Number(data.total||0)/limit))}</span><button class="btn btn-ghost" type="button" ${data.has_next?'':'disabled'} onclick="refreshResearchExperiments(${offset+limit})">Next</button>`;
  }catch(e){if(status)status.textContent='Experiment history unavailable: '+e.message;body.innerHTML=`<tr><td colspan="8" class="muted">${esc(e.message)}</td></tr>`;if(pager)pager.innerHTML='';}
}
async function viewResearchExperiment(id){
  if(!id)return;
  const panel=$('researchExperimentDetailPanel'), title=$('researchExperimentDetailTitle'), detail=$('researchExperimentDetail');
  try{
    const data=await get('/api/research/experiments/'+encodeURIComponent(id),{force:true});
    const item=data.item||{};
    if(title)title.textContent=`${item.experiment_id||'Experiment'} · ${item.symbol||''} ${item.timeframe||''}`;
    if(detail)detail.textContent=JSON.stringify(item,null,2);
    if(panel)panel.style.display='block';
  }catch(e){if(detail)detail.textContent='Experiment detail unavailable: '+e.message;if(panel)panel.style.display='block';}
}
window.refreshResearchExperiments=refreshResearchExperiments;
window.viewResearchExperiment=viewResearchExperiment;

async function refreshProviderDiagnostics(){
  try{
    const p=await get('/api/providers'); const quant=p.intelligence||{}; const news=p.news||{};
    if($('aiProviderName'))$('aiProviderName').textContent=quant.mode||'CLASSICAL ONLY';
    if($('aiModelName'))$('aiModelName').textContent=quant.language_model_calls_enabled?'ENABLED':'DISABLED';
    if($('aiProviderStatus'))$('aiProviderStatus').textContent=quant.language_model_calls_enabled?'CHECK CONFIGURATION':'CLASSICAL ONLY';
    if($('providerDiagnostic'))$('providerDiagnostic').textContent=quant.language_model_calls_enabled?'UNEXPECTED: CHECK RUNTIME MODE':'ACTIVE · LOCAL NUMERICAL ENGINE';
    if($('newsProviderDiagnostic'))$('newsProviderDiagnostic').textContent=news.configured_feeds?`${news.configured_feeds} feed(s) · ${news.items||0} headline(s)`:'NO RSS FEEDS CONFIGURED';
    if($('providerInstructions'))$('providerInstructions').innerHTML=`<b>Classical-only trading runtime · no LLM calls</b><p>Deterministic analysts and risk rules run locally. ${news.configured_feeds?'RSS/news feeds are configured as external context inputs; they are not a generative model.':'RSS/news context is not configured.'} Reinforcement-learning and unvalidated statistical models remain non-voting research only until independently validated.</p>`;
  }catch(e){if($('providerDiagnostic'))$('providerDiagnostic').textContent='Diagnostics unavailable';if($('newsProviderDiagnostic'))$('newsProviderDiagnostic').textContent='Diagnostics unavailable';}
}

function renderWatchlist(s){
  const el=$('watchlistStrip'); if(!el) return;
  const quotes=s.watchlist||{};
  const active=(s.market&&s.market.symbol)||(s.settings&&s.settings.symbol)||'';
  const keys=Object.keys(quotes);
  if(!keys.length){
    const wl=(s.settings&&s.settings.symbols)||[];
    el.innerHTML=wl.map(sym=>`<div class="watchlist-chip ${sym===active?'active':''}" onclick="switchSymbol('${esc(sym)}')"><span class="sym">${esc(sym)}</span><span class="px">—</span></div>`).join('')||'<span class="muted">No watchlist</span>';
    return;
  }
  el.innerHTML=keys.map(sym=>{
    const q=quotes[sym]||{};
    const px=q.price;
    return `<div class="watchlist-chip ${sym===active?'active':''}" onclick="switchSymbol('${esc(sym)}')"><span class="sym">${esc(sym)}</span><span class="px">${fmt(px)}</span><span class="sp">spr ${fmt(q.spread)}</span></div>`;
  }).join('');
}
function populateSymbolSelect(s){
  try{ filterSymbolDatalist(s); }catch(e){}

  const sel=$('setSymbol'); if(!sel) return;
  const catalog=window.__symbolCatalogCache||s.symbol_catalog||[];
  const wl=(s.settings&&s.settings.symbols)||[];
  const active=(s.settings&&s.settings.symbol)||'XAUUSD';
  // Build options from catalog + watchlist
  const seen=new Set();
  let html='';
  const add=(sym,label)=>{ if(seen.has(sym)) return; seen.add(sym); html+=`<option value="${esc(sym)}" ${sym===active?'selected':''}>${esc(sym)}${label?' — '+esc(label):''}</option>`; };
  wl.forEach(s=>add(s,''));
  catalog.forEach(c=>add(c.symbol, c.name||''));
  if(!seen.has(active)) add(active,'');
  if(sel.dataset.filled!==String(seen.size)+active){
    sel.innerHTML=html;
    sel.dataset.filled=String(seen.size)+active;
    sel.value=active;
  }
  const wlInput=$('setWatchlist');
  if(wlInput && document.activeElement!==wlInput){
    wlInput.value=wl.join(',');
  }
}
function applySymbolSearch(){
  const input = $('symbolSearch');
  if(!input) return;
  let raw = (input.value || '').trim().toUpperCase();
  if(!raw) return;
  raw = raw.split('—')[0].split(' - ')[0].trim().toUpperCase();
  switchSymbol(raw);
}
function filterSymbolDatalist(s){
  const dl = $('symbolSearchList');
  if(!dl) return;
  const catalog = window.__symbolCatalogCache || s.symbol_catalog || [];
  const wl = (s.settings && s.settings.symbols) || [];
  const seen = new Set();
  let html = '';
  const add = (sym, name) => { if(!sym || seen.has(sym)) return; seen.add(sym); html += `<option value="${esc(sym)}">${esc(name||sym)}</option>`; };
  wl.forEach(x => add(x, x));
  catalog.forEach(c => add(c.symbol, (c.symbol + ' — ' + (c.name||'')).trim()));
  dl.innerHTML = html;
}
async function switchSymbol(sym){
  const key=String(sym||'').trim().toUpperCase();
  if(!key) return;
  ChartState.userSelectedSymbol=true;
  ChartState.symbol=key;
  ChartState.candles=[];
  ChartState.fetchedSymbol=null; ChartState.fetchedTf=null;
  ChartState._lastDataFingerprint=''; ChartState._lastDrawnFingerprint=''; ChartState._historyError='';
  ChartState.scaleLo=null; ChartState.scaleHi=null;
  if($('chartSymbolInput')) $('chartSymbolInput').value=key;
  drawCandles();
  // Load the chart's exact history immediately; the runtime selection is persisted in parallel.
  const historyTask=loadChartHistory(key,ChartState.tf,{force:true,silent:false,notify:true});
  try{
    const wl=($('setWatchlist')&&$('setWatchlist').value)||'';
    await post('/api/settings',{symbol:key, symbols:wl, interval:Number($('setInterval')?.value||5), lookback:Number($('setLookback')?.value||300)});
    toast('SYMBOL '+key);
    await Promise.allSettled([historyTask]);
    await refresh();
  }catch(e){ toast('SETTINGS SAVE ERROR: '+e.message); }
}

async function fetchActivePageData(page){
  const jobs=[];
  const results={};
  const add=(key,url,region)=>jobs.push(get(url,{silent:true,region,retries:2}).then(data=>{results[key]=data;}).catch(err=>{results[key]={_error:String(err?.message||err)};}));
  if(page==='dashboard') add('universe',`/api/universe/page?page=${Pagers.universe.page}&page_size=${Pagers.universe.pageSize}`,'universeTable');
  if(page==='trades'){
    const p=Pagers.positions;
    add('positions',`/api/positions?page=${p.page}&page_size=${p.pageSize}`,'positionsTable');
    const o=Pagers.orders; add('orders',`/api/orders?limit=${o.pageSize}&offset=${(o.page-1)*o.pageSize}`,'ordersTable');
  }
  if(page==='events') add('events',`/api/events?page=${Pagers.events.page}&page_size=${Pagers.events.pageSize}`,'eventsLog');
  if(page==='performance') add('performance','/api/performance','agentPerfTable');
  await Promise.all(jobs);
  return results;
}

function mergeActivePageData(state, page, extra){
  if(!state || !extra) return state;
  if(page==='dashboard' && extra.universe && !extra.universe._error){
    state.universe=state.universe||{};
    state.universe.results=Array.isArray(extra.universe.items)?extra.universe.items:[];
    state.universe.total_candidates=Number(extra.universe.total||0);
    Pagers.universe.page=Number(extra.universe.page||Pagers.universe.page);
    Pagers.universe._serverMeta={page:Number(extra.universe.page||1),pages:Number(extra.universe.pages||1),total:Number(extra.universe.total||0),pageSize:Number(extra.universe.page_size||Pagers.universe.pageSize),start:extra.universe.total?((Number(extra.universe.page||1)-1)*Number(extra.universe.page_size||Pagers.universe.pageSize)+1):0,end:Math.min(Number(extra.universe.page||1)*Number(extra.universe.page_size||Pagers.universe.pageSize),Number(extra.universe.total||0))};
  }
  if(page==='trades'){
    if(extra.positions && !extra.positions._error){state.positions=extra.positions.positions||[];Pagers.positions.page=Number(extra.positions.page||Pagers.positions.page);Pagers.positions._meta={page:extra.positions.page||1,pages:extra.positions.pages||1,total:extra.positions.total||0,pageSize:extra.positions.page_size||Pagers.positions.pageSize,start:extra.positions.total?((extra.positions.page-1)*extra.positions.page_size+1):0,end:Math.min((extra.positions.page||1)*(extra.positions.page_size||Pagers.positions.pageSize),extra.positions.total||0)};}
    if(extra.orders && !extra.orders._error){state.orders=extra.orders.orders||[];const total=Number(extra.orders.total||0),limit=Number(extra.orders.limit||Pagers.orders.pageSize),pageNo=Math.floor(Number(extra.orders.offset||0)/limit)+1;Pagers.orders.page=pageNo;Pagers.orders._meta={page:pageNo,pages:Math.max(1,Math.ceil(total/limit)),total,pageSize:limit,start:total?Number(extra.orders.offset||0)+1:0,end:Math.min(Number(extra.orders.offset||0)+limit,total)};}
  }
  if(page==='events' && extra.events && !extra.events._error){state.events=extra.events.events||[];state.errors=extra.events.errors||[];Pagers.events.page=Number(extra.events.page||Pagers.events.page);Pagers.events._meta={page:extra.events.page||1,pages:extra.events.pages||1,total:extra.events.total||0,pageSize:extra.events.page_size||Pagers.events.pageSize,start:extra.events.total?((extra.events.page-1)*extra.events.page_size+1):0,end:Math.min((extra.events.page||1)*(extra.events.page_size||Pagers.events.pageSize),extra.events.total||0)};}
  if(page==='performance' && extra.performance && !extra.performance._error) state.performance=extra.performance;
  return state;
}

async function refresh(){
  try{
    // Cancel prior state poll, then fetch only the data needed by the active page in parallel.
    if(_stateAbort){ try{ _stateAbort.abort(); }catch(_){} }
    _stateAbort = new AbortController();
    const page=activePageId();
    const [baseState, pageData] = await Promise.all([
      get('/api/state', { signal: _stateAbort.signal, silent:true, retries:2 }),
      fetchActivePageData(page),
    ]);
    const s=mergeActivePageData(baseState,page,pageData);
    const pulse=JSON.stringify([s.system?.total_cycles,s.system?.status,s.market?.symbol,s.market?.price,s.decision?.decision,s.decision?.confidence,s.universe?.scan_progress?.completed_symbols,s.account?.equity]);
    window.__pollChanged=(pulse!==window.__lastPollFingerprint); window.__lastPollFingerprint=pulse;
    window.__lastState = s;
    const sys = s.system || {};
    const market = s.market || {};
    const decision = s.decision || {};
    const live = !!sys.live_execution_enabled;
    const mt5On = !!sys.mt5_connected;
    const demo = !!sys.demo_mode;
    const marketOpen = sys.market_open === true;
    const session = sys.session || {};

    // Connection banner
    setTxt('topConnection', mt5On ? 'MT5 CONNECTED' : (sys.status === 'starting' ? 'STARTING…' : 'MT5 OFFLINE'));
    setCls('topDot', 'dot ' + (mt5On ? 'good' : (demo ? 'warn' : 'bad')));
    setCls('sideDot', 'dot ' + (mt5On ? 'good' : (demo ? 'warn' : 'bad')));
    const connDetail = sys.connection_detail || sys.session?.reason || '';
    setTxt('sideState', mt5On ? 'MT5 LIVE' : (connDetail ? String(connDetail).slice(0, 48) : 'MT5 OFFLINE'));
    setTxt('modePill', sys.paper_trading ? 'PAPER' : (sys.live_ready ? 'LIVE ARMED' : 'LIVE LOCKED'));
    setTxt('sideMode', sys.paper_trading ? (demo ? 'PAPER / DEMO' : 'PAPER / MT5') : (sys.live_ready ? 'LIVE / ARMED' : 'LIVE / LOCKED'));
    // Surface connection detail in chart session reason for the offline banner
    if(!mt5On && connDetail) ChartState.sessionReason = connDetail;
    setTxt('footerState', sys.paper_trading ? (mt5On ? 'Paper · MT5 historical/live feed' : 'Paper · waiting for MT5') : 'Live execution configured');
    setTxt('cycleCounter', `CYCLES ${sys.total_cycles || 0}`);
    setTxt('errorCounter', `ERRORS ${sys.consecutive_errors || 0}`);
    const heartbeatDate = sys.heartbeat ? new Date(sys.heartbeat) : null;
    const heartbeatLocal = heartbeatDate ? heartbeatDate.toLocaleTimeString() : '—';
    const heartbeatUtc = heartbeatDate ? heartbeatDate.toISOString().slice(11, 19) : '—';
    setTxt('heartbeat', `HEARTBEAT ${heartbeatLocal} LOCAL · ${heartbeatUtc} UTC`);

    // Build / port badge
    const buildEl = $('buildBadge');
    if(buildEl) buildEl.textContent = `BUILD ${s.version || '4.39.0'} · PORT ${location.port || '8105'}`;
    setTxt('brandVersion', `v${s.version || '4.39.0'} QUANT FINANCE TERMINAL`);

    // Market header
    setTxt('symbol', market.symbol || '—');
    setTxt('price', fmt(market.price));
    setTxt('spread', `Spread ${fmt(market.spread)}`);
    if($('bidAsk')) setTxt('bidAsk', (market.bid!=null&&market.ask!=null)?`Bid ${fmt(market.bid)} · Ask ${fmt(market.ask)}`:'');
    setTxt('feedMode', market.realtime ? 'REALTIME MT5' : (sys.mt5_connected ? 'MT5' : 'OFFLINE'));
    setTxt('chartSymbolTitle', ChartState.symbol || market.symbol || '—');
    setTxt('decision', decision.decision || 'WAIT');
    setCls('decision', 'metric-main ' + (decision.decision === 'BUY' ? 'decision-buy' : decision.decision === 'SELL' ? 'decision-sell' : 'decision-wait'));
    setTxt('confidence', `Decision score ${pct(decision.confidence)} · not win probability`);
    setTxt('risk', s.risk ? (s.risk.approved ? 'APPROVED' : 'REJECTED') : 'NO PLAN');
    setCls('risk', 'metric-main ' + (s.risk?.approved ? 'decision-buy' : s.risk ? 'decision-sell' : 'decision-wait'));
    setTxt('riskReason', s.risk?.reason || decision.reasons?.[0] || 'Awaiting analysis');
    setTxt('updated', market.timestamp ? new Date(market.timestamp).toLocaleTimeString() : '—');
    setTxt('m5price', fmt(market.prices?.M5 ?? market.price));
    setTxt('m15price', fmt(market.prices?.M15));
    setTxt('h1price', fmt(market.prices?.H1));
    setTxt('chartSpread', fmt(market.spread));

    // Engine telemetry
    setTxt('activity', sys.activity || '—');
    setTxt('cycleLatency', `Last cycle ${fmt(sys.cycle_latency_ms)} ms`);
    setTxt('loopState', sys.paused ? 'PAUSED' : (sys.status === 'stopped' ? 'STOPPED' : 'RUNNING'));
    setTxt('dataState', !mt5On ? 'MT5 REQUIRED' : (!marketOpen ? 'MARKET CLOSED' : 'MT5 LIVE FEED'));
    setTxt('connectionDetail', sys.connection_detail || session.reason || '—');
    setTxt('sessionState', marketOpen ? 'OPEN' : 'CLOSED');
    setCls('sessionState', marketOpen ? '' : 'danger-text');
    ChartState.marketOpen = marketOpen;
    ChartState.mt5Connected = mt5On;
    ChartState.demo = false;
    ChartState.sessionReason = session.reason || sys.connection_detail || '';
    setTxt('execState', sys.paper_trading ? 'PAPER EXECUTION' : (sys.live_ready ? 'LIVE EXECUTION READY' : 'LIVE EXECUTION LOCKED'));
    setTxt('killState', sys.kill_switch ? 'ON' : 'OFF');
    setCls('killState', sys.kill_switch ? 'danger-text' : '');
    setTxt('uptime', `${sys.uptime_seconds || 0}s`);
    setTxt('continuousState', sys.continuous ? '24/7 LOOP' : 'STOPPED');
    setTxt('recoveryState', sys.consecutive_errors ? `RECOVERING (${sys.consecutive_errors})` : 'HEALTHY');
    if($('guardianState')) setTxt('guardianState', (sys.guardian?.status || 'UNKNOWN') + (sys.guardian?.restarts !== undefined ? ` / R${sys.guardian.restarts}` : ''));

    setTxt('equity', fmt(s.account?.equity));
    setTxt('pnl', fmt(s.account?.daily_pnl));
    setTxt('exposure', `${fmt(s.account?.exposure)}%`);
    setTxt('positions', fmt(s.account?.open_positions, 0));

    if(page==='dashboard') {
      setHtml('reasons', (decision.reasons || []).map(r => `<div class="reason">${esc(r)}</div>`).join('') || '<div class="reason">No directional consensus strong enough.</div>');
      setTxt('riskBig', s.risk ? (s.risk.approved ? 'APPROVED' : 'REJECTED') : 'NO PLAN');
      setCls('riskBig', s.risk?.approved ? 'risk-approved' : s.risk ? 'risk-rejected' : '');
      if($('riskDetails')) {
        $('riskDetails').innerHTML = s.risk
          ? `<div class="detail-row"><span>Reason</span><b>${esc(s.risk.reason)}</b></div><div class="detail-row"><span>Quantity</span><b>${fmt(s.risk.quantity)}</b></div><div class="detail-row"><span>Max loss</span><b>${fmt(s.risk.max_loss)}</b></div><div class="detail-row"><span>Exposure after</span><b>${fmt(s.risk.exposure_after)}%</b></div>`
          : '<div class="muted">No risk evaluation.</div>';
      }
      const p = decision.plan;
      if($('planGrid')) {
        $('planGrid').innerHTML = p
          ? `<div class="plan-cell"><span>Side</span><b>${esc(p.side)}</b></div><div class="plan-cell"><span>Entry</span><b>${fmt(p.entry)}</b></div><div class="plan-cell"><span>Stop</span><b>${fmt(p.stop)}</b></div><div class="plan-cell"><span>Target</span><b>${fmt(p.target)}</b></div><div class="plan-cell"><span>Confidence</span><b>${pct(p.confidence)}</b></div><div class="plan-cell"><span>Rationale</span><b>${esc((p.rationale||[]).join(' · '))}</b></div>`
          : '<div class="muted">No active plan.</div>';
      }
      setTxt('marketTimestamp', market.timestamp ? new Date(market.timestamp).toLocaleString() : '—');
    }
    setTxt('rightDecision', decision.decision || 'WAIT');
    setTxt('rightRisk', s.risk ? (s.risk.approved ? 'Risk approved' : 'Risk rejected') : 'Risk gate pending');
    setTxt('rightSession', sys.continuous ? 'CONTINUOUS' : 'STOPPED');
    setTxt('rightMode', sys.paper_trading ? 'PAPER' : 'LIVE');

    // Update only the active page's heavy components. Hidden pages remain untouched until opened.
    window.__lastState = s;
    try { renderWatchlist(s); } catch(e) { console.warn('watchlist', e); }
    if(page==='settings'){
      setTxt('settingsModeTag', sys.paper_trading ? 'PAPER DEFAULT' : (sys.live_ready ? 'LIVE ARMED' : 'LIVE LOCKED'));
      try { await ensureSymbolCatalog(); populateSymbolSelect(s); } catch(e) { console.warn('symbols',e); }
      try { if(!window.__settingsDirty) syncSettingsForm(s.settings||{}); } catch(e) { console.warn('settings form',e); }
      if($('setMode')) $('setMode').value = sys.paper_trading ? 'PAPER' : 'LIVE';
      if($('setInterval') && document.activeElement!==$('setInterval')) $('setInterval').value=s.settings?.interval||sys.interval||5;
      if($('setLookback') && document.activeElement!==$('setLookback')) $('setLookback').value=s.settings?.lookback||300;
      refreshProviderDiagnostics().catch(()=>{});
    }
    if(page==='dashboard'){
      try { renderMTF(s); } catch(e) { console.warn('mtf',e); }
      try { renderStructure(market.structure||{}); } catch(e) { console.warn('structure',e); }
      try { renderRiskDashboard(s); } catch(e) { console.warn('risk',e); }
      try { renderTrades(s); } catch(e) { console.warn('trades',e); }
      try { renderUniverse(s); } catch(e) { console.warn('universe',e); }
      try { renderMarketDesks(s); } catch(e) { console.warn('market desks',e); }
      try { draw(s.history||[],s); } catch(e) { console.warn('chart',e); }
    } else if(page==='agents'){
      try { renderAgents(s.agents||[],s.agent_status||{},s.ai_mesh||{}); } catch(e) { console.warn('agents',e); }
    } else if(page==='mesh'){
      try { renderMesh(s); } catch(e) { console.warn('mesh',e); }
    } else if(page==='market'){
      try { renderMarketDesks(s); } catch(e) { console.warn('market desks',e); }
      if(!_instrumentRegistryLoaded) refreshInstrumentRegistry(mt5On).catch(()=>{});
    } else if(page==='risk'){
      try { renderRiskDashboard(s); } catch(e) { console.warn('risk',e); }
      try { renderTrades(s); } catch(e) { console.warn('trades',e); }
    } else if(page==='trades'){
      try { renderPositions(s); } catch(e) { console.warn('positions',e); }
      try { renderOrders(s); } catch(e) { console.warn('orders',e); }
      try { renderLifecycle(s); } catch(e) { console.warn('lifecycle',e); }
      renderPager('positionsPager','positions',()=>_safeRefresh());
      renderPager('ordersPager','orders',()=>_safeRefresh());
    } else if(page==='management'){
      try { renderManagement(s); } catch(e) { console.warn('management',e); }
      try { renderLifecycle(s); } catch(e) { console.warn('lifecycle',e); }
    } else if(page==='performance'){
      try { renderPerformance(s); } catch(e) { console.warn('performance',e); }
    } else if(page==='research'){
      try { renderResearch(s); } catch(e) { console.warn('research',e); }
    } else if(page==='shadow'){
      try { renderShadow(s); } catch(e) { console.warn('shadow',e); }
    } else if(page==='runbooks'){
      try { renderRunbooks(s); } catch(e) { console.warn('runbooks',e); }
    } else if(page==='strategies'){
      try { renderStrategies(s); } catch(e) { console.warn('strategies',e); }
    } else if(page==='events'){
      try { renderLogs(s); } catch(e) { console.warn('logs',e); }
      renderPager('eventsPager','events',()=>_safeRefresh());
    }

    const errCount = (s.errors || []).length;
    setTxt('errorCount', errCount > 0 ? String(errCount) : '');
    const errBadge = $('errorCount');
    if(errBadge){ errBadge.style.display = errCount > 0 ? '' : 'none'; }
    setTxt('faultBadge', errCount > 0 ? String(errCount) : '');
    setTxt('faultCount', errCount);
    setTxt('executionLock', live ? (sys.live_ready ? 'LIVE EXECUTION READY' : 'LIVE EXECUTION LOCKED') : 'PAPER / LIVE OFF');
    if($('liveStatusBox')) {
      $('liveStatusBox').innerHTML = live
        ? `<b>${sys.live_ready ? 'LIVE EXECUTION ARMED' : 'LIVE EXECUTION CONFIGURED BUT LOCKED'}</b><p>${sys.live_ready ? 'Orders may be submitted after the hard risk gate.' : 'Arm live and release kill switch after validation.'}</p>`
        : `<b>PAPER EXECUTION</b><p>${mt5On ? 'MT5 feed active in paper mode (real historical/live rates only).' : 'Start MetaTrader 5 and log in. This system does not use synthetic demo prices.'}</p>`;
    }

  } catch(e) {
    console.error('refresh failed', e);
    // Soft failure: keep last painted state so a busy analysis cycle does not look like a full relaunch
    const msg = (e && e.name === 'AbortError') ? 'REFRESH TIMEOUT (analysis busy)' : ('API ERROR: ' + (e.message || 'fetch failed'));
    setTxt('activity', msg);
    setTxt('footerState', msg);
    if(!(e && e.name === 'AbortError')){
      setTxt('topConnection', 'RETRYING');
      setCls('topDot', 'dot warn');
      setCls('sideDot', 'dot warn');
      setTxt('sideState', 'RETRYING');
    }
  }
}


function applySettingsForm(s){
  const sys = s.system || {};
  const st = s.settings || {};
  const risk = st.risk || sys.risk || {};
  const exit = st.exit_management || s.management || {};
  const fill = (id, val) => {
    const el = $(id);
    if(!el || document.activeElement === el) return;
    if(val == null || val === '') return;
    if(el.type === 'checkbox') el.checked = !!val;
    else el.value = val;
  };
  const mode = st.mode || (sys.paper_trading ? 'PAPER' : 'LIVE');
  fill('setTradingMode', mode);
  fill('setLiveExecutionEnabled', st.live_execution_enabled ?? sys.live_trading_enabled ?? sys.live_execution_enabled);
  fill('setLiveArmed', st.live_armed ?? sys.live_armed);
  // kill switch: checked means ON
  const killEl = $('setKillSwitch');
  if(killEl && document.activeElement !== killEl){
    const k = st.kill_switch ?? sys.kill_switch;
    killEl.checked = k === true || k === 'ON' || k === 1;
  }
  const badge = $('setModeBadge');
  if(badge){
    badge.textContent = mode + ((st.live_armed ?? sys.live_armed) ? ' · ARMED' : '') + ((st.kill_switch ?? sys.kill_switch) ? ' · KILL' : '');
    badge.className = 'status-pill ' + (mode === 'LIVE' ? 'warn' : 'good');
  }
  fill('setInterval', st.interval ?? sys.interval);
  fill('setLookback', st.lookback ?? 300);
  fill('setTimeframe', st.timeframe || 'M5');
  fill('setWatchlist', Array.isArray(st.symbols) ? st.symbols.join(', ') : (sys.watchlist || ''));
  fill('setAnalyzeAllSymbols', st.analyze_all_symbols_enabled ?? sys.analyze_all_symbols_enabled);
  fill('setLocalAiEnabled', st.local_ai_enabled ?? false);
  if($('setBtSymbol')) $('setBtSymbol').value = st.bt_symbol || st.symbol || $('setBtSymbol').value || 'XAUUSD';
  if($('setBtStrategy') && st.bt_strategy) $('setBtStrategy').value = st.bt_strategy;
  if($('setBtBars') && st.bt_bars) $('setBtBars').value = st.bt_bars;
  if($('setBtEquity') && st.bt_equity) $('setBtEquity').value = st.bt_equity;
  if($('setBtRisk') && st.bt_risk_pct!=null) $('setBtRisk').value = st.bt_risk_pct;
  if($('setBtExecutionModel') && st.bt_execution_model) $('setBtExecutionModel').value = st.bt_execution_model;
  if($('setBtObjective') && st.bt_objective) $('setBtObjective').value = st.bt_objective;
  if($('setBtTrials') && st.bt_max_trials) $('setBtTrials').value = st.bt_max_trials;
  if($('setBtFolds') && st.bt_folds) $('setBtFolds').value = st.bt_folds;
  if($('setBtIsRatio') && st.bt_is_ratio) $('setBtIsRatio').value = st.bt_is_ratio;
  if($('setBtCommission') && st.bt_commission_bps!=null) $('setBtCommission').value = st.bt_commission_bps;

  fill('setLocalAiTiered', st.local_ai_tiered ?? true);
  fill('setLocalAiInfluence', st.local_ai_can_influence_decision ?? false);
  if($('setLocalAiBaseUrl') && document.activeElement !== $('setLocalAiBaseUrl'))
    $('setLocalAiBaseUrl').value = st.local_ai_base_url || 'http://127.0.0.1:11434/v1';
  if($('setLocalAiGateModel') && document.activeElement !== $('setLocalAiGateModel'))
    $('setLocalAiGateModel').value = st.local_ai_gate_model || 'qwen2.5:1.5b';
  if($('setLocalAiDeepModel') && document.activeElement !== $('setLocalAiDeepModel'))
    $('setLocalAiDeepModel').value = st.local_ai_deep_model || 'qwen2.5:3b';
  fill('setAutoTrade', st.auto_trade ?? true);
  fill('setTradeUniverseEnabled', st.trade_universe_enabled ?? true);
  if($('setTradeUniverseMaxPerCycle') && document.activeElement !== $('setTradeUniverseMaxPerCycle'))
    $('setTradeUniverseMaxPerCycle').value = st.trade_universe_max_per_cycle ?? 15;
  if($('setMaxPositionsHint') && $('setMaxPositions'))
    $('setMaxPositionsHint').value = $('setMaxPositions').value;

  fill('setSymbolScanLimit', st.all_symbol_scan_limit);
  fill('setSymbolScanBatchSize', st.all_symbol_scan_batch_size);
  fill('setSymbolRescanSeconds', st.all_symbol_rescan_seconds);
  fill('setConsensusThreshold', st.debate_consensus_threshold ?? 0.55);
  fill('setStrongOverride', st.strong_research_override ?? 0.72);
  fill('setAiInterval', st.ai_analysis_interval_seconds ?? 60);
  fill('setTradeCooldown', st.trade_cooldown_seconds);
  fill('setDeviation', st.deviation);
  fill('setLifecycleReconcile', st.lifecycle_reconcile_seconds);
  fill('setOnePositionPerSymbol', st.one_position_per_symbol !== false);
  fill('setRiskFraction', st.risk_fraction);
  fill('setMaxDailyLossPct', risk.max_daily_loss_pct);
  fill('setMaxPositionRiskPct', risk.max_position_risk_pct);
  fill('setMaxExposurePct', risk.max_account_exposure_pct);
  fill('setMinRewardRisk', risk.min_reward_risk);
  fill('setMinDecisionConfidence', risk.min_decision_confidence);
  fill('setMaxSpread', risk.max_spread);
  fill('setMaxVolatility', risk.max_volatility);
  fill('setMaxPositions', risk.max_positions);
  // desks
  const desks = new Set((st.market_desks_enabled || []).map(x => String(x).toLowerCase()));
  if(desks.size){
    [['setDeskForex','forex'],['setDeskStocks','stocks'],['setDeskCrypto','crypto'],['setDeskCfd','cfd'],['setDeskConvert','convert'],['setDeskIndices','indices']].forEach(([id,d]) => {
      const el = $(id); if(el && document.activeElement !== el) el.checked = desks.has(d);
    });
  }
  // exit
  if(exit && typeof exit === 'object'){
    fill('setExitManagerEnabled', exit.enabled !== false);
    fill('setBreakEvenEnabled', exit.break_even_enabled);
    fill('setBreakEvenR', exit.break_even_r);
    fill('setBreakEvenOffsetR', exit.break_even_offset_r);
    fill('setTrailingEnabled', exit.trailing_enabled);
    fill('setTrailingStartR', exit.trailing_start_r);
    fill('setTrailingAtrMult', exit.trailing_atr_mult);
    fill('setTrailingStructure', exit.trailing_use_structure);
    fill('setPartialEnabled', exit.partial_exit_enabled);
    fill('setPartialR', exit.partial_exit_r);
    fill('setPartialPercent', exit.partial_exit_percent);
    fill('setSecondPartialEnabled', exit.second_partial_exit_enabled);
    fill('setSecondPartialR', exit.second_partial_exit_r);
    fill('setSecondPartialPercent', exit.second_partial_exit_percent);
    fill('setPeakGivebackEnabled', exit.peak_giveback_enabled);
    fill('setPeakGivebackTriggerR', exit.peak_giveback_trigger_r);
    fill('setPeakGivebackR', exit.peak_giveback_r);
    fill('setTimeStopEnabled', exit.time_stop_enabled);
    fill('setMaxHold', exit.max_hold_minutes);
    fill('setTimeProgressR', exit.time_stop_min_progress_r);
    fill('setCloseUnprotected', exit.close_if_unprotected);
    fill('setAIInvalidation', exit.ai_invalidation_enabled);
    fill('setAIInvalidationConfidence', exit.ai_invalidation_confidence);
    fill('setAIInvalidationCycles', exit.ai_invalidation_cycles);
  }
}


async function saveSettings(){
  const statusEl = $('settingsSaveStatus');
  if(statusEl) statusEl.textContent = 'Saving…';
  const num = (id, d=0) => {
    const el = $(id);
    if(!el || el.value === '') return d;
    const v = Number(el.value);
    return Number.isFinite(v) ? v : d;
  };
  const check = (id) => !!($(id) && $(id).checked);
  const sym = ($('setSymbol') && $('setSymbol').value) || '';
  const wlRaw = ($('setWatchlist') && $('setWatchlist').value) || '';
  const wl = wlRaw.split(/[,;\s]+/).map(s => s.trim().toUpperCase()).filter(Boolean);
  const payload = {
    symbol: sym,
    symbols: wl.length ? wl : undefined,
    interval: num('setInterval', 5),
    lookback: num('setLookback', 300),
    timeframe: ($('setTimeframe') && $('setTimeframe').value) || 'M5',
    mode: ($('setTradingMode') && $('setTradingMode').value) || 'PAPER',
    paper_trading: (($('setTradingMode') && $('setTradingMode').value) || 'PAPER') === 'PAPER',
    live_trading_enabled: check('setLiveExecutionEnabled'),
    live_armed: check('setLiveArmed'),
    kill_switch: check('setKillSwitch'),
    analyze_all_symbols_enabled: check('setAnalyzeAllSymbols'),
    local_ai_enabled: check('setLocalAiEnabled'),
    local_ai_tiered: check('setLocalAiTiered'),
    local_ai_can_influence_decision: check('setLocalAiInfluence'),
    local_ai_base_url: ($('setLocalAiBaseUrl') && $('setLocalAiBaseUrl').value) || 'http://127.0.0.1:11434/v1',
    local_ai_gate_model: ($('setLocalAiGateModel') && $('setLocalAiGateModel').value) || 'qwen2.5:1.5b',
    local_ai_deep_model: ($('setLocalAiDeepModel') && $('setLocalAiDeepModel').value) || 'qwen2.5:3b',
    auto_trade: check('setAutoTrade'),
    trade_universe_enabled: check('setTradeUniverseEnabled'),
    trade_universe_max_per_cycle: num('setTradeUniverseMaxPerCycle', 15),
    bt_symbol: ($('setBtSymbol')&&$('setBtSymbol').value)||'XAUUSD',
    bt_strategy: ($('setBtStrategy')&&$('setBtStrategy').value)||'trend_following',
    bt_bars: num('setBtBars', 3000),
    bt_equity: num('setBtEquity', 10000),
    bt_risk_pct: num('setBtRisk', 0.5),
    bt_execution_model: ($('setBtExecutionModel')&&$('setBtExecutionModel').value)||'ohlc',
    bt_objective: ($('setBtObjective')&&$('setBtObjective').value)||'composite',
    bt_max_trials: num('setBtTrials', 40),
    bt_folds: num('setBtFolds', 4),
    bt_is_ratio: num('setBtIsRatio', 0.7),
    bt_commission_bps: num('setBtCommission', 0),


    all_symbol_scan_limit: num('setSymbolScanLimit', 0),
    all_symbol_scan_batch_size: num('setSymbolScanBatchSize', 25),
    all_symbol_rescan_seconds: num('setSymbolRescanSeconds', 300),
    market_desks_enabled: (function(){
      const desks = [
        ['forex','setDeskForex'],['stocks','setDeskStocks'],['crypto','setDeskCrypto'],
        ['cfd','setDeskCfd'],['convert','setDeskConvert'],['indices','setDeskIndices']
      ].filter(([,id]) => !!($(id) && $(id).checked)).map(([desk]) => desk);
      return desks.length ? desks : undefined;
    })(),
    debate_consensus_threshold: num('setConsensusThreshold', 0.48),
    strong_research_override: num('setStrongOverride', 0.72),
    ai_analysis_interval_seconds: num('setAiInterval', 60),
    trade_cooldown_seconds: num('setTradeCooldown', 300),
    deviation: num('setDeviation', 20),
    lifecycle_reconcile_seconds: num('setLifecycleReconcile', 5),
    one_position_per_symbol: check('setOnePositionPerSymbol'),
    risk_fraction: num('setRiskFraction', 0.005),
    max_daily_loss_pct: num('setMaxDailyLossPct', 1.5),
    max_position_risk_pct: num('setMaxPositionRiskPct', 0.4),
    max_account_exposure_pct: num('setMaxExposurePct', 8),
    min_reward_risk: num('setMinRewardRisk', 1.5),
    min_decision_confidence: num('setMinDecisionConfidence', 0.48),
    max_spread: num('setMaxSpread', 4),
    max_volatility: num('setMaxVolatility', 0),
    max_positions: num('setMaxPositions', 15),
    risk: {
      max_daily_loss_pct: num('setMaxDailyLossPct', 1.5),
      max_position_risk_pct: num('setMaxPositionRiskPct', 0.4),
      max_account_exposure_pct: num('setMaxExposurePct', 8),
      min_reward_risk: num('setMinRewardRisk', 1.5),
      min_decision_confidence: num('setMinDecisionConfidence', 0.48),
      max_spread: num('setMaxSpread', 4),
      max_volatility: num('setMaxVolatility', 0),
      max_positions: num('setMaxPositions', 15),
    },
    // exit manager fields (same payload)
    position_manager_enabled: check('setExitManagerEnabled'),
    break_even_enabled: check('setBreakEvenEnabled'),
    break_even_r: num('setBreakEvenR', 1),
    break_even_offset_r: num('setBreakEvenOffsetR', 0.05),
    trailing_enabled: check('setTrailingEnabled'),
    trailing_start_r: num('setTrailingStartR', 1.5),
    trailing_atr_mult: num('setTrailingAtrMult', 1.6),
    trailing_use_structure: check('setTrailingStructure'),
    partial_exit_enabled: check('setPartialEnabled'),
    partial_exit_r: num('setPartialR', 1),
    partial_exit_percent: num('setPartialPercent', 40),
    second_partial_exit_enabled: check('setSecondPartialEnabled'),
    second_partial_exit_r: num('setSecondPartialR', 2),
    second_partial_exit_percent: num('setSecondPartialPercent', 30),
    peak_giveback_enabled: check('setPeakGivebackEnabled'),
    peak_giveback_trigger_r: num('setPeakGivebackTriggerR', 2),
    peak_giveback_r: num('setPeakGivebackR', 0.9),
    time_stop_enabled: check('setTimeStopEnabled'),
    max_hold_minutes: num('setMaxHold', 240),
    time_stop_min_progress_r: num('setTimeProgressR', 0.25),
    close_if_unprotected: check('setCloseUnprotected'),
    ai_invalidation_enabled: check('setAIInvalidation'),
    ai_invalidation_confidence: num('setAIInvalidationConfidence', 0.85),
    ai_invalidation_cycles: num('setAIInvalidationCycles', 2),
  };
  return post('/api/settings', payload).then(async () => {
    window.__settingsDirty = false;
    if(statusEl) statusEl.textContent = 'Saved. Applied to runtime and disk.';
    toast('SETTINGS SAVED');
    await refresh();
    try { await loadSettingsPage(); } catch(_){}
  }).catch(e => {
    if(statusEl) statusEl.textContent = 'Error: ' + (e.message || 'save failed');
    toast('ERROR: ' + (e.message || 'save failed'));
  });
}
async function saveExitManagementSettings(){
  const num=id=>Number($(id)?.value);
  const check=id=>!!$(id)?.checked;
  const payload={
    position_manager_enabled:check('setExitManagerEnabled'),
    break_even_enabled:check('setBreakEvenEnabled'),break_even_r:num('setBreakEvenR'),break_even_offset_r:num('setBreakEvenOffsetR'),
    trailing_enabled:check('setTrailingEnabled'),trailing_start_r:num('setTrailingStartR'),trailing_atr_mult:num('setTrailingAtrMult'),trailing_use_structure:check('setTrailingStructure'),
    partial_exit_enabled:check('setPartialEnabled'),partial_exit_r:num('setPartialR'),partial_exit_percent:num('setPartialPercent'),
    second_partial_exit_enabled:check('setSecondPartialEnabled'),second_partial_exit_r:num('setSecondPartialR'),second_partial_exit_percent:num('setSecondPartialPercent'),
    peak_giveback_enabled:check('setPeakGivebackEnabled'),peak_giveback_trigger_r:num('setPeakGivebackTriggerR'),peak_giveback_r:num('setPeakGivebackR'),
    time_stop_enabled:check('setTimeStopEnabled'),max_hold_minutes:num('setMaxHold'),time_stop_min_progress_r:num('setTimeProgressR'),
    close_if_unprotected:check('setCloseUnprotected'),ai_invalidation_enabled:check('setAIInvalidation'),
    ai_invalidation_confidence:num('setAIInvalidationConfidence'),ai_invalidation_cycles:num('setAIInvalidationCycles')
  };
  try{
    await post('/api/settings',payload);
    if($('exitSaveMsg'))$('exitSaveMsg').textContent='Exit policy saved. Live actions still require a fresh, timestamp-verified quote.';
    toast('EXIT POLICY SAVED'); await refresh();
  }catch(e){if($('exitSaveMsg'))$('exitSaveMsg').textContent=e.message;toast(`ERROR: ${e.message}`);}
}
let _refreshBusy = false;
let _pollTimer = null;
function scheduleAdaptivePoll(delay){
  if(_pollTimer) clearTimeout(_pollTimer);
  _pollTimer=null;
  if(document.hidden) return;
  const base=Math.max(3500,Math.min(5000,Number(delay)||5000));
  const actual=Math.round(base*(0.96+Math.random()*0.08));
  _pollTimer=setTimeout(()=>{_pollTimer=null;_safeRefresh();},actual);
}
async function _safeRefresh(){
  if(document.hidden) return;
  if(_refreshBusy){scheduleAdaptivePoll(4000);return;}
  _refreshBusy = true;
  let changed=false;
  try { await refresh(); changed=!!window.__pollChanged; updatePerfBadge(); }
  catch(e){ console.error(e); }
  finally { _refreshBusy = false; scheduleAdaptivePoll(changed?3500:5000); }
}
// Background refreshes are silent; active-page payloads are fetched in parallel.
document.addEventListener('visibilitychange', ()=>{
  if(document.hidden){if(_pollTimer)clearTimeout(_pollTimer);_pollTimer=null;}
  else _safeRefresh();
});
_safeRefresh();

// v2.8.0 terminal layout aliases: presentation-only fields mirror authoritative runtime values.
(function(){function mirror(){[['m5price','chartM5'],['m15price','chartM15'],['h1price','chartH1'],['decision','rightDecision'],['riskReason','rightRisk']].forEach(function(pair){var a=document.getElementById(pair[0]),b=document.getElementById(pair[1]);if(a&&b&&b.textContent!==a.textContent)b.textContent=a.textContent;});}setInterval(mirror,1500);document.addEventListener('DOMContentLoaded',mirror);})();
(function(){function mirror(){[['ribbonSession','rightSession'],['ribbonMode','rightMode']].forEach(function(pair){var a=document.getElementById(pair[0]),b=document.getElementById(pair[1]);if(a&&b&&b.textContent!==a.textContent)b.textContent=a.textContent;});}setInterval(mirror,1500);document.addEventListener('DOMContentLoaded',mirror);})();

(function symbolSearchKeybind(){
  function bind(){
    const input = $('symbolSearch');
    if(!input || input._bound) return;
    input._bound = true;
    input.addEventListener('keydown', (e)=>{ if(e.key === 'Enter'){ e.preventDefault(); applySymbolSearch(); }});
  }
  if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bind);
  else bind();
})();


// v4.9.0 explicit global bindings for inline controls (safe for static-file hosting).
window.saveSettings = saveSettings; window.saveExitManagementSettings = saveExitManagementSettings; window.setChartTf = setChartTf; window.toggleIndicator = toggleIndicator; window.applySymbolSearch = applySymbolSearch;
window.onChartSymbolInput = onChartSymbolInput; window.onChartSymbolKey = onChartSymbolKey; window.pickChartSymbol = pickChartSymbol; window.switchSymbol = switchSymbol;



/* ── Dark / Light theme (localStorage) ───────────────────────── */
const THEME_KEY = 'att_theme';

function getPreferredTheme(){
  try {
    const saved = localStorage.getItem(THEME_KEY);
    if(saved === 'light' || saved === 'dark') return saved;
  } catch(_){}
  try {
    if(window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches) return 'light';
  } catch(_){}
  return 'dark';
}

let themeSwitchTimers = [];
function applyTheme(theme, { animate = true } = {}){
  const mode = theme === 'light' ? 'light' : 'dark';
  const root = document.documentElement;
  const btn = $('themeToggle');
  const chartWrap = document.querySelector('.chart-wrap');

  // Cancel an in-flight transition before starting a new one (rapid toggles stay deterministic).
  themeSwitchTimers.forEach(timer => clearTimeout(timer));
  themeSwitchTimers = [];

  // A short accent sweep starts at the toggle; colour surfaces settle in ~260ms.
  if(animate){
    if(btn){
      const rect = btn.getBoundingClientRect();
      root.style.setProperty('--att-theme-x', `${Math.round(rect.left + rect.width / 2)}px`);
      root.style.setProperty('--att-theme-y', `${Math.round(rect.top + rect.height / 2)}px`);
      btn.classList.add('is-switching');
    }
    root.classList.remove('theme-wipe');
    root.classList.add('theme-animating');
    void root.offsetWidth;
    root.classList.add('theme-wipe');
    if(chartWrap){ chartWrap.classList.add('theme-fade'); chartWrap.classList.add('is-fading'); }
  }

  document.body.classList.remove('theme-light', 'theme-dark');
  document.body.classList.add(mode === 'light' ? 'theme-light' : 'theme-dark');
  root.dataset.theme = mode;
  try { localStorage.setItem(THEME_KEY, mode); } catch(_){}

  const icon = $('themeIcon');
  const label = $('themeLabel');
  if(icon) icon.textContent = mode === 'light' ? '☾' : '☀';
  if(label) label.textContent = mode === 'light' ? 'Dark' : 'Light';
  if(btn) btn.setAttribute('aria-label', mode === 'light' ? 'Switch to dark mode' : 'Switch to light mode');

  // Redraw chart mid-transition so colors match the new theme
  const redraw = () => {
    try {
      if(typeof drawCandles === 'function' && ChartState && ChartState.candles && ChartState.candles.length)
        drawCandles();
    } catch(_){}
    if(chartWrap){ chartWrap.classList.remove('theme-fade'); chartWrap.classList.remove('is-fading'); chartWrap.classList.add('is-ready'); }
  };
  if(animate){
    themeSwitchTimers.push(setTimeout(redraw, 90));
    themeSwitchTimers.push(setTimeout(() => {
      root.classList.remove('theme-animating', 'theme-wipe');
      if(btn) btn.classList.remove('is-switching');
    }, 350));
  } else {
    root.classList.remove('theme-animating', 'theme-wipe');
    redraw();
  }
}

function toggleTheme(){
  const current = document.body.classList.contains('theme-light') ? 'light' : 'dark';
  applyTheme(current === 'light' ? 'dark' : 'light');
}

function initTheme(){
  applyTheme(getPreferredTheme(), { animate: false });
  try {
    if(window.matchMedia){
      window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', (e) => {
        // Only auto-follow system if user has never set a preference
        try {
          if(!localStorage.getItem(THEME_KEY)) applyTheme(e.matches ? 'light' : 'dark');
        } catch(_){}
      });
    }
  } catch(_){}
}

window.toggleTheme = toggleTheme;
window.applyTheme = applyTheme;

// Apply as early as this script runs
if(document.body) initTheme();
else document.addEventListener('DOMContentLoaded', initTheme);


// Mobile nav toggle
(function(){
  const toggle = document.getElementById('navToggle');
  const backdrop = document.getElementById('navBackdrop');
  if(toggle) toggle.addEventListener('click', () => document.body.classList.toggle('nav-open'));
  if(backdrop) backdrop.addEventListener('click', () => document.body.classList.remove('nav-open'));
})();


async function loadSettingsPage(){
  try {
    const st = await get('/api/settings', { force: true });
    applySettingsForm({ settings: st, system: { paper_trading: st.mode === 'PAPER', kill_switch: st.kill_switch, live_armed: st.live_armed, live_trading_enabled: st.live_execution_enabled } });
  } catch(e) { console.warn('loadSettingsPage', e); }
}
// Hook page switches
document.querySelectorAll('.nav-item[data-page]').forEach(btn => {
  btn.addEventListener('click', () => {
    window.__settingsPageActive = (btn.dataset.page === 'settings');
    if(btn.dataset.page === 'settings'){
      window.__settingsDirty = false;
      loadSettingsPage();
    }
  });
});
document.addEventListener('input', (ev) => {
  const t = ev.target;
  if(!t || !t.id) return;
  if(String(t.id).startsWith('set')) window.__settingsDirty = true;
});
document.addEventListener('change', (ev) => {
  const t = ev.target;
  if(!t || !t.id) return;
  if(String(t.id).startsWith('set')) window.__settingsDirty = true;
});


(function bindModeControls(){
  const mode = () => $('setTradingMode');
  const liveEn = () => $('setLiveExecutionEnabled');
  const armed = () => $('setLiveArmed');
  const kill = () => $('setKillSwitch');
  document.addEventListener('change', (ev) => {
    const t = ev.target;
    if(!t || !t.id) return;
    if(t.id === 'setTradingMode'){
      if(t.value === 'LIVE' && liveEn()) liveEn().checked = true;
      if(t.value === 'PAPER' && armed()) armed().checked = false;
    }
    if(t.id === 'setKillSwitch' && t.checked && armed()) armed().checked = false;
    if(t.id === 'setLiveArmed' && t.checked){
      if(kill()) kill().checked = false;
      if(liveEn()) liveEn().checked = true;
      if(mode()) mode().value = 'LIVE';
    }
  });
})();








/* ── News Hub: unified in-app newsroom + broadcaster TV shell ───────────── */
const NewsHubState = { items: [], category: 'all', query: '', impactFilter: 'all', assetFilter: 'all', displayCount: 15, status: null, intelligence: null, impactAnalysis: null, instrumentAnalysis: null, loaded: false, autoRefreshRequested: false };
const NEWS_TV_STATIONS = {
  aljazeera: { name: 'Al Jazeera English', region: 'Global · English', channel: 'UCNye-wNBqNL5ZzHSJj3l8Bg', official: 'https://www.aljazeera.com/video/live/', mark: 'AJ' },
  abc: { name: 'ABC News Live', region: 'United States · English', channel: 'UCBi2mrWuNuyYy4gbM6fU18Q', official: 'https://abcnews.go.com/Live', mark: 'ABC' },
  cbs: { name: 'CBS News 24/7', region: 'United States · English', channel: 'UC8p1vwvWtl6T73JiExfWs1g', official: 'https://www.cbsnews.com/live/', mark: 'CBS' },
  france24: { name: 'France 24 English', region: 'International · English', channel: 'UCQfwfsi5VrQ8yKZ-UWmAEFg', official: 'https://www.france24.com/en/live', mark: 'F24' },
  // Channel identity verified against Sky News' official channel feed. Some times the
  // channel has no embeddable continuous stream, so the official live page is a fallback.
  sky: { name: 'Sky News', region: 'United Kingdom · English', channel: 'UCoMdktPbSTixAyNGwb-UYkQ', official: 'https://news.sky.com/watch-live?varget=target', mark: 'SKY' },
  dw: { name: 'DW News', region: 'International · English', channel: 'UCknLrEdhRCp1aegoMqRaCZg', official: 'https://www.dw.com/en/live-tv/s-100825', mark: 'DW' }
};
let currentNewsStationId = 'aljazeera';
let newsTvPlayerMode = 'embedded';
let newsTvLoadTimer = null;
function safeNewsPublisherUrl(value){
  try{const u=new URL(String(value||''));return u.protocol==='https:'?u.href:'';}catch(_){return '';}
}
function _setNewsTvStatus(message){
  setTxt('newsTvStatus',message);
  setTxt('newsTvStatusCompact',message);
}
function _newsTvEmbedUrl(station){
  const query=new URLSearchParams({autoplay:'0',controls:'1',modestbranding:'1',rel:'0',playsinline:'1'});
  return `https://www.youtube-nocookie.com/embed/live_stream?channel=${encodeURIComponent(station.channel)}&${query.toString()}`;
}
function _applyNewsTvMode(id, mode){
  const station=NEWS_TV_STATIONS[id];
  if(!station)return;
  currentNewsStationId=id; newsTvPlayerMode=mode==='official'?'official':'embedded';
  const frame=$('newsTvPlayer');
  const officialLink=$('newsTvOfficialLink');
  const title=$('newsTvTitle');
  const screenStation=$('newsTvScreenStation');
  const modeBtn=$('newsTvModeBtn');
  const mark=station.mark||'TV';
  if(frame){
    // Only reviewed static HTTPS URLs are used. Broadcaster players remain responsible for
    // playback availability; we never proxy or re-host broadcast video.
    frame.src=newsTvPlayerMode==='official'?station.official:_newsTvEmbedUrl(station);
    frame.title=`${station.name} news broadcast`;
  }
  if(title)title.textContent=`${station.name} · ${newsTvPlayerMode==='official'?'Official player':'In-app player'}`;
  if(screenStation)screenStation.textContent=`${mark}  /  ${station.name.toUpperCase()}`;
  if(officialLink){officialLink.href=station.official;officialLink.textContent='Open station ↗';officialLink.target='_blank';officialLink.rel='noopener noreferrer';}
  if(modeBtn)modeBtn.textContent=newsTvPlayerMode==='official'?'Use in-app player':'Try broadcaster player';
  setTxt('newsTvTicker',`${station.name}  •  ${station.region}  •  Official feed`);
  _setNewsTvStatus(newsTvPlayerMode==='official'
    ? `Trying ${station.name}'s official player inside the TV frame. If the broadcaster prevents embedding, use Open station.`
    : `Tuning ${station.name} in the in-app TV player. If the stream is unavailable in your region or no live event is embeddable, try the broadcaster player.`);
  if(newsTvLoadTimer)clearTimeout(newsTvLoadTimer);
  newsTvLoadTimer=setTimeout(()=>{
    if(currentNewsStationId===id){
      _setNewsTvStatus(newsTvPlayerMode==='official'
        ? `If the official player stays blank, the broadcaster may block embedding. Use Open station to watch via its own site.`
        : `If no picture appears, the channel may not expose a live embed right now. Try the broadcaster player or Open station.`);
    }
  },9000);
  document.querySelectorAll('[data-news-station]').forEach(btn=>{
    const active=btn.dataset.newsStation===id;
    btn.classList.toggle('active',active);
    btn.setAttribute('aria-pressed',active?'true':'false');
  });
  try{localStorage.setItem('att_news_tv_station',id);}catch(_){}
}
function selectNewsStation(id){
  if(!NEWS_TV_STATIONS[String(id||'')])return;
  _applyNewsTvMode(String(id),'embedded');
}
function toggleNewsPlayerMode(){_applyNewsTvMode(currentNewsStationId,newsTvPlayerMode==='embedded'?'official':'embedded');}
function newsDateLabel(value){
  if(!value)return 'Publication time unavailable';
  const d=new Date(value);
  if(Number.isNaN(d.getTime()))return 'Publication time unavailable';
  return d.toLocaleString();
}
function _newsCategoryLabel(value){return String(value||'world').replaceAll('_',' ').replace(/\b\w/g,ch=>ch.toUpperCase());}
function openNewsStoryByIndex(index){
  const numeric=Number(index);
  if(!Number.isInteger(numeric)||numeric<0||numeric>=NewsHubState.items.length)return;
  const item=NewsHubState.items[numeric];
  if(!item)return;
  const title=String(item.title||'Untitled headline');
  const source=String(item.source||'News publisher');
  setTxt('newsStoryTitle',title);
  setTxt('newsStorySource',`${source}  ·  ${item.provider||'News feed'}`);
  setTxt('newsStoryMeta',`${newsDateLabel(item.published)} · ${item.timestamp_kind==='gdelt_seen_at_not_publisher_time'?'Timestamp is when GDELT saw this story':'Publisher timestamp'}`);
  const cats=Array.isArray(item.categories)?item.categories:[];
  setHtml('newsStoryCategories',cats.map(c=>`<span class="news-tag">${esc(_newsCategoryLabel(c))}</span>`).join(''));
  const quantRoot=$('newsStoryQuantMeta');
  if(quantRoot){
    const band=String(item.impact_band||'low').toLowerCase();
    const score=Number.isFinite(Number(item.research_priority))?Math.max(0,Math.min(100,Number(item.research_priority))):0;
    const marketLink=Number.isFinite(Number(item.market_relevance_score))?Math.max(0,Math.min(100,Number(item.market_relevance_score))):0;
    const gate=String(item.trade_gate_status|| (marketLink<45?'BLOCKED_NO_MARKET_LINK':'CONTEXT_ONLY_NO_TRADE_AUTHORIZATION'));
    const gateLabel=gate==='BLOCKED_NO_MARKET_LINK'?'BLOCKED · NO MARKET LINK':gate==='BLOCKED_NEEDS_MULTI_DOMAIN_CORROBORATION'?'BLOCKED · NEEDS MULTI-DOMAIN PICKUP':'CONTEXT ONLY · NO TRADE AUTHORIZATION';
    const exposures=(item.asset_exposures||[]).map(a=>`<span class="news-tag">${esc(a.label||a.key||'Asset')}</span>`).join('')||'<span class="muted">No qualified market exposure detected</span>';
    const reasons=(item.research_priority_reasons||[]).map(r=>`<li>${esc(r)}</li>`).join('');
    quantRoot.innerHTML=`<div class="news-quant-score"><span class="news-mini-label">RESEARCH QUEUE PRIORITY · NOT A PERCENTAGE</span><b class="impact-text-${esc(band)}">${score}<small>/100 points</small></b><span class="news-impact-pill impact-${esc(band)}">${esc(band.toUpperCase())} PRIORITY</span></div><div class="news-quant-facts"><div><span>Market relevance</span><b>${marketLink}/100 · ${esc(String(item.market_relevance_type||'unqualified').replaceAll('_',' '))}</b></div><div><span>Automated analysis gate</span><b>${esc(gateLabel)}</b></div><div><span>Event class heuristic</span><b>${esc(item.event_type_label||'General news')}</b></div><div><span>Source-label tier heuristic</span><b>${esc(String(item.source_quality_tier||'unrated').replaceAll('_',' ').toUpperCase())} · ${esc(String(item.source_quality_score??'—'))}/100 · unverified</b></div><div><span>Headline novelty</span><b>${esc(String(item.novelty_score??'—'))}/100 · ${esc(String(item.event_cluster_size||1))} similar item(s)</b></div><div><span>Headline wording tone</span><b>${esc(String(item.research_sentiment_label||item.local_sentiment||'neutral').toUpperCase())} · ${esc(String(item.research_sentiment_score??item.local_sentiment_score??0))}</b></div></div><div class="news-quant-exposures"><span class="news-mini-label">QUALIFIED MARKET EXPOSURES</span><div>${exposures}</div></div><div class="news-quant-reasons"><span class="news-mini-label">WHY IT RANKED HERE / GATE RATIONALE</span><ul>${reasons||'<li>Insufficient metadata for a detailed explanation.</li>'}</ul></div><p class="news-quant-disclaimer">Priority is an ordinal research queue score, not a probability or expected return. It cannot predict direction or authorize a trade. No relevance score is proof that the story is true; publisher metadata, market linkage, live reaction, and instrument-specific validation remain separate checks.</p>`;
    const agent=item.news_analyst;
    if(agent&&typeof agent==='object'){
      const hypotheses=(agent.asset_impact_hypotheses||[]).slice(0,4).map(h=>`<div class="news-agent-mini-hypothesis"><b>${esc(h.asset||'Asset')} · ${esc(h.directional_state||'WATCH_ONLY')}</b><span>${esc(h.reason||'Conditional scenario only.')}</span><small>UP: ${esc(h.upside_channel||'Not established')}<br>DOWN: ${esc(h.downside_channel||'Not established')}</small></div>`).join('');
      const tickerHypotheses=(agent.ticker_impact_hypotheses||[]).slice(0,3).map(h=>`<div class="news-agent-mini-hypothesis"><b>${esc(h.ticker||'TICKER')} · ${esc(h.directional_state||'TWO_SIDED')}</b><span>${esc(h.reason||'Issuer-specific confirmation required.')}</span><small>UP: ${esc(h.upside_channel||'Not established')}<br>DOWN: ${esc(h.downside_channel||'Not established')}</small></div>`).join('');
      const reaction=agent.market_reaction||{};
      quantRoot.insertAdjacentHTML('beforeend',`<div class="news-agent-story-analysis"><div class="news-agent-story-head"><b>NEWS AGENT · EVENT TRANSMISSION</b><span>${esc(agent.event_type_label||agent.event_type||'General news')} · ${Number(agent.event_impact_score||0)} severity points</span></div><p>${esc((agent.event_mechanism||{}).mechanism||'No defensible transmission mechanism established from this headline.')}</p><div class="news-agent-story-meta"><span>Evidence quality <b>${Number(agent.evidence_quality_score||0)}/100</b></span><span>Market reaction <b>${esc(reaction.status||'NOT_MEASURED')}</b></span><span>Gate <b>${esc(agent.agent_gate_status||'CONTEXT ONLY')}</b></span></div>${hypotheses}${tickerHypotheses}<p class="news-quant-disclaimer">Observed price movement is not causal attribution. Hypotheses are conditional research context, not a probability, trade direction or order instruction.</p></div>`);
    }

  }
  const summary=String(item.summary||'').trim();
  setHtml('newsStorySummary',summary
    ? `<p>${esc(summary)}</p><p class="news-story-provenance">Publisher-provided feed synopsis. It may be shorter than the publisher's full article.</p>`
    : `<p class="muted">This source supplied headline metadata but no article synopsis. The News Desk will not invent article text. Related coverage is shown below to provide additional context.</p>`);
  const related=NewsHubState.items.map((row,idx)=>({row,idx})).filter(({row,idx})=>idx!==numeric&&cats.some(c=>(row.categories||[]).includes(c))).slice(0,4);
  const relatedRoot=$('newsStoryRelated');
  if(relatedRoot){
    relatedRoot.innerHTML=related.length?related.map(({row,idx})=>`<button type="button" class="news-related-item" onclick="openNewsStoryByIndex(${idx})"><span class="news-related-source">${esc(row.source||row.provider||'News desk')}</span><b>${esc(row.title||'Related coverage')}</b><small>${esc(newsDateLabel(row.published))}</small></button>`).join(''):'<p class="muted">No related headlines are available in the current feed window.</p>';
  }
  const publisher=$('newsStoryPublisherLink');
  const safeUrl=safeNewsPublisherUrl(item.url);
  if(publisher){publisher.href=safeUrl||'#';publisher.hidden=!safeUrl;publisher.onclick=ev=>{if(!safeUrl){ev.preventDefault();return;} };}
  const dialog=$('newsStoryDialog');
  if(dialog&&typeof dialog.showModal==='function')dialog.showModal();
  else if(dialog)dialog.setAttribute('open','open');
}
function closeNewsStory(){const dialog=$('newsStoryDialog');if(dialog?.open&&typeof dialog.close==='function')dialog.close();else dialog?.removeAttribute('open');}
function renderNewsHeadlines(){
  const root=$('newsFeedGrid');if(!root)return;
  const q=NewsHubState.query.trim().toLowerCase();
  const all=NewsHubState.items||[];
  const impactRank={low:0,medium:1,high:2,critical:3};
  const rows=all.filter(item=>{
    const cats=Array.isArray(item.categories)?item.categories:[];
    const categoryOk=NewsHubState.category==='all'||cats.includes(NewsHubState.category);
    const text=[item.title,item.summary,item.source,item.provider,(item.ticker_mentions||[]).join(' '),(item.asset_exposures||[]).map(a=>a.label||a.key).join(' ')].join(' ').toLowerCase();
    const band=String(item.impact_band||'low').toLowerCase();
    const impactOk=NewsHubState.impactFilter==='all'||(NewsHubState.impactFilter==='critical'?band==='critical':NewsHubState.impactFilter==='high'?(impactRank[band]||0)>=2:NewsHubState.impactFilter==='medium'?(impactRank[band]||0)>=1:true);
    const assetOk=NewsHubState.assetFilter==='all'||(item.asset_exposures||[]).some(a=>a.key===NewsHubState.assetFilter);
    return categoryOk&&impactOk&&assetOk&&(!q||text.includes(q));
  });
  const shown=rows.slice(0,NewsHubState.displayCount);
  const countLabel=$('newsResultsLabel');
  if(countLabel)countLabel.textContent=rows.length?`Showing ${shown.length} of ${rows.length} stories in this News Desk`:'No stories match this filter.';
  const more=$('newsLoadMoreBtn');if(more)more.hidden=shown.length>=rows.length||rows.length===0;
  if(!shown.length){
    let title='No matching stories';
    let reason='There are no stories in this category in the currently loaded feed window. Refresh feeds or try All headlines.';
    if(!all.length){title='No news loaded yet';reason=NewsHubState.status?.last_error||(!NewsHubState.loaded?'Feed request has not completed.':'Sources returned no current headlines. Try refreshing or check Feed status below.');}
    root.innerHTML=`<div class="market-empty news-empty-state"><b>${esc(title)}</b><p>${esc(reason)}</p><button class="btn btn-ghost" type="button" onclick="refreshNewsHub()">Refresh newsroom</button><button class="btn btn-ghost" type="button" onclick="setNewsCategory('all')">Show all headlines</button></div>`;
    return;
  }
  root.innerHTML=shown.map(item=>{
    const fullIndex=all.indexOf(item);
    const cats=(item.categories||[]).slice(0,3).map(c=>`<span class="news-tag">${esc(_newsCategoryLabel(c))}</span>`).join('');
    const localSentimentBadge=item.local_sentiment&&item.local_sentiment_score!==undefined?`<span class="news-tag sentiment-${esc(item.local_sentiment)}">Local tone ${esc(String(item.local_sentiment).toUpperCase())}</span>`:'';
    const tradingUrgencyBadge=item.provider==='TradingNews.press API'&&item.urgency?`<span class="news-tag urgency-${esc(item.urgency)}">${esc(String(item.urgency).toUpperCase())}</span>`:'';
    const summary=item.summary?`<p class="news-card-summary">${esc(item.summary)}</p>`:'<p class="news-card-summary news-summary-unavailable">Publisher feed did not provide a synopsis. Open the story brief for related coverage.</p>';
    const age=item.published?Math.max(0,(Date.now()-new Date(item.published).getTime())/60000):null;
    const fresh=age!==null&&Number.isFinite(age)&&age<=180;
    const timestamp=item.timestamp_kind==='gdelt_seen_at_not_publisher_time'?'Seen by GDELT':'Publisher time';
    const band=String(item.impact_band||'low').toLowerCase();
    const priority=Number.isFinite(Number(item.research_priority))?Math.max(0,Math.min(100,Number(item.research_priority))):0;
    const exposures=(item.asset_exposures||[]).slice(0,4).map(a=>`<span class="news-tag">${esc(a.key||a.label||'ASSET')}</span>`).join('');
    const relevance=Math.max(0,Math.min(100,Number(item.market_relevance_score)||0));
    const linkGate=String(item.trade_gate_status|| (relevance<45?'BLOCKED_NO_MARKET_LINK':'CONTEXT_ONLY_NO_TRADE_AUTHORIZATION'));
    const linkLabel=relevance<45?'MARKET LINK BLOCKED':item.trade_gate_status==='BLOCKED_NEEDS_MULTI_DOMAIN_CORROBORATION'?'NEEDS MULTI-DOMAIN PICKUP':`MARKET LINK ${relevance}/100`;
    const quantStrip=`<div class="news-research-strip"><span class="news-impact-pill impact-${esc(band)}">${esc(band.toUpperCase())} PRIORITY · ${priority}/100 PTS</span><span class="news-event-class">${esc(item.event_type_label||'General coverage')}</span><span class="news-novelty-label">${esc(linkLabel)}</span><span class="news-novelty-label">${esc(linkGate==='BLOCKED_NO_MARKET_LINK'?'NON-VOTING':'CONTEXT ONLY')}</span></div>`;
    return `<article class="news-article-card"><div class="news-card-topline"><span class="news-publisher">${esc(item.source||'News source')}</span><span class="news-time-label ${fresh?'fresh':''}">${fresh?'RECENT · ':''}${esc(newsDateLabel(item.published))}</span></div><button class="news-story-title-button" type="button" onclick="openNewsStoryByIndex(${fullIndex})">${esc(item.title||'Untitled headline')}</button>${quantStrip}${summary}<div class="news-card-footer"><div class="news-tag-row">${cats}${exposures}${tradingUrgencyBadge}${localSentimentBadge}<span class="news-provider-tag">${esc(item.provider||'Feed')}</span></div><span class="news-timestamp-kind">${esc(timestamp)}</span><button type="button" class="news-open-link news-read-more-btn" onclick="openNewsStoryByIndex(${fullIndex})">Read more in News Desk · Open research brief →</button></div></article>`;
  }).join('');
}
function renderNewsIntelligence(){
  const intel=NewsHubState.intelligence||{};
  const items=NewsHubState.items||[];
  setTxt('newsIntelEngineStatus',items.length?'CONSERVATIVE LINKAGE · NEWS NON-VOTING':'TRIAGE ENGINE READY');
  setTxt('newsIntelAsOf',NewsHubState.status?.last_fetch_at?`Sample as of ${new Date(NewsHubState.status.last_fetch_at).toLocaleTimeString()}`:'Awaiting current sample');
  setTxt('newsIntelHighCount',String(intel.high_impact_count??items.filter(x=>['critical','high'].includes(x.impact_band)).length));
  setTxt('newsIntelCriticalDetail',`${Number(intel.critical_count||0)} critical-priority · ${items.length} headline(s) sampled`);
  setTxt('newsIntelClusterCount',String(intel.distinct_event_clusters??'—'));
  setTxt('newsIntelDuplicateDetail',`${Number(intel.duplicate_headline_count||0)} near-duplicate item(s) consolidated`);
  setTxt('newsIntelCorroborationCount',String(intel.cross_source_corroborated_clusters??0));
  setTxt('newsIntelMarketLink',`${Number(intel.market_relevant_headline_count||0)} / ${items.length}`);
  setTxt('newsIntelBlockedDetail',`${Number(intel.blocked_unlinked_headline_count||0)} unlinked/blocked by market-relevance gate`);
  const radar=$('newsEventRadarList');
  const priorityRows=[...items].sort((a,b)=>(Number(b.research_priority)||0)-(Number(a.research_priority)||0)).slice(0,6);
  setTxt('newsIntelRadarCount',`${priorityRows.length} EVENT${priorityRows.length===1?'':'S'}`);
  if(radar){
    radar.innerHTML=priorityRows.length?priorityRows.map(item=>{
      const idx=items.indexOf(item);const band=String(item.impact_band||'low').toLowerCase();const score=Math.max(0,Math.min(100,Number(item.research_priority)||0));
      const related=Number(item.event_cluster_size||1);const source=String(item.source||item.provider||'News source');
      const exposures=(item.asset_exposures||[]).slice(0,3).map(a=>`<span>${esc(a.key||a.label||'ASSET')}</span>`).join('');
      const relevance=Math.max(0,Math.min(100,Number(item.market_relevance_score)||0));
      const gate=String(item.trade_gate_status|| (relevance<45?'BLOCKED_NO_MARKET_LINK':'CONTEXT_ONLY_NO_TRADE_AUTHORIZATION'));
      return `<button class="news-radar-row" type="button" onclick="openNewsStoryByIndex(${idx})"><div class="news-radar-score impact-${esc(band)}"><b>${score}</b><small>pts / 100</small></div><div class="news-radar-body"><div class="news-radar-meta"><span class="news-impact-pill impact-${esc(band)}">${esc(band.toUpperCase())} PRIORITY</span><span>Market link ${relevance}/100</span><span>${esc(item.event_type_label||'General news')}</span><time>${esc(newsDateLabel(item.published))}</time></div><strong>${esc(item.title||'Untitled headline')}</strong><div class="news-radar-foot"><span>${esc(source)}</span><span>${related>1?`${related} similar headlines (not independently verified)`:'No near duplicate'}</span><span class="news-radar-exposures">${gate==='BLOCKED_NO_MARKET_LINK'?'AUTOMATED ANALYSIS BLOCKED':gate==='BLOCKED_NEEDS_MULTI_DOMAIN_CORROBORATION'?'NEEDS MULTI-DOMAIN PICKUP':'CONTEXT ONLY'} · ${exposures}</span></div><div class="news-priority-track"><i style="width:${score}%"></i></div></div><span class="news-radar-open">↗</span></button>`;
    }).join(''):'<div class="news-empty-mini">No current headlines. Refresh feeds to populate the priority queue.</div>';
  }
  const matrixRoot=$('newsAssetMatrix');const matrix=intel.asset_matrix||[];
  if(matrixRoot){
    matrixRoot.innerHTML=matrix.length?matrix.slice(0,7).map(row=>{
      const score=Math.max(-1,Math.min(1,Number(row.tone_score)||0));const width=Math.round(Math.abs(score)*100);const cls=score>=0.12?'tone-positive':score<=-0.12?'tone-negative':'tone-neutral';
      return `<button class="news-asset-row" type="button" onclick="setNewsAssetFilter('${esc(row.key)}')"><span class="news-asset-label"><b>${esc(row.key)}</b><small>${esc(row.label)}</small></span><span class="news-asset-volume"><b>${Number(row.headline_count||0)}</b><small>${Number(row.high_impact_count||0)} high-priority</small></span><span class="news-asset-tone"><span class="news-tone-track"><i class="${cls}" style="width:${Math.max(8,width)}%"></i></span><small>${esc(row.tone_label||'mixed / neutral')} · ${score>0?'+':''}${score.toFixed(2)}</small></span></button>`;
    }).join(''):'<div class="news-empty-mini">Asset coverage appears as soon as relevant terms are detected in the current headline sample.</div>';
  }
  const brief=$('newsIntelBrief');
  if(brief){
    const newestAge=intel.latest_story_age_minutes;
    const freshCount=Number(intel.fresh_60m_count||0);
    const top=priorityRows[0];
    const concentration=matrix[0];
    const topRelevance=top?Number(top.market_relevance_score)||0:0;
    const topGate=top?String(top.trade_gate_status|| (topRelevance<45?'BLOCKED_NO_MARKET_LINK':'CONTEXT_ONLY_NO_TRADE_AUTHORIZATION')):'BLOCKED_NO_MARKET_LINK';
    const topText=top?`Highest research-queue item: “${esc(top.title||'Untitled headline')}” (${Number(top.research_priority)||0}/100 points; market link ${topRelevance}/100; ${topGate==='BLOCKED_NO_MARKET_LINK'?'automated analysis blocked':'context only, no trade authorization'}).`:'No headline event is available to rank yet.';
    brief.innerHTML=`<div class="news-brief-statline"><b>${items.length}</b><span>headlines in current sample · ${Number(intel.market_relevant_headline_count||0)} passed the global market-link screen</span></div><p>${freshCount} headline(s) have a parsed timestamp within 60 minutes${newestAge===null?'':' · newest parsed item '+(newestAge<60?`${Math.round(newestAge)}m ago`:`${(newestAge/60).toFixed(1)}h ago`)}.</p><p>${Number(intel.blocked_unlinked_headline_count||0)} headline(s) were excluded from market-linked coverage. Stories with a fresh publisher link and an explicit asset relationship are separately required before any story reaches the instrument analysis context.</p><p>${Number(intel.cross_source_corroborated_clusters||0)} cluster(s) have multiple source labels. That is pickup/syndication evidence only, not proof of truth or independent confirmation.</p><p>${concentration?`Most represented qualified exposure: ${esc(concentration.label)} (${Number(concentration.headline_count)} headline(s), ${Number(concentration.high_impact_count)} high/critical priority).`:'No qualified market exposure was detected in this sample.'}</p><div class="news-brief-topline">${topText}</div>`;
  }
  const method=$('newsIntelMethodology');
  if(method){const rows=intel.methodology||[];method.innerHTML=`<ul>${rows.map(x=>`<li>${esc(x)}</li>`).join('')||'<li>Research priority is an ordinal queue score. Explicit market relevance, source/time provenance and instrument-specific gates remain separate; news does not authorize trades.</li>'}</ul>`;}
}
function renderNewsAgentConsole(){
  const report=NewsHubState.instrumentAnalysis||{};
  const events=Array.isArray(report.events)?report.events:[];
  const instrument=String(report.instrument||'—').toUpperCase();
  const status=String(report.status||(report.engine?'ok':'WAITING_FOR_MARKET_SNAPSHOT'));
  const statusLabel=status.toLowerCase()==='ok'?'ANALYZED':status.replaceAll('_',' ');
  const riskReview=report.risk_review||{};
  setTxt('newsAgentInstrumentPill',`${instrument} · ${statusLabel}`);
  setTxt('newsAgentVotePill',report.directional_vote_allowed?'VOTE GATE REQUIRES EXTERNAL VALIDATION':'DIRECTIONAL VOTE DISABLED');
  const sizeOn=Boolean(riskReview.risk_size_reduction_enabled||riskReview.automatic_risk_change);
  const riskText=sizeOn?'One-way size reduction enabled only after a qualified event plus fresh same-instrument reaction.':'Automatic news size overlay disabled; context only.';
  const reportTime=report.generated_at?newsDateLabel(report.generated_at):'time not available';
  setTxt('newsAgentStatusLine',`${Number(report.matched_event_count||0)} matched event(s) · ${Number(report.qualified_event_count||0)} eligible for context-only research · ${riskReview.required?'risk review required':'no qualified high-impact catalyst'} · as of ${reportTime} · ${riskText}`);
  const root=$('newsAgentEvents');if(!root)return;
  const visible=events.slice(0,4);
  root.innerHTML=visible.length?visible.map(event=>{
    const gate=String(event.instrument_gate_status||event.agent_gate_status||'BLOCKED');
    const eligible=Boolean(event.eligible_for_instrument_research);
    const severity=Math.max(0,Math.min(100,Number(event.event_impact_score)||0));
    const quality=Math.max(0,Math.min(100,Number(event.evidence_quality_score)||0));
    const reaction=event.market_reaction||{};
    const hypotheses=(event.asset_impact_hypotheses||[]).slice(0,4).map(h=>`<div class="news-agent-hypothesis"><div><b>${esc(h.asset||'ASSET')}</b><span>${esc(h.directional_state||'WATCH_ONLY')}</span></div><p>${esc(h.reason||'Mechanism not established.')}</p><div class="news-agent-channel"><small>UPSIDE / SUPPORTIVE</small>${esc(h.upside_channel||'Not established from current text.')}</div><div class="news-agent-channel"><small>DOWNSIDE / ADVERSE</small>${esc(h.downside_channel||'Not established from current text.')}</div><div class="news-agent-confirm">Check: ${esc((h.confirmation_checks||[]).join(' · ')||'Await independent data')}</div></div>`).join('');
    const tickerHypotheses=(event.ticker_impact_hypotheses||[]).slice(0,3).map(h=>`<div class="news-agent-hypothesis"><div><b>${esc(h.ticker||'TICKER')}</b><span>TWO-SIDED</span></div><p>${esc(h.reason||'Issuer-specific confirmation required.')}</p><div class="news-agent-channel"><small>UPSIDE / SUPPORTIVE</small>${esc(h.upside_channel||'Not established from current text.')}</div><div class="news-agent-channel"><small>DOWNSIDE / ADVERSE</small>${esc(h.downside_channel||'Not established from current text.')}</div><div class="news-agent-confirm">Check: ${esc((h.confirmation_checks||[]).join(' · ')||'Await independent data')}</div></div>`).join('');
    const mechanism=event.event_mechanism||{};
    const title=String(event.headline||'Untitled event');
    return `<article class="news-agent-event ${eligible?'is-qualified':'is-blocked'}"><div class="news-agent-event-top"><div><span class="news-mini-label">${esc(event.event_type_label||event.event_type||'EVENT')}</span><h4>${esc(title)}</h4><small>${esc(event.source||'Unknown source')} · ${esc(newsDateLabel(event.published))}</small></div><div class="news-agent-scores"><b>${severity}<small> severity pts</small></b><span>Evidence ${quality}/100</span></div></div><div class="news-agent-gates"><span class="${eligible?'gate-ok':'gate-blocked'}">${esc(gate.replaceAll('_',' '))}</span><span>Market link ${Number(event.market_relevance_score||event.instrument_relevance_score||0)}/100</span><span>Publisher domains ${Number(event.publisher_domain_count||0)}</span></div><p class="news-agent-mechanism">${esc(mechanism.mechanism||'No clear transmission mechanism from current headline text.')}</p><div class="news-agent-reaction"><b>Observed tape:</b> ${esc(reaction.status||'NOT_MEASURED')} ${reaction.move_pct==null?'':`· ${Number(reaction.move_pct).toFixed(3)}%`} · causal link ${esc(reaction.causal_attribution||'NOT_ESTABLISHED')}<small>${esc(reaction.reason||'Price response is unavailable or not attributable to this story.')}</small></div><div class="news-agent-hypotheses">${hypotheses}${tickerHypotheses||(!hypotheses?'<div class="news-empty-mini">No explicit tradable asset exposure was established from this article.</div>':'')}</div><div class="news-agent-event-foot">${esc(event.research_note||'Research context only.')} <b>NO TRADE AUTHORIZATION</b></div></article>`;
  }).join(''):`<div class="news-empty-mini">${esc(report.status==='ANALYSIS_UNAVAILABLE'?'News Agent analysis is unavailable. The system fails closed and does not infer an impact.':'No instrument-matched event is available. Headlines remain visible in the general News Desk, but no market link is assumed.')}</div>`;
}

function setNewsImpactFilter(value){
  NewsHubState.impactFilter=String(value||'all');NewsHubState.displayCount=15;
  const select=$('newsImpactFilter');if(select&&select.value!==NewsHubState.impactFilter)select.value=NewsHubState.impactFilter;
  renderNewsHeadlines();
}
function setNewsAssetFilter(value){
  NewsHubState.assetFilter=String(value||'all');NewsHubState.displayCount=15;
  const select=$('newsAssetFilter');if(select&&select.value!==NewsHubState.assetFilter)select.value=NewsHubState.assetFilter;
  renderNewsHeadlines();
}
function renderNewsFeedStatuses(){
  const root=$('newsSourceStatusList');if(!root)return;
  const feeds=NewsHubState.status?.feeds||{};
  const rows=Object.entries(feeds);
  root.innerHTML=rows.map(([name,stat])=>`<div class="news-source-status-row"><div><b>${esc(name.replaceAll('_',' ').toUpperCase())}</b><small>${Number(stat.count||0)} item(s) · ${stat.latency_ms==null?'latency n/a':`${esc(stat.latency_ms)} ms`}</small></div><span class="status-pill ${stat.status==='ok'?'good':stat.status==='disabled'||stat.status==='not_configured'?'neutral':'warn'}">${esc(String(stat.status||'unknown').toUpperCase())}</span>${stat.error?`<p class="news-feed-error">${esc(stat.error)}</p>`:''}</div>`).join('')||'<p class="muted">No source status reported yet.</p>';
}
function renderNewsHub(data){
  if(data&&Array.isArray(data.items)){NewsHubState.items=data.items;NewsHubState.loaded=true;}
  if(data&&data.status)NewsHubState.status=data.status;
  if(data&&data.intelligence)NewsHubState.intelligence=data.intelligence;
  if(data&&data.impact_analysis)NewsHubState.impactAnalysis=data.impact_analysis;
  if(data&&data.instrument_analysis)NewsHubState.instrumentAnalysis=data.instrument_analysis;
  const items=NewsHubState.items||[];
  const now=Date.now();
  const fresh=items.filter(x=>x.published&&Number.isFinite(new Date(x.published).getTime())&&(now-new Date(x.published).getTime())<=3*3600000).length;
  setTxt('newsCountMetric',String(items.length));
  setTxt('newsCountDetail',items.length?'Stories unified in the News Desk':'No current stories');
  setTxt('newsFreshMetric',String(fresh));
  const feedStatuses=NewsHubState.status?.feeds||{};
  const visibleStatuses=Object.values(feedStatuses).filter(x=>!['not_configured','disabled','cooldown'].includes(x.status));
  const ok=visibleStatuses.filter(x=>x.status==='ok').length;
  setTxt('newsSourceMetric',`${ok}/${visibleStatuses.length||NewsHubState.status?.configured_feeds||0}`);
  setTxt('newsSourceDetail',`${NewsHubState.status?.rss_feeds||0} RSS/Atom · ${NewsHubState.status?.gdelt_enabled?'GDELT on':'GDELT off'} · ${NewsHubState.status?.newsapi_enabled?'NewsAPI on':NewsHubState.status?.newsapi_key_configured?'NewsAPI disabled':'NewsAPI key not set'} · ${NewsHubState.status?.tradingnews_press_enabled?'TradingNews on':NewsHubState.status?.tradingnews_press?.status==='display_license_confirmation_required'?'TradingNews permission needed':NewsHubState.status?.tradingnews_press?.status==='not_configured'?'TradingNews key not set':'TradingNews off'} · ${NewsHubState.status?.free_news_sources?.rss_enabled?'Free RSS on':'Free RSS off'} · ${NewsHubState.status?.free_news_sources?.reddit_enabled?'Reddit configured':'Reddit off'} · ${NewsHubState.status?.free_news_sources?.stocktwits_enabled?'StockTwits configured':'StockTwits off'}`);
  setTxt('newsRefreshMetric',NewsHubState.status?.last_fetch_at?new Date(NewsHubState.status.last_fetch_at).toLocaleTimeString():'—');
  setTxt('newsRefreshDetail',NewsHubState.status?.refreshing?'Fetch in progress':NewsHubState.status?.last_error||'Refresh runs in the background');
  setTxt('newsLastRefreshText',NewsHubState.status?.last_fetch_at?`News desk last checked ${newsDateLabel(NewsHubState.status.last_fetch_at)}`:'Not yet refreshed');
  const configured=NewsHubState.status?.enabled===false?false:true;
  const conn=$('newsConnectionPill');
  if(conn){conn.textContent=!configured?'DISABLED':NewsHubState.status?.refreshing?'FETCHING':ok?'CONNECTED':'SOURCES DEGRADED';conn.className=`status-pill ${!configured?'warn':NewsHubState.status?.refreshing?'neutral':ok?'good':'warn'}`;}
  const feed=$('newsFeedStatus');if(feed){feed.textContent=NewsHubState.status?.refreshing?'REFRESHING':ok?'FEEDS ONLINE':NewsHubState.status?.last_error?'FEED ISSUE':'CHECK SOURCE STATUS';feed.className=`status-pill ${ok?'good':NewsHubState.status?.last_error?'warn':'neutral'}`;}
  renderNewsIntelligence();renderNewsAgentConsole();renderNewsHeadlines();renderNewsFeedStatuses();
}
async function loadNewsHub({autoRefresh=false}={}){
  try{
    const params=new URLSearchParams({limit:'100'});
    const data=await get('/api/news?'+params.toString(),{silent:true,region:'newsFeedGrid',retries:2,timeout:12000});
    renderNewsHub(data);
    const status=data.status||{};
    if(autoRefresh&&!status.refreshing&&!status.last_fetch_at&&status.enabled&&!NewsHubState.autoRefreshRequested){
      NewsHubState.autoRefreshRequested=true;
      await post('/api/news/refresh',{}, {silent:true});
      setTxt('newsRefreshDetail','Initial newsroom refresh started…');
      setTimeout(()=>{if(activePageId()==='news')loadNewsHub().catch(()=>{});},2200);
    }else if(status.refreshing){setTimeout(()=>{if(activePageId()==='news')loadNewsHub().catch(()=>{});},2500);}
  }catch(err){
    const conn=$('newsConnectionPill');if(conn){conn.textContent='UNAVAILABLE';conn.className='status-pill warn';}
    if(!$('newsFeedGrid')?.children.length||!NewsHubState.loaded){setHtml('newsFeedGrid',`<div class="market-empty news-empty-state"><b>News service unavailable</b><p>${esc(err.message||'Unable to reach the news service.')}</p><button class="btn btn-ghost" type="button" onclick="loadNewsHub({autoRefresh:true})">Retry</button></div>`);}
    setTxt('newsRefreshDetail',String(err.message||'News request failed'));
  }
}
async function refreshNewsHub(){
  const btn=$('newsRefreshBtn');if(btn){btn.disabled=true;btn.textContent='Refreshing…';}
  setTxt('newsRefreshDetail','Requesting a background refresh; existing stories remain available…');
  try{
    const response=await post('/api/news/refresh',{}, {silent:true});
    ApiCache.invalidate('/api/news');
    if(response.started)setTxt('newsRefreshDetail','Background news refresh started.');
    let attempts=0;
    const poll=async()=>{
      if(activePageId()!=='news'||attempts++>=8){if(btn){btn.disabled=false;btn.textContent='Refresh news';}return;}
      try{const data=await get('/api/news?limit=100',{silent:true,region:'newsFeedGrid',retries:1,timeout:12000,useCache:false});renderNewsHub(data);if(data.refreshing){setTimeout(poll,2000);return;}}
      catch(err){setTxt('newsRefreshDetail',String(err.message||err));}
      if(btn){btn.disabled=false;btn.textContent='Refresh news';}
    };
    setTimeout(poll,1200);
  }catch(err){setTxt('newsRefreshDetail',`Refresh failed: ${err.message||err}`);if(btn){btn.disabled=false;btn.textContent='Refresh news';}}
}
function setNewsCategory(category){
  NewsHubState.category=String(category||'all');NewsHubState.displayCount=15;
  document.querySelectorAll('[data-news-category]').forEach(btn=>{const active=btn.dataset.newsCategory===NewsHubState.category;btn.classList.toggle('active',active);btn.setAttribute('aria-pressed',active?'true':'false');});
  renderNewsHeadlines();
}
function loadMoreNews(){NewsHubState.displayCount=Math.min(100,NewsHubState.displayCount+15);renderNewsHeadlines();}
(function initNewsHub(){
  const search=$('newsSearchInput');
  if(search)search.addEventListener('input',debounce(()=>{NewsHubState.query=search.value||'';NewsHubState.displayCount=15;renderNewsHeadlines();},150));
  const frame=$('newsTvPlayer');
  if(frame){frame.addEventListener('load',()=>{if(newsTvLoadTimer)clearTimeout(newsTvLoadTimer);_setNewsTvStatus(`Player frame loaded for ${NEWS_TV_STATIONS[currentNewsStationId]?.name||'selected station'}. If there is no picture/audio, try the alternate player or open the official station page.`);});frame.addEventListener('error',()=>_setNewsTvStatus('The embedded player could not be loaded. Try the alternate player or open the official station page.'));}
  const dialog=$('newsStoryDialog');
  if(dialog){dialog.addEventListener('click',ev=>{if(ev.target===dialog)closeNewsStory();});}
  try{const saved=localStorage.getItem('att_news_tv_station');if(saved&&NEWS_TV_STATIONS[saved])currentNewsStationId=saved;_applyNewsTvMode(currentNewsStationId,'embedded');}catch(_){_applyNewsTvMode('aljazeera','embedded');}
  const clock=()=>setTxt('newsTvLocalClock',new Date().toLocaleTimeString());clock();setInterval(clock,15000);
})();
window.selectNewsStation=selectNewsStation;
window.toggleNewsPlayerMode=toggleNewsPlayerMode;
window.openNewsStoryByIndex=openNewsStoryByIndex;
window.closeNewsStory=closeNewsStory;
window.loadNewsHub=loadNewsHub;
window.refreshNewsHub=refreshNewsHub;
window.setNewsCategory=setNewsCategory;
window.setNewsImpactFilter=setNewsImpactFilter;
window.setNewsAssetFilter=setNewsAssetFilter;
window.loadMoreNews=loadMoreNews;

/* ── Global navigation + methodology ─────────────────────── */
function syncGlobalNav(page){
  document.querySelectorAll('#globalNav .global-nav-btn').forEach(b=>{
    b.classList.toggle('active', b.dataset.page === page);
  });
}
document.addEventListener('click', (ev)=>{
  const t = ev.target.closest('[data-page]');
  if(!t) return;
  // nav-item already handled; also dash cards + global nav
  if(t.classList.contains('global-nav-btn') || t.classList.contains('dash-nav-card') || t.classList.contains('nav-item')){
    const page = t.dataset.page;
    if(page && typeof goPage === 'function'){
      goPage(page);
      ev.preventDefault();
    }
  }
});

function setQuantStage(stage){
  // Portfolio is a dedicated top-level workspace, not an embedded Quant panel.
  if(stage === 'portfolio'){
    if(typeof goPage === 'function') goPage('portfolio', {smooth:true});
    return;
  }
  const panel = document.querySelector(`#quantStagePanels [data-stage-panel="${stage}"]`);
  if(!panel) return;
  document.querySelectorAll('#quantStageTabs .stage-tab').forEach(b=>{
    const selected = b.dataset.stage === stage;
    b.classList.toggle('active', selected);
    b.setAttribute('aria-selected', selected ? 'true' : 'false');
  });
  document.querySelectorAll('#quantStagePanels [data-stage-panel]').forEach(p=>{
    const selected = p.dataset.stagePanel === stage;
    p.classList.toggle('active', selected);
    p.hidden = !selected;
  });
  if(stage === 'docs') loadMethodologyDocs();
  if(stage === 'pricing' && typeof window.loadQuantLibStatus === 'function') window.loadQuantLibStatus();
  // Plotly may have initialized while a panel was hidden; resize only charts in the now-visible panel.
  requestAnimationFrame(()=>{
    if(window.Plotly){
      panel.querySelectorAll('.js-plotly-plot').forEach(plot=>{
        try{ window.Plotly.Plots.resize(plot); }catch(_){}
      });
    }
  });
}
document.addEventListener('click', (ev)=>{
  const tab = ev.target.closest('.stage-tab');
  if(tab && tab.dataset.stage) setQuantStage(tab.dataset.stage);
});
document.addEventListener('click', (ev)=>{
  const card = ev.target.closest('[data-quant-stage]');
  if(card && card.dataset.quantStage) setQuantStage(card.dataset.quantStage);
});

async function loadMethodologyDocs(){
  try{
    const data = await post('/api/quant/lab', { stages: ['docs'], closes: [100,101,102,103,104] });
    const docs = (data.stages || {}).docs || [];
    const html = docs.map(d=>`<div class="log" style="margin-bottom:10px"><b>${esc(d.title||d.id)}</b><pre style="white-space:pre-wrap;font-size:11px;margin:6px 0 0;opacity:.92">${esc((d.body||'').trim())}</pre></div>`).join('') || '<div class="muted">No docs</div>';
    if($('methodologyDocs')) $('methodologyDocs').innerHTML = html;
    if($('quantDocs')) { $('quantDocs').innerHTML = html; $('quantDocs').style.display = 'block'; }
    if($('quantResult')) $('quantResult').innerHTML = `<div class="safety-box"><b>Methodology loaded</b><p class="muted">${docs.length} formulas / notes</p></div>`;
    toast('METHODOLOGY · '+docs.length+' entries');
  }catch(e){
    toast('ERROR: '+(e.message||e));
  }
}
window.loadMethodologyDocs = loadMethodologyDocs;
window.setQuantStage = setQuantStage;
window.syncGlobalNav = syncGlobalNav;
/* ── Unified Quant + ICE (single process) ─────────────────── */
const _QLayout = () => {
  const dark = !document.body.classList.contains('theme-light') && document.documentElement.dataset.theme !== 'light';
  return {
    paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
    font: { color: dark ? '#c8d0e0' : '#1a2233', size: 11 },
    margin: { l: 48, r: 20, t: 24, b: 40 },
    legend: { orientation: 'h', y: 1.12 },
    xaxis: { gridcolor: dark ? 'rgba(255,255,255,0.06)' : 'rgba(0,0,0,0.06)' },
    yaxis: { gridcolor: dark ? 'rgba(255,255,255,0.06)' : 'rgba(0,0,0,0.06)' },
  };
};
function _quantPanelMessage(el, message, kind='info', title='Chart unavailable'){
  const node = typeof el === 'string' ? $(el) : el;
  if(!node) return;
  node.innerHTML = `<div class="quant-chart-state ${kind==='error'?'error':(kind==='warning'?'warning':'')}" role="status"><strong>${esc(title)}</strong><span>${esc(message || 'No chart data was returned.')}</span></div>`;
}
function _plotly(el, data, extra){
  const node = typeof el === 'string' ? $(el) : el;
  if(!node) return null;
  const hasSeries = Array.isArray(data) && data.some(trace => trace && typeof trace === 'object' &&
    Object.values(trace).some(value => Array.isArray(value) && value.length > 0));
  if(!hasSeries){
    _quantPanelMessage(node, 'The analytics response did not contain a usable series for this panel.', 'info', 'No series returned');
    return null;
  }
  if(typeof Plotly === 'undefined'){
    _quantPanelMessage(node, 'The Plotly browser library did not load. Check network access to cdn.plot.ly, then reload this page.', 'error', 'Chart library unavailable');
    return null;
  }
  try{
    node.innerHTML = '';
    const result = Plotly.newPlot(node, data, Object.assign({}, _QLayout(), extra||{}), {displayModeBar:false, responsive:true});
    if(result && typeof result.catch === 'function') result.catch(error=>{
      console.error('Quant chart render failed:', error);
      _quantPanelMessage(node, 'The browser chart renderer failed: '+(error?.message||error), 'error', 'Chart rendering failed');
    });
    return result;
  }catch(error){
    console.error('Quant chart render failed:', error);
    _quantPanelMessage(node, 'The browser chart renderer failed: '+(error?.message||error), 'error', 'Chart rendering failed');
    return null;
  }
}
function _qPct(x){ return (typeof x==='number' && isFinite(x)) ? (x*100).toFixed(1)+'%' : '—'; }
function _qNum(x,d=2){ return (typeof x==='number' && isFinite(x)) ? x.toFixed(d) : '—'; }

function _quantBody(stages){
  return {
    symbol: ($('quantSymbol')?.value || '').trim() || undefined,
    timeframe: $('quantTf')?.value || 'M5',
    bars: Number($('quantBars')?.value || 400),
    stages: stages || ['kalman','garch','ewma','forecast','vol_surface','pricing','signals','walk_forward','docs'],
    option: {
      strike: Number($('quantStrike')?.value) || undefined,
      T: Math.max(1, Number($('quantDays')?.value || 30)) / 365.0,
      sigma: Number($('quantSigma')?.value || 0.2) || 0.2,
      is_call: !!($('quantIsCall')?.checked),
      n_paths: 3500, steps: 32,
    }
  };
}

function _quantStageError(data, stage){
  const errors = data && data.errors && typeof data.errors === 'object' ? data.errors : {};
  return errors[stage] || errors.data || '';
}
function renderQuantCharts(data){
  const ch = data.charts || {};
  const pk = ch.price_kalman;
  if(pk && Array.isArray(pk.close) && pk.close.length){
    _plotly('chartKalman', [
      { x: pk.x, y: pk.close, name: 'Close', line: { color: '#3dd6c6', width: 1.5 } },
      { x: pk.x, y: pk.level, name: 'Kalman', line: { color: '#7aa2ff', width: 1.5 } },
      { x: pk.x, y: pk.upper, name: '+2σ', line: { color: 'rgba(122,162,255,0.35)', width: 1, dash: 'dot' } },
      { x: pk.x, y: pk.lower, name: '−2σ', line: { color: 'rgba(122,162,255,0.35)', width: 1, dash: 'dot' }, fill: 'tonexty', fillcolor: 'rgba(122,162,255,0.08)' },
    ]);
  }else{
    _quantPanelMessage('chartKalman', _quantStageError(data,'kalman') || 'Waiting for at least 30 usable closes for the requested symbol/timeframe.', 'warning', 'Kalman data unavailable');
  }
  const vs = ch.vol_surface;
  if(vs && Array.isArray(vs.iv_grid) && vs.iv_grid.length){
    _plotly('chartVol', [{ type:'surface', x: vs.strikes, y: vs.maturities.map(x=>Number((x*365).toFixed(0))), z: vs.iv_grid.map(row=>row.map(v=>Number.isFinite(v)?v*100:null)), colorscale:'Viridis', colorbar:{title:'Vol %'} }],
      { scene:{ xaxis:{title:'Strike'}, yaxis:{title:'Days to expiry'}, zaxis:{title:'Annualized vol (%)'}, bgcolor:'rgba(0,0,0,0)' }, margin:{l:0,r:0,t:20,b:0}, height:340 });
  }else{
    _quantPanelMessage('chartVol', _quantStageError(data,'vol_surface') || 'A volatility surface could not be built from the available close series.', 'warning', 'Volatility surface unavailable');
  }
  if(ch.pricing && Array.isArray(ch.pricing.labels) && ch.pricing.labels.length){
    _plotly('chartPricing', [{ type:'bar', x: ch.pricing.labels, y: ch.pricing.prices, marker:{ color:'#7aa2ff' } }]);
  }else{
    _quantPanelMessage('chartPricing', _quantStageError(data,'pricing') || 'This lightweight preview skips Monte Carlo/PDE pricing to keep CPU usage low. Select “Run Quant + ICE” to calculate the pricing engines.', 'info', 'Full pricing not run');
  }
  const forecast = (data.stages || {}).forecast || {};
  const gaussian = Array.isArray(forecast.gaussian_cone) ? forecast.gaussian_cone : [];
  const bootstrap = (forecast.bootstrap_cone || {}).cones || [];
  const forecastTraces = [];
  if(gaussian.length){
    forecastTraces.push({x:gaussian.map(x=>x.horizon_days),y:gaussian.map(x=>x.low),name:'Gaussian low',mode:'lines',line:{dash:'dot',color:'#7aa2ff'}});
    forecastTraces.push({x:gaussian.map(x=>x.horizon_days),y:gaussian.map(x=>x.mid),name:'Gaussian median',mode:'lines+markers',line:{color:'#3dd6c6'}});
    forecastTraces.push({x:gaussian.map(x=>x.horizon_days),y:gaussian.map(x=>x.high),name:'Gaussian high',mode:'lines',line:{dash:'dot',color:'#7aa2ff'}});
  }
  if(bootstrap.length){
    forecastTraces.push({x:bootstrap.map(x=>x.horizon_days),y:bootstrap.map(x=>x.p05),name:'Bootstrap 5th pct',mode:'lines',line:{dash:'dash',color:'#d69bff'}});
    forecastTraces.push({x:bootstrap.map(x=>x.horizon_days),y:bootstrap.map(x=>x.p50),name:'Bootstrap median',mode:'lines+markers',line:{color:'#d69bff'}});
    forecastTraces.push({x:bootstrap.map(x=>x.horizon_days),y:bootstrap.map(x=>x.p95),name:'Bootstrap 95th pct',mode:'lines',line:{dash:'dash',color:'#d69bff'}});
  }
  if(forecastTraces.length){
    _plotly('chartForecast', forecastTraces, {xaxis:{title:'Horizon (trading days)'},yaxis:{title:'Scenario price'},margin:{l:52,r:12,t:20,b:42},height:320});
    setTxt('quantForecastSummary', `Historical-return scenarios · ${(forecast.bootstrap_cone||{}).n_paths||0} bootstrap paths · not calibrated probabilities and not guarantees.`);
  }else{
    _quantPanelMessage('chartForecast', _quantStageError(data,'forecast') || 'Forecast needs at least 50 real closes. Select a symbol/timeframe with available MT5 history.', 'warning', 'Forecast unavailable');
    setTxt('quantForecastSummary', 'No forecast calculated for this data window.');
  }
  const stc = ch.strategies || {};
  const names = Object.keys(stc);
  if(names.length){
    _plotly('chartStrat', names.map(n=>({ y: stc[n].equity||[], name:n, line:{width:1.3} })));
  }else{
    _quantPanelMessage('chartStrat', _quantStageError(data,'signals') || 'Strategy equity needs a sufficiently long close history.', 'warning', 'Strategy equity unavailable');
  }
  if(ch.walk_forward && Array.isArray(ch.walk_forward.fold) && ch.walk_forward.fold.length){
    const wf = ch.walk_forward;
    _plotly('chartWF', [
      { x: wf.fold, y: wf.is_sharpe, name:'IS', mode:'lines+markers', line:{color:'#7aa2ff'} },
      { x: wf.fold, y: wf.oos_sharpe, name:'OOS', mode:'lines+markers', line:{color:'#3dd6c6'} },
    ]);
  }else{
    _quantPanelMessage('chartWF', _quantStageError(data,'walk_forward') || 'Walk-forward needs a longer history (normally 150+ closes).', 'warning', 'Walk-forward unavailable');
  }
}
function renderIcePanel(iceData){
  const ice = (iceData && iceData.ice) || iceData || {};
  const layers = ice.layers || [];
  const sig = ice.signal;
  const label = sig>0 ? 'BUY' : (sig<0 ? 'SELL' : 'FLAT');
  setTxt('qIceSig', label);
  setTxt('qIceConf', 'conf '+_qPct(ice.confidence)+' · '+(ice.pass?'PASS':'WAIT'));
  if(layers.length){
    _plotly('chartIce', [{
      type: 'bar',
      x: layers.map(l=>l.name),
      y: layers.map(l=>l.score),
      marker: { color: layers.map(l=> l.side>0 ? '#3dd6c6' : (l.side<0 ? '#ff6b6b' : '#8899aa')) }
    }], { title: '' });
  }else{
    _quantPanelMessage('chartIce', (iceData && (iceData.error || iceData.message)) || 'No ICE layers were returned. Check symbol selection and MT5 history, then retry ICE + Grid.', 'error', 'ICE data unavailable');
  }
  const grid = (iceData && iceData.grid) || {};
  if($('qGridTitle')) $('qGridTitle').textContent = (grid.type || '—') + ' · step ' + _qNum(grid.step_used, 4);
  const body = $('quantGridBody');
  if(body){
    const levels = grid.levels || [];
    body.innerHTML = levels.length ? levels.map(lv=>
      `<tr><td>${lv.index}</td><td>${lv.side>0?'BUY':'SELL'}</td><td>${fmt(lv.price)}</td><td>${_qNum(lv.lots,3)}</td><td>${lv.filled?'yes':'no'}</td></tr>`
    ).join('') : '<tr><td colspan="5" class="muted">No grid</td></tr>';
  }
}

function renderQuantLab(data){
  window.__quantLastData = data || null;
  const st = data.stages || {};
  setTxt('qLast', fmt(data.last_price));
  setTxt('qBars', (data.n_bars!=null? data.n_bars+' bars · '+(data.timeframe||'') : '—'));
  const kf = st.kalman || {};
  setTxt('qKalmanSig', kf.signal || '—');
  const g = st.garch || {};
  const eng = (st.pricing||{}).engines || {};
  setTxt('qGarch', g.latest_vol_ann!=null ? _qPct(g.latest_vol_ann) : '—');
  setTxt('qBs', eng.bs_closed_form?.price!=null ? ('BS '+fmt(eng.bs_closed_form.price)) : 'BS —');
  if($('quantPill')){ $('quantPill').textContent = 'IN-PROCESS'; $('quantPill').className = 'status-pill good'; }
  renderQuantCharts(data);
  const sig = st.signals || {};
  const body = $('quantStratBody');
  if(body){
    const names = Object.keys(sig);
    body.innerHTML = names.length ? names.map(name=>{
      const m = sig[name]||{};
      return `<tr><td><b>${esc(name)}</b></td><td>${_qNum(m.sharpe,2)}</td><td>${_qNum(m.sortino,2)}</td><td>${_qPct(m.max_drawdown)}</td><td>${_qNum(m.final_equity,3)}</td><td>${_qNum(m.look_ahead_gap,3)}</td></tr>`;
    }).join('') : '<tr><td colspan="6" class="muted">—</td></tr>';
  }
  if($('quantResult')){
    const sourceWarning = data.stages?.vol_surface?.market_implied === false
      ? ' Volatility display is a model scenario based on an assumed volatility; this project has no broker option-chain quotes for a market-implied surface.' : '';
    $('quantResult').textContent = (data.disclaimer || '') + (data.data_source ? ' · data source: '+data.data_source : '') + (data.stages?.vol_surface?.source_note ? ' · volatility: '+data.stages.vol_surface.source_note : '') + sourceWarning;
  }
}

async function _runQuantPipeline({full=true, automatic=false}={}){
  if(window.__quantRunInFlight) return;
  window.__quantRunInFlight = true;
  const status = $('quantStatus');
  const label = full ? 'Running full Quant + ICE…' : (automatic ? 'Loading lightweight Quant + ICE preview…' : 'Running lightweight quant charts…');
  if(status) status.textContent = label;
  if($('quantPill')){ $('quantPill').textContent = 'LOADING'; $('quantPill').className = 'status-pill warn'; }
  const stages = full ? undefined : ['kalman','garch','ewma','forecast','vol_surface','signals','walk_forward'];
  const symbol = ($('quantSymbol')?.value || '').trim() || undefined;
  const grid = $('quantGrid')?.value || 'SmartRegime';
  const quantPromise = post('/api/quant/lab', _quantBody(stages), { retries: 2 });
  const icePromise = post('/api/ice/evaluate', { symbol, timeframe: $('quantTf')?.value || 'M5', grid, equity: 10000 }, { retries: 2 });
  let qOk = false, iceOk = false, qMessage = '', iceMessage = '';
  try{
    const [qResult, iceResult] = await Promise.allSettled([quantPromise, icePromise]);
    let qData = null, iceData = null;
    if(qResult.status === 'fulfilled' && qResult.value && !qResult.value.error){
      qData = qResult.value;
      renderQuantLab(qData);
      qOk = true;
      window.__quantPreviewLoaded = true;
      if(full) window.__quantFullLoaded = true;
    }else{
      qMessage = qResult.status === 'rejected' ? (qResult.reason?.message || String(qResult.reason)) : (qResult.value?.error || 'Quant endpoint returned no data');
      ['chartKalman','chartVol','chartPricing','chartStrat','chartWF'].forEach(id=>_quantPanelMessage(id, qMessage, 'error', 'Quant request failed'));
      if($('quantPill')){ $('quantPill').textContent = 'ERROR'; $('quantPill').className = 'status-pill bad'; }
    }
    if(iceResult.status === 'fulfilled' && iceResult.value && !iceResult.value.error){
      iceData = iceResult.value;
      renderIcePanel(iceData);
      iceOk = true;
      window.__quantIceLoaded = true;
    }else{
      iceMessage = iceResult.status === 'rejected' ? (iceResult.reason?.message || String(iceResult.reason)) : (iceResult.value?.error || 'ICE endpoint returned no bars');
      _quantPanelMessage('chartIce', iceMessage, 'error', 'ICE / Grid request failed');
      setTxt('qIceSig', '—'); setTxt('qIceConf', 'ICE unavailable · check MT5 history');
    }
    const sourceWarning = qData?.stages?.vol_surface?.market_implied === false
      ? ' Volatility display is a model scenario, not a market-implied surface; no option-chain quotes were supplied.' : '';
    if(status){
      if(qOk && iceOk) status.textContent = (full ? 'Full Quant + ICE ready.' : 'Lightweight Quant + ICE preview ready.') + (qData?.data_source ? ' Data: '+qData.data_source+'.' : '') + sourceWarning;
      else if(qOk) status.textContent = 'Quant charts ready; ICE/Grid unavailable: '+iceMessage+'.'+sourceWarning;
      else if(iceOk) status.textContent = 'ICE/Grid ready; Quant charts unavailable: '+qMessage;
      else status.textContent = 'Quant and ICE failed. Quant: '+qMessage+' | ICE: '+iceMessage;
    }
    if($('quantPill') && qOk){
      $('quantPill').textContent = qOk && iceOk ? 'READY' : 'PARTIAL';
      $('quantPill').className = 'status-pill '+(qOk&&iceOk?'good':'warn');
    }
    if(!automatic){
      if(qOk && iceOk) toast(full ? 'QUANT + ICE READY' : 'QUANT PREVIEW READY');
      else toast('PARTIAL RESULT · see Quant status');
    }
  }catch(e){
    const message = e?.message || String(e);
    if(status) status.textContent = 'Quant request failed: '+message;
    ['chartKalman','chartVol','chartPricing','chartStrat','chartWF'].forEach(id=>_quantPanelMessage(id, message, 'error', 'Quant request failed'));
    _quantPanelMessage('chartIce', message, 'error', 'ICE request failed');
    if($('quantPill')){ $('quantPill').textContent = 'ERROR'; $('quantPill').className = 'status-pill bad'; }
    if(!automatic) toast('ERROR: '+message);
  }finally{
    window.__quantRunInFlight = false;
  }
}
async function ensureQuantPreview(){
  if(window.__quantRunInFlight || window.__quantPreviewLoaded) return;
  await _runQuantPipeline({full:false, automatic:true});
}
async function runQuantLabFast(){ return _runQuantPipeline({full:false, automatic:false}); }
async function runUnifiedQuant(){ return _runQuantPipeline({full:true, automatic:false}); }

async function runIceOnly(){
  const status = $('quantStatus');
  if(status) status.textContent = 'Running ICE + Grid in-process…';
  try{
    const body = {
      symbol: ($('quantSymbol')?.value || '').trim() || undefined,
      timeframe: $('quantTf')?.value || 'M5', grid: $('quantGrid')?.value || 'SmartRegime', equity: 10000,
    };
    const data = await post('/api/ice/evaluate', body, { retries: 2 });
    if(data.error) throw Error(data.error);
    renderIcePanel(data); window.__quantIceLoaded = true;
    if(status) status.textContent = 'ICE backend: '+(data.native ? 'native C++' : 'python parity');
    toast('ICE READY');
  }catch(e){
    const message = e.message || String(e);
    window.__quantIceLoaded = false;
    _quantPanelMessage('chartIce', message, 'error', 'ICE / Grid unavailable');
    setTxt('qIceSig','—'); setTxt('qIceConf','ICE unavailable · retry after MT5 recovers');
    if(status) status.textContent = 'ICE / Grid unavailable: '+message;
    toast('ERROR: '+message);
  }
}

window.ensureQuantPreview = ensureQuantPreview;
window.runUnifiedQuant = runUnifiedQuant;
window.runQuantLab = runUnifiedQuant;
window.runQuantLabFast = runQuantLabFast;
window.runIceOnly = runIceOnly;
window.loadQuantDocs = runQuantLabFast;

async function loadQuantLibStatus(){
  const statusNode=$('quantLibStatus'), badge=$('quantLibBadge');
  if(statusNode) statusNode.textContent='Checking QuantLib Python binding…';
  try{
    const state=await get('/api/quantlib/status',{silent:true,retries:1});
    if(state && state.available){
      if(badge){badge.textContent='AVAILABLE · '+(state.quantlib_version||state.version||'QuantLib');badge.className='status-pill good';}
      if(statusNode) statusNode.textContent='QuantLib is ready. '+(state.capabilities||[]).join(' · ');
    }else{
      if(badge){badge.textContent='NOT INSTALLED';badge.className='status-pill warn';}
      if(statusNode) statusNode.textContent=(state?.error||'QuantLib Python binding is unavailable')+' Install from the project environment; restart the dashboard after installation.';
    }
    return state;
  }catch(error){
    const message=error?.message||String(error);
    if(badge){badge.textContent='STATUS ERROR';badge.className='status-pill bad';}
    if(statusNode) statusNode.textContent='Could not check QuantLib: '+message;
    return {available:false,error:message};
  }
}

async function runQuantLibOption(){
  const status=$('quantLibStatus'), metrics=$('quantLibMetrics'), resultNode=$('quantLibResult');
  const badge=$('quantLibBadge');
  if(status) status.textContent='Running QuantLib engine and 3×3 sensitivity grid…';
  if(metrics) metrics.innerHTML='';
  if(resultNode) resultNode.textContent='';
  if(badge){badge.textContent='CALCULATING';badge.className='status-pill warn';}
  try{
    const selectedSymbol=String($('quantSymbol')?.value||'').trim().toUpperCase();
    const lastData=window.__quantLastData||{};
    const lastSymbol=String(lastData.symbol||'').trim().toUpperCase();
    let spotRaw=String($('qlSpot')?.value||'').trim();
    if(!spotRaw && lastData.last_price!=null && (!selectedSymbol || !lastSymbol || selectedSymbol===lastSymbol)) spotRaw=String(lastData.last_price);
    const spot=Number(spotRaw);
    if(!spotRaw || !Number.isFinite(spot) || spot<=0){
      throw Error('Enter a positive spot price, or load verified candles for the selected symbol first so the engine can use its latest close.');
    }
    const strikeRaw=String($('qlStrike')?.value||'').trim();
    const strike=strikeRaw ? Number(strikeRaw) : spot;
    const marketRaw=String($('qlMarketPrice')?.value||'').trim();
    const payload={
      symbol:selectedSymbol || lastSymbol || null,
      spot,
      strike,
      days_to_expiry:Number($('qlDays')?.value||30),
      risk_free_rate:Number($('qlRiskFree')?.value||0),
      dividend_yield:Number($('qlDividend')?.value||0),
      volatility:Number($('qlVolatility')?.value||0.2),
      option_type:$('qlOptionType')?.value||'call',
      exercise:$('qlExercise')?.value||'european',
      engine:$('qlEngine')?.value||'auto',
      steps:Number($('qlSteps')?.value||200),
      market_price:marketRaw ? Number(marketRaw) : null
    };
    const data=await post('/api/quantlib/option',payload,{retries:1});
    const g=data.greeks||{};
    const cards=[
      ['MODEL PRICE',_qNum(data.price,6)],['DELTA',_qNum(g.delta,6)],['GAMMA',_qNum(g.gamma,6)],
      ['VEGA / 1.00 VOL',_qNum(g.vega,6)],['THETA / YEAR',_qNum(g.theta,6)],['RHO / 1.00 RATE',_qNum(g.rho,6)],
      ...(data.implied_volatility!=null?[['IMPLIED VOL',_qPct(data.implied_volatility)]]:[])
    ];
    if(metrics) metrics.innerHTML=cards.map(([label,value])=>`<div class="metric-card"><span>${esc(label)}</span><div class="metric-main">${esc(value)}</div></div>`).join('');
    const matrix=data.scenario_grid||[];
    const z=matrix.map(row=>(row||[]).map(cell=>Number(cell.price)));
    const x=['−10%','0%','+10%'], y=['Vol −5pp','Base vol','Vol +5pp'];
    _plotly('chartQuantLibScenarios',[{type:'heatmap',x,y,z,colorscale:'Viridis',colorbar:{title:'Model price'},hovertemplate:'Spot shock: %{x}<br>Vol scenario: %{y}<br>Price: %{z:.6f}<extra></extra>'}],{
      title:'QuantLib repricing: spot × volatility',xaxis:{title:'Spot shock'},yaxis:{title:'Volatility shock'},margin:{l:75,r:25,t:40,b:50},height:300
    });
    if(badge){badge.textContent='READY · '+(data.library_version||'QuantLib');badge.className='status-pill good';}
    if(status) status.textContent=`${data.engine} · ${data.option_type.toUpperCase()} · ${data.exercise} · valuation ${data.valuation_date}, expiry ${data.expiry_date}`;
    if(resultNode) resultNode.textContent=(data.disclaimer||'')+(data.implied_volatility_note?' · '+data.implied_volatility_note:'')+' Greeks are model sensitivities, not direction or win-probability estimates.';
    toast('QUANTLIB PRICING READY');
    return data;
  }catch(error){
    const message=error?.message||String(error);
    if(badge){badge.textContent='UNAVAILABLE';badge.className='status-pill warn';}
    if(status) status.textContent='QuantLib pricing unavailable: '+message;
    if(resultNode) resultNode.textContent=message;
    _quantPanelMessage('chartQuantLibScenarios',message,'error','QuantLib calculation unavailable');
    toast('QUANTLIB: '+message);
    return null;
  }
}
window.loadQuantLibStatus=loadQuantLibStatus;
window.runQuantLibOption=runQuantLibOption;

async function runQuantPortfolio(){
  const status=$('quantPortfolioStatus'), body=$('quantPortfolioBody'), metrics=$('quantPortfolioMetrics'), riskMetrics=$('quantPortfolioRiskMetrics');
  const symbols=String($('quantPortfolioSymbols')?.value||'').split(',').map(s=>s.trim().toUpperCase()).filter(Boolean);
  if(status) status.textContent='Fetching exact MT5 D1 history, aligning simple returns, and estimating constrained risk…';
  if(body) body.innerHTML='<tr><td colspan="8" class="muted">Loading portfolio data…</td></tr>';
  try{
    const result=await post('/api/quant/portfolio',{
      symbols,
      bars:Number($('quantPortfolioBars')?.value||400),
      capital:Number($('quantPortfolioCapital')?.value||10000),
      risk_free_rate:Number($('quantPortfolioRf')?.value??0.02),
      max_weight:Number($('quantPortfolioMaxWeight')?.value||0.60),
      transaction_cost_bps:Number($('quantPortfolioCostBps')?.value||0)
    },{retries:2});
    window.__portfolioOptimizationResult = result;
    const ms=result.max_sharpe||{}, mv=result.min_variance||{}, rp=result.risk_parity||{}, msRisk=ms.risk||{}, mvRisk=mv.risk||{}, rpRisk=rp.risk||{};
    const portfolioRiskNode=$('portfolioRiskResearchMetrics');
    if(portfolioRiskNode){
      portfolioRiskNode.innerHTML=[
        ['HISTORICAL VaR 95% (1D)',_qPct(msRisk.var_95_1d)],['HISTORICAL CVaR 95% (1D)',_qPct(msRisk.cvar_95_1d)],
        ['HISTORICAL CVaR 99% (1D)',_qPct(msRisk.cvar_99_1d)],['MAX DRAWDOWN',_qPct(msRisk.max_drawdown_pct)],
        ['DOWNSIDE DEVIATION',_qPct(msRisk.downside_deviation_ann)],['EFFECTIVE ASSETS',_qNum(msRisk.effective_assets,2)],
        ['WORST 5-DAY WINDOW',_qPct(msRisk.worst_5d_return)],['WORST 20-DAY WINDOW',_qPct(msRisk.worst_20d_return)],
        ['REBALANCE COST ESTIMATE',fmt(msRisk.estimated_one_time_rebalance_cost,2)],['COVARIANCE MODEL',result.covariance_estimator?.method||'—']
      ].map(([label,value])=>`<div class="metric-card"><span>${esc(label)}</span><div class="metric-main">${esc(value)}</div></div>`).join('');
    }
    const allocations=new Map((ms.allocation||[]).map(x=>[x.symbol,x.dollars]));
    const symbolsOut=result.symbols||[];
    if(body) body.innerHTML=symbolsOut.map((sym,i)=>`<tr><td><b>${esc(sym)}</b></td><td>${_qPct(ms.weights?.[sym])}</td><td>${_qPct((msRisk.risk_contribution_pct||[])[i])}</td><td>${_qPct(mv.weights?.[sym])}</td><td>${_qPct((mvRisk.risk_contribution_pct||[])[i])}</td><td>${_qPct(rp.weights?.[sym])}</td><td>${_qPct((rpRisk.risk_contribution_pct||[])[i])}</td><td>${fmt(allocations.get(sym),2)}</td></tr>`).join('')||'<tr><td colspan="8">No allocation rows returned.</td></tr>';
    if(metrics) metrics.innerHTML=[
      ['MAX-SHARPE EXPECTED RETURN',_qPct(ms.return_ann)],['MAX-SHARPE VOLATILITY',_qPct(ms.vol_ann)],['MAX-SHARPE RATIO',_qNum(ms.sharpe,2)],
      ['MIN-VARIANCE EXPECTED RETURN',_qPct(mv.return_ann)],['MIN-VARIANCE VOLATILITY',_qPct(mv.vol_ann)],['ALIGNED DAILY RETURNS',String(result.aligned_returns||0)]
    ].map(([label,value])=>`<div class="metric-card"><span>${esc(label)}</span><div class="metric-main">${esc(value)}</div></div>`).join('');
    if(riskMetrics) riskMetrics.innerHTML=[
      ['MAX-SHARPE 1D VaR 95%',_qPct(msRisk.var_95_1d)],['MAX-SHARPE 1D CVaR 95%',_qPct(msRisk.cvar_95_1d)],
      ['MAX-SHARPE MAX DRAWDOWN',_qPct(msRisk.max_drawdown_pct)],['MAX-SHARPE EFFECTIVE ASSETS',_qNum(msRisk.effective_assets,2)],
      ['MIN-VARIANCE 1D CVaR 95%',_qPct(mvRisk.cvar_95_1d)],['MIN-VARIANCE MAX DRAWDOWN',_qPct(mvRisk.max_drawdown_pct)],
      ['MAX-SHARPE ONE-TIME REBALANCE COST',fmt(msRisk.estimated_one_time_rebalance_cost,2)],['FIRST-YEAR RETURN AFTER COST',_qPct(msRisk.first_year_return_after_one_time_cost)],['COST / GROSS PROFIT RATIO',_qNum(msRisk.costs_to_gross_profit_ratio,2)],['RISK-PARITY MAX DRAWDOWN',_qPct(rpRisk.max_drawdown_pct)]
    ].map(([label,value])=>`<div class="metric-card"><span>${esc(label)}</span><div class="metric-main">${esc(value)}</div></div>`).join('');
    const traces=[
      {type:'bar',x:symbolsOut,y:symbolsOut.map(x=>100*Number(ms.weights?.[x]||0)),name:'Max Sharpe %'},
      {type:'bar',x:symbolsOut,y:symbolsOut.map(x=>100*Number(mv.weights?.[x]||0)),name:'Min variance %'},
      {type:'bar',x:symbolsOut,y:symbolsOut.map(x=>100*Number(rp.weights?.[x]||0)),name:'Risk parity %'},
      {type:'bar',x:symbolsOut,y:symbolsOut.map(x=>100*Number(result.inverse_volatility?.weights?.[x]||0)),name:'Inverse volatility %'},
      {type:'bar',x:symbolsOut,y:symbolsOut.map(x=>100*Number(result.equal_weight?.weights?.[x]||0)),name:'Equal weight %'}
    ];
    _plotly('chartPortfolio',traces,{barmode:'group',yaxis:{title:'Allocation (%)',ticksuffix:'%'},margin:{l:48,r:10,t:20,b:54},height:300});
    const frontier=result.frontier||[];
    const methodPoints=['max_sharpe','min_variance','risk_parity','inverse_volatility','equal_weight'].filter(k=>result[k]).map(k=>({name:k.replaceAll('_',' '),x:Number(result[k].vol_ann||0)*100,y:Number(result[k].return_ann||0)*100}));
    if(frontier.length){
      _plotly('chartPortfolioFrontier',[
        {type:'scatter',mode:'lines+markers',x:frontier.map(p=>Number(p.vol||0)*100),y:frontier.map(p=>Number(p.return||0)*100),name:'Feasible frontier',line:{color:'#4b9af5'},hovertemplate:'Volatility %{x:.2f}%<br>Annualized return %{y:.2f}%<extra></extra>'},
        {type:'scatter',mode:'markers+text',x:methodPoints.map(p=>p.x),y:methodPoints.map(p=>p.y),text:methodPoints.map(p=>p.name),textposition:'top center',name:'Compared allocations',marker:{size:9,color:'#39c6a7'},hovertemplate:'%{text}<br>Volatility %{x:.2f}%<br>Annualized return %{y:.2f}%<extra></extra>'}
      ],{title:'Estimated risk / return frontier',xaxis:{title:'Annualized volatility (%)'},yaxis:{title:'Annualized arithmetic return (%)',ticksuffix:'%'},margin:{l:65,r:20,t:42,b:60},height:340});
    }else _quantPanelMessage('chartPortfolioFrontier','The allocator did not return frontier points for this data set.','warning','Frontier unavailable');
    const corr=result.correlation||{};
    if(Array.isArray(corr.matrix)&&corr.matrix.length){
      _plotly('chartPortfolioCorr',[{type:'heatmap',x:corr.symbols||symbolsOut,y:corr.symbols||symbolsOut,z:corr.matrix,zmin:-1,zmax:1,colorscale:'RdBu',reversescale:true,colorbar:{title:'Correlation'},hovertemplate:'%{y} × %{x}<br>Correlation: %{z:.3f}<extra></extra>'}],{margin:{l:90,r:18,t:14,b:65},height:320});
    }else{
      _quantPanelMessage('chartPortfolioCorr','The API did not return a usable correlation matrix.','warning','Correlation unavailable');
    }
    const missing=Object.keys(result.missing_symbols||{});
    const pair=(corr.highest_absolute_pairs||[])[0];
    const capNote=result.max_weight_effective!==result.max_weight_requested?` Requested cap adjusted to ${_qPct(result.max_weight_effective)} to remain feasible. `:'';
    const pairNote=pair?` Largest absolute pair correlation: ${pair.a}/${pair.b} ${_qNum(pair.correlation,2)}. `:'';
    if(status) status.textContent=`${result.data_source||'MT5'} · ${result.aligned_returns} aligned daily simple returns · ${result.date_start} to ${result.date_end}. Covariance: ${result.covariance_estimator?.method||'—'}; shrinkage ${_qNum(result.covariance_estimator?.shrinkage,3)}. ${capNote}${pairNote}${missing.length?`Skipped: ${missing.join(', ')}. `:''}${result.disclaimer||''}`;
    toast('PORTFOLIO RISK ANALYSIS READY');
  }catch(error){
    const message=error?.message||String(error);
    if(body) body.innerHTML=`<tr><td colspan="8" class="error-text">${esc(message)}</td></tr>`;
    if(metrics) metrics.innerHTML='';
    if(riskMetrics) riskMetrics.innerHTML='';
    _quantPanelMessage('chartPortfolio',message,'error','Portfolio unavailable');
    _quantPanelMessage('chartPortfolioCorr',message,'error','Correlation unavailable');
    if(status) status.textContent='Portfolio unavailable: '+message;
  }
}
window.runQuantPortfolio=runQuantPortfolio;

/* ── Integrated Finance toolkit: Research Lab and Portfolio workflows ─ */
window.__financeToolkitStatus = null;
window.__financeLastIndicatorResult = null;
window.__financeLastStatsResult = null;
window.__financeLastForecastResult = null;
window.__financeLastStrategyResult = null;
window.__financeLastPortfolioSimulation = null;

async function loadFinanceToolkitStatus(force=false){
  const status=$('financeToolkitStatus'), badge=$('financeToolkitBadge');
  if(!status&&!badge)return null;
  if(status)status.textContent='Checking installed quantitative research capabilities…';
  try{
    const data=await get('/api/finance/status',{force,retries:1,silent:true});
    window.__financeToolkitStatus=data;
    const featureCount=Object.values(data.features||{}).filter(Boolean).length;
    const total=Object.keys(data.features||{}).length;
    if(badge){badge.textContent=data.ok?`TOOLKIT READY · ${featureCount}/${total} CAPABILITIES`:'TOOLKIT INCOMPLETE';badge.className='status-pill '+(data.ok?'good':'warn');}
    if(status){
      const deps=data.dependencies||{};
      const missing=Object.keys(deps).filter(k=>!deps[k]);
      status.textContent=(data.note||'')+(missing.length?` Optional or required modules not installed: ${missing.join(', ')}.`:' All listed optional modules are present.');
    }
    return data;
  }catch(e){
    if(badge){badge.textContent='STATUS UNAVAILABLE';badge.className='status-pill warn';}
    if(status)status.textContent=`Toolkit status request failed: ${e.message}`;
    return null;
  }
}
window.loadFinanceToolkitStatus=loadFinanceToolkitStatus;

function setFinanceResearchTab(tab){
  const panel=document.querySelector(`#financeResearchPanels [data-finance-panel="${tab}"]`);
  if(!panel)return;
  document.querySelectorAll('#financeResearchTabs [data-finance-tab]').forEach(btn=>{
    const active=btn.dataset.financeTab===tab;
    btn.classList.toggle('active',active);btn.setAttribute('aria-selected',active?'true':'false');
  });
  document.querySelectorAll('#financeResearchPanels [data-finance-panel]').forEach(node=>{
    const active=node.dataset.financePanel===tab;node.classList.toggle('active',active);node.hidden=!active;
  });
  if(tab==='statistics' || tab==='forecast' || tab==='strategy' || tab==='screen' || tab==='indicators' || tab==='company') loadFinanceToolkitStatus();
}
window.setFinanceResearchTab=setFinanceResearchTab;
document.addEventListener('click',ev=>{const btn=ev.target.closest('[data-finance-tab]');if(btn)setFinanceResearchTab(btn.dataset.financeTab);});

function financeMetricCards(targetId, items){
  const node=$(targetId);if(!node)return;
  node.innerHTML=(items||[]).map(([name,value,note])=>`<div class="metric-card"><span>${esc(name)}</span><div class="metric-main">${esc(value)}</div>${note?`<small>${esc(note)}</small>`:''}</div>`).join('');
}
function financeNum(value,digits=4){return value===null||value===undefined||!Number.isFinite(Number(value))?'—':fmt(Number(value),digits);}
function financeRequestBase(prefix){
  const symbol=$(prefix+'Symbol')?.value?.trim().toUpperCase();
  const timeframe=$(prefix+'Tf')?.value||'D1';
  return {symbol,timeframe};
}

async function runFinanceIndicators(){
  const status=$('financeIndicatorStatus'); if(status)status.textContent='Requesting verified MT5 candles and calculating the indicator stack…';
  if($('financeIndicatorTable'))$('financeIndicatorTable').innerHTML='<tr><td colspan="4" class="muted">Calculation running…</td></tr>';
  try{
    const params=financeRequestBase('financeIndicator');
    const data=await post('/api/finance/indicators',{...params,bars:Number($('financeIndicatorBars')?.value||500)},{retries:2,silent:true,region:'financeIndicatorTable'});
    window.__financeLastIndicatorResult=data;
    financeMetricCards('financeIndicatorMetrics',[
      ['SYMBOL / TF',`${data.symbol} · ${data.timeframe}`,`${data.bars} verified bars`],['LAST CLOSE',financeNum(data.last_close,6),data.last_bar_utc||''],
      ['TREND',data.trend,data.trend_strength],['MOMENTUM',data.momentum_state,data.breakout_state],['SUPERTREND',data.supertrend_direction,'ATR-based line'],['VOLUME',data.volume_available?'AVAILABLE':'MISSING',data.volume_basis||'']
    ]);
    if($('financeIndicatorTable'))$('financeIndicatorTable').innerHTML=(data.metrics||[]).map(m=>`<tr><td>${esc(m.label)}</td><td>${esc(financeNum(m.value,6))}</td><td>${esc(m.unit||'')}</td><td>${esc(m.interpretation||'')}</td></tr>`).join('')||'<tr><td colspan="4">No indicators returned.</td></tr>';
    if($('financeIndicatorNotes'))$('financeIndicatorNotes').textContent=data.methodology_note||'';
    if(status)status.textContent=`Calculated from ${data.data_source||'broker history'} · ${data.last_bar_utc||'last bar time unknown'}.`;
  }catch(e){if(status)status.textContent=`Indicator calculation failed: ${e.message}`;if($('financeIndicatorTable'))$('financeIndicatorTable').innerHTML=`<tr><td colspan="4" class="muted">${esc(e.message)}</td></tr>`;}
}
window.runFinanceIndicators=runFinanceIndicators;

async function runFinanceScreen(){
  const status=$('financeScreenStatus');if(status)status.textContent='Scanning entered symbols one at a time with bounded candle requests…';
  if($('financeScreenBody'))$('financeScreenBody').innerHTML='<tr><td colspan="8" class="muted">Screening running…</td></tr>';
  try{
    const symbols=String($('financeScreenSymbols')?.value||'').split(',').map(x=>x.trim().toUpperCase()).filter(Boolean);
    const data=await post('/api/finance/screen',{symbols,timeframe:$('financeScreenTf')?.value||'H1',bars:Number($('financeScreenBars')?.value||300)},{retries:1,silent:true,region:'financeScreenBody'});
    if($('financeScreenBody'))$('financeScreenBody').innerHTML=(data.results||[]).map(r=>`<tr><td><b>${esc(r.symbol)}</b></td><td>${esc(financeNum(r.last_close,6))}</td><td>${esc(r.trend)}</td><td>${esc(financeNum(r.rsi14,2))}</td><td>${esc(financeNum(r.adx14,2))}</td><td>${esc(financeNum(r.natr14_pct,3))}</td><td>${esc(r.breakout)}</td><td><span class="status-pill neutral">${esc(r.screen_label)}</span></td></tr>`).join('')||'<tr><td colspan="8" class="muted">No symbols returned usable history.</td></tr>';
    if($('financeScreenFailures'))$('financeScreenFailures').innerHTML=Object.entries(data.failures||{}).map(([sym,msg])=>`<div class="log error"><b>${esc(sym)}</b> · ${esc(msg)}</div>`).join('');
    if(status)status.textContent=`Screened ${data.scanned||0} symbols; ${data.failed||0} history failures. Results are descriptive candidates, not instructions.`;
  }catch(e){if(status)status.textContent=`Screen failed: ${e.message}`;if($('financeScreenBody'))$('financeScreenBody').innerHTML=`<tr><td colspan="8" class="muted">${esc(e.message)}</td></tr>`;}
}
window.runFinanceScreen=runFinanceScreen;

async function runFinanceStatistics(){
  const status=$('financeStatsStatus');if(status)status.textContent='Aligning exact broker price histories; computing correlation, PCA, clusters and cointegration…';
  try{
    const symbols=String($('financeStatsSymbols')?.value||'').split(',').map(x=>x.trim().toUpperCase()).filter(Boolean);
    const data=await post('/api/finance/statistics',{symbols,timeframe:$('financeStatsTf')?.value||'D1',bars:Number($('financeStatsBars')?.value||500)},{retries:1,silent:true,region:'financeStatsMetrics'});
    window.__financeLastStatsResult=data;
    financeMetricCards('financeStatsMetrics',[
      ['ALIGNED PRICES',fmt(data.aligned_price_observations||0,0),`${data.return_observations||0} aligned returns`],
      ['INSTRUMENTS',fmt((data.symbols||[]).length,0),(data.symbols||[]).join(', ')],
      ['PCA PC1',financeNum(data.pca_explained_variance?.pc1,3),'Explained variance ratio'],
      ['COINTEGRATION PAIRS',(data.cointegration_pairs||[]).length,'Holm-adjusted pair tests']
    ]);
    const corr=data.correlation_matrix||{}, names=data.symbols||[];
    if($('financeCorrHead'))$('financeCorrHead').innerHTML=`<tr><th>Symbol</th>${names.map(n=>`<th>${esc(n)}</th>`).join('')}</tr>`;
    if($('financeCorrBody'))$('financeCorrBody').innerHTML=names.map(n=>`<tr><th>${esc(n)}</th>${names.map(m=>`<td>${esc(financeNum(corr[m]?.[n]??corr[n]?.[m],3))}</td>`).join('')}</tr>`).join('');
    if($('financeStatsStructure'))$('financeStatsStructure').innerHTML=`<div class="log"><b>PCA explained variance</b><div class="muted">${esc(JSON.stringify(data.pca_explained_variance||{}))}</div></div><div class="log"><b>Asset clusters</b><div class="muted">${esc(JSON.stringify(data.asset_clusters||{}))}</div></div><div class="log"><b>Strongest correlations</b><div class="muted">${esc((data.strongest_correlations||[]).slice(0,5).map(x=>`${x.first}/${x.second}: ${Number(x.correlation).toFixed(3)}`).join(' · ')||'No pairs available')}</div></div>`;
    if($('financeCointegrationBody'))$('financeCointegrationBody').innerHTML=(data.cointegration_pairs||[]).map(x=>`<tr><td>${esc(x.first)}</td><td>${esc(x.second)}</td><td>${esc(financeNum(x.statistic,3))}</td><td>${esc(financeNum(x.p_value,4))}</td><td>${esc(financeNum(x.adjusted_p_value,4))}</td></tr>`).join('')||'<tr><td colspan="5" class="muted">No pair results. Check history length and statsmodels capability.</td></tr>';
    if($('financeStatsNotes'))$('financeStatsNotes').textContent=[data.methodology_note,data.cointegration_note,data.factor_model_note,Object.keys(data.failures||{}).length?`Failed histories: ${JSON.stringify(data.failures)}`:'' ].filter(Boolean).join(' ');
    if(status)status.textContent=`Research complete on ${names.length} aligned symbols. Validate any pair on a separate out-of-sample period.`;
  }catch(e){if(status)status.textContent=`Statistical research failed: ${e.message}`;if($('financeStatsNotes'))$('financeStatsNotes').textContent=e.message;}
}
window.runFinanceStatistics=runFinanceStatistics;

async function runFinanceForecast(){
  const status=$('financeForecastStatus');if(status)status.textContent='Fitting bounded model on chronological training window and evaluating the held-out sample…';
  try{
    const data=await post('/api/finance/forecast',{symbol:$('financeForecastSymbol')?.value?.trim().toUpperCase(),timeframe:$('financeForecastTf')?.value||'D1',bars:Number($('financeForecastBars')?.value||800),model:$('financeForecastModel')?.value||'ridge',train_fraction:Number($('financeForecastTrain')?.value||.75)},{retries:1,silent:true,region:'financeForecastMetrics'});
    window.__financeLastForecastResult=data;
    const m=data.return_regression_metrics||{}, mdl=m[data.model]||{}, baseline=m.zero_return||{};
    financeMetricCards('financeForecastMetrics',[
      ['HOLDOUT BARS',fmt(data.holdout_observations||0,0),`${data.symbol} · ${data.timeframe}`],
      ['MODEL MAE',financeNum(mdl.mae,6),'Fractional return units'],['ZERO-RETURN MAE',financeNum(baseline.mae,6),'Baseline'],
      ['MODEL RMSE',financeNum(mdl.rmse,6),'Fractional return units'],['ZERO-RETURN RMSE',financeNum(baseline.rmse,6),'Baseline']
    ]);
    const latest=data.latest_experimental_forecast||{};
    const dir=data.direction_metrics||{};
    const dirText=Object.entries(dir).map(([k,v])=>`${k}: Brier ${financeNum(v.brier,4)}, log-loss ${financeNum(v.log_loss,4)}`).join(' · ');
    if($('financeForecastDetails'))$('financeForecastDetails').innerHTML=`<div class="log"><b>Chronological split</b><div class="muted">Training end: ${esc(data.training_end||'—')} · Test starts: ${esc(data.test_start||'—')}</div></div><div class="log"><b>Latest experimental output</b><div class="muted">${esc(latest.unavailable||`Predicted fractional return: ${financeNum(latest.predicted_return,6)} · implied price: ${financeNum(latest.implied_price,6)} · model: ${latest.model||data.model}`)}</div></div><div class="log"><b>Direction classifier versus training-prior baseline</b><div class="muted">${esc(dirText||data.direction_note||'Direction metrics unavailable for this sample.')}</div></div>`;
    if($('financeForecastNotes'))$('financeForecastNotes').textContent=data.methodology_note||'';
    if(status)status.textContent='Holdout complete. A fitted model is not evidence of alpha unless it outperforms baselines across robust unseen periods.';
  }catch(e){if(status)status.textContent=`Forecast validation failed: ${e.message}`;if($('financeForecastDetails'))$('financeForecastDetails').innerHTML=`<div class="log error">${esc(e.message)}</div>`;}
}
window.runFinanceForecast=runFinanceForecast;

async function runFinanceStrategyTest(){
  const status=$('financeStrategyStatus');if(status)status.textContent='Running research engine with close-time targets and next-open fills…';
  try{
    const data=await post('/api/finance/strategy-test',{symbol:$('financeStrategySymbol')?.value?.trim().toUpperCase(),timeframe:$('financeStrategyTf')?.value||'M5',bars:Number($('financeStrategyBars')?.value||2000),strategy:$('financeStrategyMethod')?.value||'ma_crossover',fast:Number($('financeStrategyFast')?.value||20),slow:Number($('financeStrategySlow')?.value||50),initial_cash:Number($('financeStrategyCapital')?.value||10000),commission_bps:Number($('financeStrategyCommission')?.value||0),slippage_bps:Number($('financeStrategySlippage')?.value||0)},{retries:1,silent:true,region:'financeStrategyMetrics'});
    window.__financeLastStrategyResult=data;
    const m=data.metrics||{};
    financeMetricCards('financeStrategyMetrics',[
      ['FINAL EQUITY',portfolioMoney(data.final_equity,''),`${data.symbol} · ${data.timeframe}`],['TOTAL RETURN',pct(data.total_return),`${data.bars} bars`],
      ['MAX DRAWDOWN',pct(data.maximum_drawdown), 'Historical research backtest'],['SHARPE',financeNum(m.sharpe,3),'Annualized per timeframe convention'],
      ['FILL COUNT',fmt(data.fill_count||0,0),`Commission ${data.commission_bps_per_side} bps + slippage ${data.slippage_bps_per_side} bps/side`],['BENCHMARK RETURN',pct(m.benchmark_return),'Equal-dollar buy-and-hold baseline']
    ]);
    if($('financeStrategyFills'))$('financeStrategyFills').innerHTML=(data.last_fills||[]).map(x=>`<tr><td>${esc(x.date||'—')}</td><td>${esc(x.signal_date||'—')}</td><td>${esc(x.asset||'—')}</td><td>${esc(financeNum(x.quantity,5))}</td><td>${esc(financeNum(x.price,6))}</td><td>${esc(financeNum(x.commission,4))}</td><td>${esc(x.phase||'—')}</td></tr>`).join('')||'<tr><td colspan="7" class="muted">No fills under this strategy and historical window.</td></tr>';
    if($('financeStrategyNotes'))$('financeStrategyNotes').textContent=[data.engine,...(data.limitations||[])].join(' ');
    if(status)status.textContent='Research test complete. This fractional-unit engine does not reproduce broker lots, margin, swaps or executable MT5 P&L.';
  }catch(e){if(status)status.textContent=`Strategy test failed: ${e.message}`;if($('financeStrategyNotes'))$('financeStrategyNotes').textContent=e.message;}
}

window.runFinanceStrategyTest=runFinanceStrategyTest;

function financePublicTicker(){return String($('financeCompanyTicker')?.value||'').trim().toUpperCase();}
function financeSourceLink(raw){const url=String(raw||'');return /^https?:\/\//i.test(url)?`<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">Source</a>`:'—';}
function financePretty(value){
  if(value===null||value===undefined||value===''||!Number.isFinite(Number(value))&&typeof value!=='string')return '—';
  if(typeof value==='number'||(typeof value==='string'&&value.trim()!==''&&Number.isFinite(Number(value))))return fmt(Number(value),4);
  return String(value);
}
async function runFinanceCompanySnapshot(){
  const status=$('financeCompanyStatus'), ticker=financePublicTicker();
  if(status)status.textContent=`Requesting public company snapshot for ${ticker||'(empty ticker)'}…`;
  try{
    const data=await post('/api/finance/company-snapshot',{ticker},{retries:1,silent:true,region:'financeCompanyBody'});
    const f=data.fields||{};window.__financeCompanySnapshot=data;
    const cards=[['PRICE',financePretty(f.price),'Provider current snapshot'],['MARKET CAP',financePretty(f.market_cap),'Provider currency units'],['TRAILING P/E',financePretty(f.pe),'Not a standalone valuation'],['ROE',pct(f.roe),'Provider ratio'],['REVENUE GROWTH',pct(f.revenue_growth),'Provider ratio'],['FREE CASH FLOW',financePretty(f.free_cash_flow),'Provider reported']];
    financeMetricCards('financeCompanyMetrics',cards);
    const labels={name:'Company',price:'Price',market_cap:'Market capitalization',pe:'Trailing P/E',roe:'Return on equity',roa:'Return on assets',revenue_growth:'Revenue growth',earnings_growth:'Earnings growth',gross_margin:'Gross margin',annual_dividend:'Annual dividend',dividend_yield:'Dividend yield',debt:'Total debt',cash:'Total cash',free_cash_flow:'Free cash flow',shares:'Shares outstanding',analyst_rating:'Analyst rating'};
    const entries=Object.entries(f).filter(([k,v])=>v!==null&&v!==undefined&&v!=='');
    if($('financeCompanyBody'))$('financeCompanyBody').innerHTML=entries.map(([k,v])=>`<tr><td>${esc(labels[k]||k.replaceAll('_',' '))}</td><td>${esc(financePretty(v))}</td></tr>`).join('')||'<tr><td colspan="2" class="muted">Provider returned no usable fields.</td></tr>';
    if(status)status.textContent=`Loaded ${data.ticker} from ${data.provider} at ${data.retrieved_at}. Current snapshot; not point-in-time backtest data.`;
  }catch(e){if(status)status.textContent=`Company snapshot unavailable: ${e.message}`;if($('financeCompanyBody'))$('financeCompanyBody').innerHTML=`<tr><td colspan="2" class="muted">${esc(e.message)}</td></tr>`;}
}
window.runFinanceCompanySnapshot=runFinanceCompanySnapshot;

async function runFinanceCompanyStatements(){
  const ticker=financePublicTicker(), status=$('financeCompanyStatus');
  if(status)status.textContent=`Requesting statement history for ${ticker}…`;
  try{
    const kind=$('financeCompanyStatementKind')?.value||'income', frequency=$('financeCompanyStatementFrequency')?.value||'annual';
    const data=await post('/api/finance/company-statements',{ticker,kind,frequency},{retries:1,silent:true,region:'financeCompanyStatementBody'});
    if($('financeCompanyStatementTitle'))$('financeCompanyStatementTitle').textContent=`${data.frequency} ${data.kind} statement — ${data.ticker}`;
    const periods=data.periods||[], metrics=data.metrics||[];
    if($('financeCompanyStatementHead'))$('financeCompanyStatementHead').innerHTML=`<tr><th>Metric</th>${periods.map(x=>`<th>${esc(x)}</th>`).join('')}</tr>`;
    if($('financeCompanyStatementBody'))$('financeCompanyStatementBody').innerHTML=metrics.map(row=>`<tr><td>${esc(row.metric)}</td>${(row.values||[]).map(v=>`<td>${esc(financePretty(v))}</td>`).join('')}</tr>`).join('')||`<tr><td colspan="${periods.length+1}" class="muted">No statement rows were returned.</td></tr>`;
    if($('financeCompanyStatementNote'))$('financeCompanyStatementNote').textContent=data.note||'';
    if(status)status.textContent=`Loaded ${metrics.length} statement metrics across ${periods.length} periods from ${data.provider}.`;
  }catch(e){if(status)status.textContent=`Statement unavailable: ${e.message}`;if($('financeCompanyStatementBody'))$('financeCompanyStatementBody').innerHTML=`<tr><td class="muted">${esc(e.message)}</td></tr>`;}
}
window.runFinanceCompanyStatements=runFinanceCompanyStatements;

async function runFinanceCompanyNews(){
  const ticker=financePublicTicker(), status=$('financeCompanyStatus');
  if(status)status.textContent=`Requesting headline data and scoring language for ${ticker}…`;
  try{
    const data=await post('/api/finance/company-news',{ticker,limit:20},{retries:1,silent:true,region:'financeCompanyNewsBody'});
    financeMetricCards('financeCompanySentimentMetrics',[["MEAN HEADLINE SCORE",financeNum(data.mean_compound,3),"VADER compound score"],["HEADLINES SCORED",fmt(data.count||0,0),"Returned provider headlines"],["AGGREGATE LABEL",data.aggregate_label||'—',"Descriptive text sentiment only"]]);
    const badge=$('financeCompanySentimentBadge');if(badge){badge.textContent=data.aggregate_label||'COMPLETE';badge.className='status-pill neutral';}
    if($('financeCompanyNewsBody'))$('financeCompanyNewsBody').innerHTML=(data.headlines||[]).map(row=>`<tr><td>${esc(row.published||'—')}</td><td>${esc(row.title||'')}</td><td>${esc(row.label||'—')}</td><td>${financeNum(row.compound,3)}</td><td>${financeSourceLink(row.url)}</td></tr>`).join('')||'<tr><td colspan="5" class="muted">No headlines returned.</td></tr>';
    if($('financeCompanyNewsNote'))$('financeCompanyNewsNote').textContent=data.warning||'';
    if(status)status.textContent=`Scored ${data.count} headlines for ${data.ticker}; no trading decision was changed.`;
  }catch(e){const badge=$('financeCompanySentimentBadge');if(badge){badge.textContent='UNAVAILABLE';badge.className='status-pill warn';}if(status)status.textContent=`Headline sentiment unavailable: ${e.message}`;if($('financeCompanyNewsBody'))$('financeCompanyNewsBody').innerHTML=`<tr><td colspan="5" class="muted">${esc(e.message)}</td></tr>`;}
}
window.runFinanceCompanyNews=runFinanceCompanyNews;

async function runFinanceDCF(){
  const status=$('financeDcfStatus');if(status)status.textContent='Calculating DCF from your explicit projections…';
  try{
    const flows=String($('financeDcfFlows')?.value||'').split(',').map(x=>Number(x.trim()));
    if(!flows.length||flows.some(x=>!Number.isFinite(x)))throw new Error('Enter comma-separated finite annual cash-flow projections.');
    const data=await post('/api/finance/dcf',{cash_flows:flows,discount_rate:Number($('financeDcfRate')?.value),terminal_growth:Number($('financeDcfGrowth')?.value),cash:Number($('financeDcfCash')?.value||0),debt:Number($('financeDcfDebt')?.value||0),shares:Number($('financeDcfShares')?.value||1)},{retries:1,silent:true,region:'financeDcfSensitivityBody'});
    financeMetricCards('financeDcfMetrics',[["ENTERPRISE VALUE",financePretty(data.enterprise_value),'DCF model output'],["EQUITY VALUE",financePretty(data.equity_value),'EV + cash − debt'],["VALUE / SHARE",financePretty(data.value_per_share),'In projection currency units'],["TERMINAL PV",financePretty(data.terminal_present_value),'Sensitivity to terminal assumptions']]);
    if($('financeDcfSensitivityBody'))$('financeDcfSensitivityBody').innerHTML=(data.sensitivity||[]).map(r=>`<tr><td>${pct(r.discount_rate)}</td><td>${pct(r.terminal_growth)}</td><td>${esc(financePretty(r.enterprise_value))}</td><td>${esc(financePretty(r.equity_value))}</td><td>${esc(financePretty(r.value_per_share))}</td></tr>`).join('')||'<tr><td colspan="5" class="muted">No valid sensitivity scenarios.</td></tr>';
    if($('financeDcfNotes'))$('financeDcfNotes').textContent=data.warning||'';
    if(status)status.textContent=`DCF calculated for ${data.assumptions.annual_cash_flows.length} annual projections. These are user assumptions, not observed market forecasts.`;
  }catch(e){if(status)status.textContent=`DCF unavailable: ${e.message}`;if($('financeDcfSensitivityBody'))$('financeDcfSensitivityBody').innerHTML=`<tr><td colspan="5" class="muted">${esc(e.message)}</td></tr>`;}
}
window.runFinanceDCF=runFinanceDCF;

async function runFinancePortfolioSimulation(){
  const status=$('portfolioSimulationStatus');if(status)status.textContent='Fetching verified daily closes and estimating requested scenario paths…';
  const node=$('portfolioSimulationChart');if(node)node.innerHTML='<div class="quant-chart-state"><strong>Scenario running</strong><span>Waiting for market data and Monte Carlo calculation.</span></div>';
  try{
    const symbols=String($('portfolioSimulationSymbols')?.value||'').split(',').map(x=>x.trim().toUpperCase()).filter(Boolean);
    const data=await post('/api/finance/portfolio-simulation',{symbols,method:$('portfolioSimulationMethod')?.value||'monte_carlo',bars:Number($('portfolioSimulationBars')?.value||500),capital:Number($('portfolioSimulationCapital')?.value||10000),years:Number($('portfolioSimulationYears')?.value||1),paths:Number($('portfolioSimulationPaths')?.value||500),installments:Number($('portfolioSimulationInstallments')?.value||12)},{retries:1,silent:true,region:'portfolioSimulationMetrics'});
    window.__financeLastPortfolioSimulation=data;
    if(data.method==='lump_sum_vs_dca'){
      financeMetricCards('portfolioSimulationMetrics',[
        ['FINAL LUMP SUM',portfolioMoney(data.final_lump_sum,''),'Historical observed path'],['FINAL DCA',portfolioMoney(data.final_dca,''),'Idle cash earns zero'],['DCA INSTALLMENTS',fmt(Number($('portfolioSimulationInstallments')?.value||12),0),`${data.observations} daily observations`]
      ]);
      const curve=data.history_curve||[];
      if(window.Plotly&&node){await window.Plotly.react(node,[{x:curve.map(x=>x.time),y:curve.map(x=>x.lump_sum),type:'scatter',mode:'lines',name:'Lump sum'},{x:curve.map(x=>x.time),y:curve.map(x=>x.dca),type:'scatter',mode:'lines',name:'DCA'}],{..._QLayout(),yaxis:{...( _QLayout().yaxis||{}),title:'Portfolio value'},xaxis:{...( _QLayout().xaxis||{}),title:'Historical date'}},{displayModeBar:false,responsive:true});}
      if(status)status.textContent='Historical lump-sum/DCA comparison complete. This replays observed prices and is not a future forecast.';
    }else{
      const q=data.terminal_percentiles||{};
      financeMetricCards('portfolioSimulationMetrics',[
        ['TERMINAL P05',portfolioMoney(q.p05,''),'5th percentile scenario'],['TERMINAL MEDIAN',portfolioMoney(q.p50,''),'50th percentile scenario'],['TERMINAL P95',portfolioMoney(q.p95,''),'95th percentile scenario'],
        ['P(END < CAPITAL)',pct(data.probability_below_initial),'Model-implied scenario frequency, not forecast probability'],['MEDIAN MAX DRAWDOWN',pct(data.median_max_drawdown),'Across simulated paths'],['PATHS',fmt(data.paths||0,0),`${data.years} scenario years`]
      ]);
      const curve=data.curve||[];
      if(window.Plotly&&node){const x=curve.map(v=>v.years);await window.Plotly.react(node,[{x,y:curve.map(v=>v.p05),type:'scatter',mode:'lines',line:{width:0},name:'P05',hoverinfo:'skip'},{x,y:curve.map(v=>v.p95),type:'scatter',mode:'lines',line:{width:0},fill:'tonexty',name:'P05–P95'},{x,y:curve.map(v=>v.p25),type:'scatter',mode:'lines',line:{width:0},name:'P25',hoverinfo:'skip'},{x,y:curve.map(v=>v.p75),type:'scatter',mode:'lines',line:{width:0},fill:'tonexty',name:'P25–P75'},{x,y:curve.map(v=>v.p50),type:'scatter',mode:'lines',name:'Median'}],{..._QLayout(),xaxis:{...( _QLayout().xaxis||{}),title:'Years'},yaxis:{...( _QLayout().yaxis||{}),title:'Portfolio value'}},{displayModeBar:false,responsive:true});}
      if(status)status.textContent=`Generated ${data.paths} scenario paths from ${data.observations} aligned historical observations.`;
    }
    if($('portfolioSimulationNotes'))$('portfolioSimulationNotes').textContent=[data.assumptions||data.note||'',data.warning||'',Object.keys(data.failures||{}).length?`Failed symbols: ${JSON.stringify(data.failures)}`:''].filter(Boolean).join(' ');
  }catch(e){if(status)status.textContent=`Portfolio simulation failed: ${e.message}`;if(node)node.innerHTML=`<div class="quant-chart-state"><strong>Simulation unavailable</strong><span>${esc(e.message)}</span></div>`;}
}
window.runFinancePortfolioSimulation=runFinancePortfolioSimulation;

/* ── Portfolio command center: live book, stress, performance, rebalance ─ */
window.__portfolioLiveSnapshot = null;
window.__portfolioPerformance = null;
window.__portfolioStressResult = null;
window.__portfolioOptimizationResult = window.__portfolioOptimizationResult || null;

function setPortfolioTab(tab){
  const panel=document.querySelector(`#portfolioPanels [data-portfolio-panel="${tab}"]`);
  if(!panel) return;
  document.querySelectorAll('#portfolioTabs [data-portfolio-tab]').forEach(b=>{
    const yes=b.dataset.portfolioTab===tab; b.classList.toggle('active',yes); b.setAttribute('aria-selected',yes?'true':'false');
  });
  document.querySelectorAll('#portfolioPanels [data-portfolio-panel]').forEach(p=>{
    const yes=p.dataset.portfolioPanel===tab; p.classList.toggle('active',yes); p.hidden=!yes;
  });
  if(tab==='overview'||tab==='live') loadPortfolioLive(false);
  if(tab==='risk'&&!window.__portfolioStressResult) runPortfolioStress();
  if(tab==='simulation') loadFinanceToolkitStatus();
  if(tab==='performance'&&!window.__portfolioPerformance) loadPortfolioPerformance(false);
  if(tab==='performance'&&!window.__portfolioAnalytics) loadPortfolioAnalytics(false);
  if(tab==='performance'&&!window.__tradeLearningSummary) loadTradeLearningSummary(false);
  requestAnimationFrame(()=>{
    if(window.Plotly) panel.querySelectorAll('.js-plotly-plot').forEach(plot=>{try{window.Plotly.Plots.resize(plot);}catch(_){}});
  });
}
window.setPortfolioTab=setPortfolioTab;
document.addEventListener('click',(ev)=>{const btn=ev.target.closest('[data-portfolio-tab]'); if(btn)setPortfolioTab(btn.dataset.portfolioTab);});

function portfolioMoney(value, currency){
  if(value===null||value===undefined||!Number.isFinite(Number(value))) return '—';
  return `${currency?currency+' ':''}${fmt(Number(value),2)}`;
}
function portfolioMetricCards(targetId, cards){
  const node=$(targetId); if(!node)return;
  node.innerHTML=cards.map(([label,value,sub])=>`<div class="metric-card"><span>${esc(label)}</span><div class="metric-main">${esc(value)}</div>${sub?`<small>${esc(sub)}</small>`:''}</div>`).join('');
}
async function loadPortfolioLive(force=false){
  const status=$('portfolioOverviewStatus'), liveStatus=$('portfolioLiveStatus');
  if(status)status.textContent='Reading account equity, open positions, stop coverage and pending orders from MT5…';
  try{
    const data=await get('/api/portfolio/live',{force, retries:1, silent:true, region:'portfolioOverviewMetrics'});
    if(data.ok===false)throw new Error(data.error||'MT5 portfolio snapshot unavailable');
    window.__portfolioLiveSnapshot=data;
    const a=data.account||{}, s=data.summary||{}, currency=a.currency||'';
    const badge=$('portfolioSourceBadge'); if(badge){badge.textContent=data._stale_cache?'STALE BROKER SNAPSHOT':'LIVE MT5 SNAPSHOT';badge.className='status-pill '+(data._stale_cache?'warn':'good');}
    const cards=[
      ['BALANCE',portfolioMoney(a.balance,currency)],['EQUITY',portfolioMoney(a.equity,currency)],
      ['ACCOUNT FLOATING P&L',portfolioMoney(a.profit,currency)],['FREE MARGIN',portfolioMoney(a.free_margin,currency)],
      ['USED MARGIN',portfolioMoney(a.margin,currency)],['MARGIN LEVEL',a.margin_level==null?'—':fmt(a.margin_level,1)+'%'],
      ['OPEN POSITIONS',String(s.open_positions??0)],['PENDING ORDERS',String(s.pending_orders??0)],
      ['POSITIONS WITHOUT SL',String(s.positions_without_stop??0),s.stop_risk_coverage_complete?'Stop coverage complete':'Unprotected risk is unknown'],
      ['KNOWN STOP RISK',s.known_stop_risk_pct_equity==null?'—':fmt(s.known_stop_risk_pct_equity,2)+'% equity',portfolioMoney(s.known_stop_risk_cash,currency)]
    ];
    portfolioMetricCards('portfolioOverviewMetrics',cards);
    const by=(data.by_symbol||[]);
    setHtml('portfolioOverviewSymbols',by.length?by.slice(0,12).map(r=>`<tr><td><b>${esc(r.symbol)}</b></td><td>${fmt(r.positions,0)}</td><td>${fmt(r.buy_lots,2)}</td><td>${fmt(r.sell_lots,2)}</td><td>${esc(portfolioMoney(r.floating_pnl,currency))}</td></tr>`).join(''):'<tr><td colspan="5" class="muted">No open positions on the connected account.</td></tr>');
    const history=data.account_history||[];
    if(history.length>=2){
      _plotly('portfolioAccountHistoryChart',[
        {type:'scatter',mode:'lines',x:history.map(x=>x.time_utc),y:history.map(x=>Number(x.equity||0)),name:'Equity',line:{color:'#3ec6a8'}},
        {type:'scatter',mode:'lines',x:history.map(x=>x.time_utc),y:history.map(x=>Number(x.balance||0)),name:'Balance',line:{color:'#8297f5'}}
      ],{title:'Observed MT5 balance / equity snapshots',xaxis:{title:'Sample time (UTC)'},yaxis:{title:`Account currency (${currency||'not supplied'})`},legend:{orientation:'h'},margin:{l:70,r:20,t:42,b:55},height:300});
    }else _quantPanelMessage('portfolioAccountHistoryChart','Only one saved snapshot is available so far. Leave the Portfolio workspace open to sample history over time.','info','Building account history');
    const positions=data.positions||[];
    setHtml('portfolioLivePositionsBody',positions.length?positions.map(p=>{
      const protectedSL=Number(p.sl)>0; const risk=p.stop_risk_cash;
      return `<tr><td>${esc(p.ticket)}</td><td><b>${esc(p.symbol)}</b></td><td>${esc(p.side)}</td><td>${fmt(p.volume,2)}</td><td>${fmt(p.price_open,5)}</td><td>${fmt(p.price_current,5)}</td><td>${Number(p.sl)>0?fmt(p.sl,5):'—'}</td><td>${Number(p.tp)>0?fmt(p.tp,5):'—'}</td><td>${esc(portfolioMoney(p.profit,currency))}</td><td>${esc(portfolioMoney(p.swap,currency))}</td><td>${risk==null?'UNKNOWN':esc(portfolioMoney(risk,currency))}</td><td>${protectedSL?'SL SET':'UNPROTECTED'}</td></tr>`;
    }).join(''):'<tr><td colspan="12" class="muted">No open MT5 positions.</td></tr>');
    setHtml('portfolioLiveExposureBody',by.length?by.map(r=>`<tr><td><b>${esc(r.symbol)}</b></td><td>${fmt(r.positions,0)}</td><td>${fmt(r.buy_lots,2)}</td><td>${fmt(r.sell_lots,2)}</td><td>${fmt(r.net_lots,2)}</td><td>${esc(portfolioMoney(r.floating_pnl,currency))}</td><td>${fmt(r.positions_without_stop,0)}</td><td>${esc(portfolioMoney(r.known_stop_risk_cash,currency))}</td></tr>`).join(''):'<tr><td colspan="8" class="muted">No open positions.</td></tr>');
    const orderType={2:'Buy limit',3:'Sell limit',4:'Buy stop',5:'Sell stop',6:'Buy stop limit',7:'Sell stop limit'};
    setHtml('portfolioLiveOrdersBody',(data.orders||[]).length?data.orders.map(o=>`<tr><td>${esc(o.ticket)}</td><td><b>${esc(o.symbol)}</b></td><td>${esc(orderType[o.type]||('Type '+o.type))}</td><td>${fmt(o.volume_current??o.volume_initial,2)}</td><td>${fmt(o.price_open,5)}</td><td>${Number(o.sl)>0?fmt(o.sl,5):'—'}</td><td>${Number(o.tp)>0?fmt(o.tp,5):'—'}</td><td>${esc(o.time_setup_iso||'—')}</td></tr>`).join(''):'<tr><td colspan="8" class="muted">No pending broker orders.</td></tr>');
    const text=`${data.source||'MT5'} · ${data.generated_at||''} · ${positions.length} positions, ${(data.orders||[]).length} pending orders. ${s.positions_without_stop||0} position(s) have no stop loss.`;
    if(status)status.textContent=text; if(liveStatus)liveStatus.textContent=text;
    return data;
  }catch(error){
    const msg=error?.message||String(error); if(status)status.textContent='Live portfolio unavailable: '+msg; if(liveStatus)liveStatus.textContent='Live portfolio unavailable: '+msg;
    const badge=$('portfolioSourceBadge');if(badge){badge.textContent='BROKER DATA UNAVAILABLE';badge.className='status-pill warn';}
    setHtml('portfolioOverviewMetrics',`<div class="quant-chart-state"><strong>Live account unavailable</strong><span>${esc(msg)}. Connect MT5 and retry; no synthetic account is displayed.</span></div>`);
    return null;
  }
}
window.loadPortfolioLive=loadPortfolioLive;

async function runPortfolioStress(){
  const status=$('portfolioStressStatus'),body=$('portfolioStressBody');
  if(status)status.textContent='Calculating shocks with broker account-currency profit conversion…';
  try{
    const shocks=String($('portfolioStressShocks')?.value||'-10,-5,-2,2,5,10').split(',').map(x=>Number(x.trim()));
    if(!shocks.length||shocks.some(x=>!Number.isFinite(x)))throw new Error('Enter comma-separated finite percentages.');
    const data=await post('/api/portfolio/stress',{shocks_pct:shocks},{retries:2,silent:true,region:'portfolioStressChart'});
    window.__portfolioStressResult=data;
    const currency=data.account_currency||'';const rows=data.scenarios||[];
    setHtml('portfolioStressBody',rows.map(r=>`<tr><td>${fmt(r.shock_pct,2)}%</td><td>${esc(portfolioMoney(r.incremental_pnl,currency))}</td><td>${r.estimated_equity==null?'—':esc(portfolioMoney(r.estimated_equity,currency))}</td><td>${fmt(r.covered_positions,0)}</td><td>${fmt(r.unpriced_positions,0)}</td></tr>`).join('')||'<tr><td colspan="5">No scenarios returned.</td></tr>');
    _plotly('portfolioStressChart',[{type:'bar',x:rows.map(r=>`${r.shock_pct>0?'+':''}${r.shock_pct}%`),y:rows.map(r=>Number(r.incremental_pnl||0)),marker:{color:rows.map(r=>r.incremental_pnl<0?'#ef6464':'#39c6a7')},hovertemplate:'Shock %{x}<br>Incremental P&L: %{y:.2f}<extra></extra>'}],{title:'Incremental P&L under uniform instrument-price shocks',xaxis:{title:'Price shock'},yaxis:{title:`P&L (${currency||'account currency'})`},margin:{l:65,r:20,t:42,b:55},height:340});
    if(status)status.textContent=`${data.source} · ${rows.length} scenarios · ${rows.reduce((a,r)=>a+r.unpriced_positions,0)} unpriced position-scenario observations. ${data.notes?.join(' ')||''}`;
    return data;
  }catch(error){const msg=error?.message||String(error);if(status)status.textContent='Stress test unavailable: '+msg;setHtml('portfolioStressBody',`<tr><td colspan="5" class="error-text">${esc(msg)}</td></tr>`);_quantPanelMessage('portfolioStressChart',msg,'error','Stress analysis unavailable');return null;}
}
window.runPortfolioStress=runPortfolioStress;

async function loadPortfolioPerformance(force=false){
  const status=$('portfolioPerformanceStatus'),days=Number($('portfolioPerfDays')?.value||90);
  if(status)status.textContent='Reading closed trade deals from MT5 history…';
  try{
    const data=await get(`/api/portfolio/performance?days=${encodeURIComponent(days)}`,{force,retries:1,silent:true,region:'portfolioPerformanceMetrics'});
    if(data.ok===false)throw new Error(data.error||'Broker performance unavailable');
    window.__portfolioPerformance=data;const ccy=window.__portfolioLiveSnapshot?.account?.currency||'';
    portfolioMetricCards('portfolioPerformanceMetrics',[
      ['CLOSED POSITIONS',String(data.trades??0)],['NET REALIZED P&L',portfolioMoney(data.net_realized_pnl,ccy)],
      ['WIN RATE',data.win_rate==null?'—':fmt(data.win_rate*100,1)+'%'],['PROFIT FACTOR',data.profit_factor==null?'—':fmt(data.profit_factor,2)],
      ['AVERAGE TRADE',portfolioMoney(data.average_trade_pnl,ccy)],['MAX DRAWDOWN · P&L CURVE',portfolioMoney(data.max_drawdown_cash_on_cumulative_realized_pnl,ccy)],
      ['GROSS PROFIT',portfolioMoney(data.gross_profit,ccy)],['GROSS LOSS',portfolioMoney(data.gross_loss,ccy)]
    ]);
    const curve=data.daily_curve||[];
    if(curve.length){_plotly('portfolioPerformanceChart',[
      {type:'scatter',mode:'lines',x:curve.map(x=>x.date),y:curve.map(x=>x.cumulative_net_pnl),name:'Cumulative realized P&L',line:{color:'#43c5a8'}},
      {type:'bar',x:curve.map(x=>x.date),y:curve.map(x=>x.daily_net_pnl),name:'Daily realized P&L',opacity:0.45,yaxis:'y2'}
    ],{title:'Realized P&L in history window (not account equity)',xaxis:{title:'Close date'},yaxis:{title:`Cumulative P&L (${ccy||'account currency'})`},yaxis2:{title:'Daily P&L',overlaying:'y',side:'right',showgrid:false},legend:{orientation:'h'},margin:{l:65,r:60,t:42,b:55},height:320});}
    else _quantPanelMessage('portfolioPerformanceChart','No completed position deals were returned for this date window.','info','No closed trades in window');
    setHtml('portfolioPerfSymbols',(data.by_symbol||[]).map(x=>`<tr><td><b>${esc(x.symbol)}</b></td><td>${fmt(x.trades,0)}</td><td>${x.trades?fmt(100*x.wins/x.trades,1)+'%':'—'}</td><td>${esc(portfolioMoney(x.net_pnl,ccy))}</td><td>${x.gross_loss>0?fmt(x.gross_profit/x.gross_loss,2):'—'}</td></tr>`).join('')||'<tr><td colspan="5" class="muted">No closed trades.</td></tr>');
    setHtml('portfolioPerfMagic',(data.by_magic||[]).map(x=>`<tr><td><b>${esc(x.magic)}</b></td><td>${fmt(x.trades,0)}</td><td>${x.trades?fmt(100*x.wins/x.trades,1)+'%':'—'}</td><td>${esc(portfolioMoney(x.net_pnl,ccy))}</td><td>${x.gross_loss>0?fmt(x.gross_profit/x.gross_loss,2):'—'}</td></tr>`).join('')||'<tr><td colspan="5" class="muted">No strategy attribution available.</td></tr>');
    setHtml('portfolioPerfTrades',(data.recent_trades||[]).map(t=>`<tr><td>${esc(t.position_id)}</td><td>${esc(t.symbol)}</td><td>${esc(t.closed_at||'—')}</td><td>${esc(portfolioMoney(t.net_pnl,ccy))}</td><td>${fmt(t.deal_legs_in_window,0)}</td></tr>`).join('')||'<tr><td colspan="5" class="muted">No closed trades.</td></tr>');
    if(status)status.textContent=`MT5 deal history · ${days}-day window · ${data.trades||0} completed positions. ${data.notes?.join(' ')||''}`;
    return data;
  }catch(error){const msg=error?.message||String(error);if(status)status.textContent='Trade performance unavailable: '+msg;setHtml('portfolioPerfSymbols',`<tr><td colspan="5" class="error-text">${esc(msg)}</td></tr>`);setHtml('portfolioPerfTrades',`<tr><td colspan="5" class="error-text">${esc(msg)}</td></tr>`);_quantPanelMessage('portfolioPerformanceChart',msg,'error','Broker history unavailable');return null;}
}
window.loadPortfolioPerformance=loadPortfolioPerformance;

function _renderTradeLearning(data){
  if(!data)return null;
  window.__tradeLearningSummary=data;
  const status=$('tradeLearningStatus'), sum=data.summary||{}, sync=data.sync||{};
  const syncLine=data.status==='not_synced'?'No history has been synced yet.':`Last sync ${String(sync.synced_at_utc||'').replace('T',' ').replace('Z','')} UTC · account ${data.account?.login||'—'} @ ${data.account?.server||'—'}`;
  portfolioMetricCards('tradeLearningMetrics',[
    ['CLOSED POSITIONS',fmt(data.closed_positions_in_window||0,0),`${fmt(sum.wins||0,0)} wins · ${fmt(sum.losses||0,0)} losses`],
    ['NET REALIZED P&L',portfolioMoney(sum.net_pnl,window.__portfolioLiveSnapshot?.account?.currency||''),`${fmt(data.deals_stored||0,0)} stored deal legs`],
    ['WIN RATE',sum.win_rate==null?'—':fmt(Number(sum.win_rate)*100,1)+'%',sum.sample_quality||'insufficient sample'],
    ['EXPECTANCY / POSITION',portfolioMoney(sum.expectancy_per_trade,window.__portfolioLiveSnapshot?.account?.currency||''),'Net of deal commissions, swap and fees'],
    ['DECISIONS MATCHED',fmt(data.decision_matches||0,0),'Historical decisions joined to broker positions'],
    ['META-LABELER UPDATES',fmt(data.meta_labeler_updates||0,0),'Only matched decision features; deduplicated']
  ]);
  const fmtRate=v=>v==null?'—':fmt(Number(v)*100,1)+'%';
  const fmtPf=v=>v==null?'—':fmt(v,2);
  setHtml('tradeLearningSymbols',(data.by_symbol||[]).map(r=>`<tr><td><b>${esc(r.symbol)}</b></td><td>${fmt(r.closed_trades,0)}</td><td>${fmtRate(r.win_rate)}</td><td>${r.expectancy_per_trade==null?'—':esc(portfolioMoney(r.expectancy_per_trade,window.__portfolioLiveSnapshot?.account?.currency||''))}</td><td>${esc(portfolioMoney(r.net_pnl,window.__portfolioLiveSnapshot?.account?.currency||''))}</td><td>${fmtPf(r.profit_factor)}</td></tr>`).join('')||'<tr><td colspan="6" class="muted">No complete closed positions in this window.</td></tr>');
  setHtml('tradeLearningMagic',(data.by_magic||[]).map(r=>`<tr><td><b>${esc(r.magic)}</b></td><td>${fmt(r.closed_trades,0)}</td><td>${fmtRate(r.win_rate)}</td><td>${r.expectancy_per_trade==null?'—':esc(portfolioMoney(r.expectancy_per_trade,window.__portfolioLiveSnapshot?.account?.currency||''))}</td><td>${esc(portfolioMoney(r.net_pnl,window.__portfolioLiveSnapshot?.account?.currency||''))}</td><td>${fmtPf(r.profit_factor)}</td></tr>`).join('')||'<tr><td colspan="6" class="muted">No magic-number attribution in this window.</td></tr>');
  setHtml('tradeLearningRegimes',(data.by_regime||[]).map(r=>`<tr><td><b>${esc(r.regime)}</b></td><td>${fmt(r.closed_trades,0)}</td><td>${fmtRate(r.win_rate)}</td><td>${r.expectancy_per_trade==null?'—':esc(portfolioMoney(r.expectancy_per_trade,window.__portfolioLiveSnapshot?.account?.currency||''))}</td><td>${esc(portfolioMoney(r.net_pnl,window.__portfolioLiveSnapshot?.account?.currency||''))}</td></tr>`).join('')||'<tr><td colspan="5" class="muted">No stored decision-regime attribution. Unknown regimes are not inferred from current market state.</td></tr>');
  setHtml('tradeLearningRecent',(data.recent_trades||[]).map(r=>`<tr><td>${esc(r.position_id)}</td><td>${esc(r.symbol)}</td><td>${esc(r.magic)}</td><td>${esc(r.side)}</td><td>${esc(r.closed_at_utc||'—')}</td><td>${esc(portfolioMoney(r.gross_profit,window.__portfolioLiveSnapshot?.account?.currency||''))}</td><td>${esc(portfolioMoney(r.commission,window.__portfolioLiveSnapshot?.account?.currency||''))}</td><td>${esc(portfolioMoney(r.swap,window.__portfolioLiveSnapshot?.account?.currency||''))}</td><td>${esc(portfolioMoney(r.fee,window.__portfolioLiveSnapshot?.account?.currency||''))}</td><td><b>${esc(portfolioMoney(r.net_pnl,window.__portfolioLiveSnapshot?.account?.currency||''))}</b></td><td>${r.decision_matched?'Matched':'Unmatched'}</td></tr>`).join('')||'<tr><td colspan="11" class="muted">No reconciled position outcomes.</td></tr>');
  if(status)status.textContent=`${syncLine} · ${data.summary?.closed_trades||0} complete positions included in summary.`;
  const notes=[...(data.notes||[])];
  if(data.incomplete_closed_positions)notes.push(`${data.incomplete_closed_positions} closed position group(s) were excluded because the retained history did not contain an opening deal.`);
  const note=$('tradeLearningNotes');if(note)note.textContent=notes.join(' ');
  return data;
}
async function loadTradeLearningSummary(force=false){
  const status=$('tradeLearningStatus');if(status)status.textContent='Reading saved broker trade history…';
  try{const days=Number($('tradeLearningDays')?.value||365);const data=await get(`/api/trade-learning/summary?days=${encodeURIComponent(days)}&limit=50&offset=0`,{force,retries:1,silent:true,region:'tradeLearningMetrics'});if(data.ok===false)throw new Error(data.error||'Trade history unavailable');return _renderTradeLearning(data);}
  catch(error){const msg=error?.message||String(error);if(status)status.textContent='Saved trade history unavailable: '+msg;setHtml('tradeLearningRecent',`<tr><td colspan="11" class="error-text">${esc(msg)}</td></tr>`);return null;}
}
async function syncTradeHistoryAndLearn(){
  const status=$('tradeLearningStatus');if(status)status.textContent='Connecting to MT5 and importing historical deal legs; this can take time for large history windows…';
  try{const days=Number($('tradeLearningDays')?.value||365);const result=await post('/api/trade-learning/sync',{days},{retries:0,silent:true,region:'tradeLearningMetrics'});if(result.ok===false)throw new Error(result.error||'Trade history sync failed');const data=result.summary||await get(`/api/trade-learning/summary?days=${days}&limit=50`,{force:true,silent:true});_renderTradeLearning(data);const sync=result.sync||{},learning=result.learning||{};if(status)status.textContent=`Imported ${fmt(sync.fetched_deals||0,0)} deal legs (${fmt(sync.new_deals||0,0)} new, ${fmt(sync.updated_existing_deals||0,0)} updated); ${fmt(learning.decision_rows_reconciled||0,0)} decision rows reconciled, ${fmt(learning.meta_labeler_updates||0,0)} model updates. No orders sent.`;window.__tradeLearningSummary=data;return result;}
  catch(error){const msg=error?.message||String(error);if(status)status.textContent='MT5 history synchronization failed: '+msg;setHtml('tradeLearningRecent',`<tr><td colspan="11" class="error-text">${esc(msg)}</td></tr>`);return null;}
}
window.loadTradeLearningSummary=loadTradeLearningSummary;
window.syncTradeHistoryAndLearn=syncTradeHistoryAndLearn;

async function loadPortfolioAnalytics(force=false){
  const status=$('portfolioAccountAnalyticsStatus');
  const days=Number($('portfolioPerfDays')?.value||90);
  const benchmark=String($('portfolioBenchmarkSymbol')?.value||'').trim().toUpperCase();
  const factors=String($('portfolioFactorProxySymbols')?.value||'').split(',').map(x=>x.trim().toUpperCase()).filter(Boolean).filter((x,i,a)=>x!==benchmark&&a.indexOf(x)===i).slice(0,5);
  if(status)status.textContent='Reconciling account snapshots and MT5 balance deals…';
  try{
    const qs=new URLSearchParams({days:String(days),benchmark,factors:factors.join(',')});
    const data=await get(`/api/portfolio/analytics?${qs.toString()}`,{force,retries:1,silent:true,region:'portfolioAccountAnalyticsMetrics'});
    if(data.ok===false)throw new Error(data.error||'Account analytics unavailable');
    window.__portfolioAnalytics=data;
    const ccy=data.account?.currency||window.__portfolioLiveSnapshot?.account?.currency||'';
    const perf=data.cashflow_adjusted_performance||{}, hist=data.account_history||{}, bm=data.benchmark||{}, fm=data.factor_model||{};
    const pct=(v)=>v==null?'—':fmt(Number(v),2)+'%';
    const cards=[
      ['OBSERVED SNAPSHOTS',fmt(hist.snapshot_count||0,0),`${fmt(hist.coverage_days||0,2)} days covered`],
      ['LINKED TWR · ESTIMATE',pct(perf.time_weighted_return_pct_estimate),perf.status||'unavailable'],
      ['ANNUALIZED TWR · ESTIMATE',pct(perf.annualized_twr_pct_estimate),'Elapsed-calendar-time annualization'],
      ['MONEY-WEIGHTED XIRR',pct(perf.money_weighted_annualized_return_pct_estimate),perf.money_weighted_return_status||'unavailable'],
      ['BALANCE FLOW CANDIDATES',portfolioMoney(perf.external_cashflow_total,ccy),`${fmt(perf.external_cashflow_count||0,0)} events`],
      ['ADJUSTMENTS TO REVIEW',fmt(perf.adjustments_requiring_review||0,0),'Credits / corrections / bonuses'],
      ['BENCHMARK BETA',bm.status==='ok'&&bm.beta!=null?fmt(bm.beta,3):'—',bm.status==='ok'?`${bm.observations} paired periods`:bm.status||'not configured'],
      ['ANNUALIZED ALPHA · APPROX.',bm.status==='ok'?pct(bm.alpha_annualized_pct_approx):'—','Zero risk-free rate assumption'],
      ['TRACKING ERROR · ANN.',bm.status==='ok'?pct(bm.tracking_error_annualized_pct):'—','Vs selected benchmark'],
      ['INFORMATION RATIO',bm.status==='ok'&&bm.information_ratio_approx!=null?fmt(bm.information_ratio_approx,2):'—','Approximate daily-period estimate'],
      ['FACTOR MODEL R²',fm.r_squared==null?'—':pct(Number(fm.r_squared)*100),fm.status||'not calculated'],
      ['RESIDUAL VOL · ANN.',fm.residual_vol_annualized_pct==null?'—':pct(fm.residual_vol_annualized_pct),'OLS proxy-factor model']
    ];
    portfolioMetricCards('portfolioAccountAnalyticsMetrics',cards);
    const curve=bm.comparison_curve||[];
    if(curve.length>=2){
      _plotly('portfolioBenchmarkChart',[
        {type:'scatter',mode:'lines',x:curve.map(x=>x.date),y:curve.map(x=>Number(x.portfolio_cumulative_return_pct)),name:'Portfolio · cash-flow adjusted',line:{color:'#3ec6a8'}},
        {type:'scatter',mode:'lines',x:curve.map(x=>x.date),y:curve.map(x=>Number(x.benchmark_cumulative_return_pct)),name:bm.symbol||'Benchmark',line:{color:'#8196f5'}}
      ],{title:`Cumulative return · portfolio vs ${bm.symbol||'benchmark'}`,xaxis:{title:'Matched broker daily-bar dates'},yaxis:{title:'Cumulative return (%)'},legend:{orientation:'h'},margin:{l:65,r:20,t:42,b:55},height:320});
    }else _quantPanelMessage('portfolioBenchmarkChart',benchmark?`Benchmark comparison unavailable: ${bm.error||bm.status||'insufficient aligned observations'}. ${bm.notes||''}`:'Enter a benchmark symbol to calculate benchmark-relative metrics. Account cash-flow metrics are still available.','info','Benchmark comparison needs data');
    const betas=fm.betas||{},tstats=fm.t_statistics_approx||{};
    setHtml('portfolioFactorProxyBody',Object.keys(betas).length?Object.keys(betas).map(sym=>`<tr><td><b>${esc(sym)}</b></td><td>${fmt(betas[sym],4)}</td><td>${tstats[sym]==null?'—':fmt(tstats[sym],2)}</td></tr>`).join(''):`<tr><td colspan="3" class="muted">${esc(fm.status||'Factor model not calculated')}. ${esc(fm.error||'Add exact broker symbol(s), and collect sufficient account history.')}</td></tr>`);
    if($('portfolioFactorModelStatus'))$('portfolioFactorModelStatus').textContent=`${fm.method||'Factor model unavailable'} · ${fm.observations||0} observations · ${fm.status||'not calculated'}${fm.required_observations?` · needs ${fm.required_observations}`:''}`;
    const flows=data.cashflows||{};
    const audit=[...(flows.external_candidates||[]),...(flows.adjustments_requiring_review||[])].sort((a,b)=>Number(b.time||0)-Number(a.time||0)).slice(0,100);
    setHtml('portfolioAccountCashflowsBody',audit.length?audit.map(x=>`<tr><td>${esc(x.time_iso||'—')}</td><td>${esc(x.type_name||'—')}</td><td>${esc(x.classification||'—')}</td><td>${esc(portfolioMoney(x.amount,ccy))}</td><td>${esc(x.comment||'—')}</td></tr>`).join(''):'<tr><td colspan="5" class="muted">No balance-flow candidates or review-required adjustments were returned for this window.</td></tr>');
    const notes=[...(perf.notes||[]),...(data.market_data_errors?Object.entries(data.market_data_errors).map(([sym,msg])=>`${sym}: ${msg}`):[]),...(fm.notes||[])];
    if($('portfolioAccountAnalyticsNotes'))$('portfolioAccountAnalyticsNotes').textContent=notes.join(' ');
    if(status)status.textContent=`${data.source} · ${hist.snapshot_count||0} snapshots / ${fmt(hist.coverage_days||0,2)} days · benchmark ${bm.status||'not configured'} · factor model ${fm.status||'not requested'}.`;
    return data;
  }catch(error){
    const msg=error?.message||String(error);if(status)status.textContent='Account analytics unavailable: '+msg;
    setHtml('portfolioAccountAnalyticsMetrics',`<div class="quant-chart-state"><strong>Account analytics unavailable</strong><span>${esc(msg)}. Check MT5 connection and server logs; no synthetic performance is displayed.</span></div>`);
    _quantPanelMessage('portfolioBenchmarkChart',msg,'error','Account analytics unavailable');return null;
  }
}
window.loadPortfolioAnalytics=loadPortfolioAnalytics;

document.addEventListener('change',(ev)=>{if(['portfolioPerfDays','portfolioBenchmarkSymbol','portfolioFactorProxySymbols'].includes(ev.target?.id))window.__portfolioAnalytics=null;});

async function buildPortfolioRebalance(){
  const status=$('portfolioRebalanceStatus'),body=$('portfolioRebalanceBody');
  const opt=window.__portfolioOptimizationResult;
  if(!opt){if(status)status.textContent='Build a research allocation first, then generate a proposal.';return null;}
  try{
    const method=$('portfolioRebalanceMethod')?.value||'max_sharpe';const target=opt[method]?.weights;
    if(!target)throw new Error(`Allocation method ${method} is not available. Run the allocation again.`);
    const current={};String($('portfolioCurrentWeights')?.value||'').split(',').forEach(pair=>{const i=pair.indexOf('=');if(i>0){const k=pair.slice(0,i).trim().toUpperCase(),v=Number(pair.slice(i+1).trim());if(k)current[k]=v;}});
    const data=await post('/api/portfolio/rebalance',{target_weights:target,current_weights:current,capital:Number($('portfolioRebalanceCapital')?.value||10000),cost_bps_per_side:Number($('portfolioRebalanceCost')?.value||5)},{retries:1,silent:true,region:'portfolioRebalanceBody'});
    portfolioMetricCards('portfolioRebalanceMetrics',[
      ['ONE-WAY TURNOVER',fmt(data.one_way_turnover_fraction*100,2)+'%'],['EST. GROSS TRADED NOTIONAL',fmt(data.gross_traded_notional_estimate,2)],['EST. TRANSACTION COST',fmt(data.estimated_transaction_cost,2)],['EXECUTABLE',data.is_executable?'YES':'NO — RESEARCH ONLY']
    ]);
    setHtml('portfolioRebalanceBody',(data.rows||[]).map(r=>`<tr><td><b>${esc(r.symbol)}</b></td><td>${_qPct(r.current_weight)}</td><td>${_qPct(r.target_weight)}</td><td>${_qPct(r.delta_weight)}</td><td>${esc(r.action)}</td><td>${fmt(r.current_value,2)}</td><td>${fmt(r.target_value,2)}</td><td>${fmt(r.estimated_cost,2)}</td></tr>`).join('')||'<tr><td colspan="8">No rebalance rows.</td></tr>');
    if(status)status.textContent=`${method} target · ${data.rows?.length||0} symbols · ${data.notes?.join(' ')||''}`;
    return data;
  }catch(error){const msg=error?.message||String(error);if(status)status.textContent='Rebalance proposal unavailable: '+msg;setHtml('portfolioRebalanceBody',`<tr><td colspan="8" class="error-text">${esc(msg)}</td></tr>`);return null;}
}
window.buildPortfolioRebalance=buildPortfolioRebalance;

/* ── Macro / Economic / Geopolitical ─────────────────────── */
async function runMacroGeoAnalysis(){
  try{
    const data = await post('/api/macro/analyze', { try_fetch: true });
    if(data.error) throw Error(data.error);
    setTxt('macroPosture', data.posture || '—');
    setTxt('macroRisk', 'risk '+ (data.composite_risk!=null ? Number(data.composite_risk).toFixed(0)+'/100' : '—'));
    const m = data.market || {};
    setTxt('macroRegime', m.regime || '—');
    setTxt('macroBias', 'bias '+(m.bias||'—')+' · vol '+ (m.realized_vol_ann!=null ? (m.realized_vol_ann*100).toFixed(1)+'%' : '—'));
    const g = data.geopolitical || {};
    setTxt('macroGeoTone', g.tone || '—');
    setTxt('macroGeoScore', 'score '+(g.risk_score!=null ? Number(g.risk_score).toFixed(0) : '—'));
    const events = (data.economic || {}).events || [];
    if(events.length){
      setTxt('macroNextEvent', events[0].name || '—');
      setTxt('macroNextWhen', (events[0].hours_until!=null ? Number(events[0].hours_until).toFixed(1)+'h · ' : '')+(events[0].impact||''));
    } else {
      setTxt('macroNextEvent', '—');
      setTxt('macroNextWhen', 'no events');
    }
    const body = $('macroEventsBody');
    if(body){
      body.innerHTML = events.length ? events.slice(0,12).map(e=>
        `<tr><td>${esc(e.name)}</td><td>${esc(e.impact)}</td><td>${Number(e.hours_until).toFixed(1)}</td><td>${esc(e.region)}</td></tr>`
      ).join('') : '<tr><td colspan="4" class="muted">None</td></tr>';
    }
    const hl = $('macroHeadlines');
    if(hl){
      const lines = g.headlines || [];
      hl.innerHTML = lines.length ? lines.slice(0,10).map(t=>`<div class="log">${esc(t)}</div>`).join('') : `<div class="muted">${esc(g.summary||'No headlines')}</div>`;
    }
    const adv = $('macroAdvice');
    if(adv){
      adv.innerHTML = (data.advice||[]).map(a=>`<div class="log"><b>Advice</b> ${esc(a)}</div>`).join('') +
        `<div class="muted">${esc(data.disclaimer||'')}</div>`;
    }
    toast('MACRO / GEO UPDATED');
  }catch(e){
    toast('ERROR: '+(e.message||e));
  }
}
window.runMacroGeoAnalysis = runMacroGeoAnalysis;


/* v4.36.0 — optional local interface feedback. No sound is played until the user enables it. */
(function initInstitutionalUiFeedback(){
  const SOUND_KEY = 'att_ui_sound_v1';
  let audioContext = null;
  const soundIsEnabled = () => { try { return localStorage.getItem(SOUND_KEY) === 'on'; } catch (_) { return false; } };
  function paintSoundToggle(){
    const button = document.getElementById('soundToggle');
    const icon = document.getElementById('soundToggleIcon');
    const label = document.getElementById('soundToggleLabel');
    const enabled = soundIsEnabled();
    if (button) {
      button.setAttribute('aria-pressed', enabled ? 'true' : 'false');
      button.setAttribute('aria-label', enabled ? 'Disable interface click sounds' : 'Enable interface click sounds');
      button.title = enabled ? 'Subtle interface click sounds · on' : 'Subtle interface click sounds · off by default';
      button.classList.toggle('sound-enabled', enabled);
    }
    if (icon) icon.textContent = enabled ? '♫' : '♪';
    if (label) label.textContent = enabled ? 'Sound on' : 'Sound off';
  }
  function playUiTap(){
    if (!soundIsEnabled()) return;
    try {
      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      if (!AudioCtx) return;
      if (!audioContext) audioContext = new AudioCtx();
      if (audioContext.state === 'suspended') audioContext.resume().catch(() => {});
      const now = audioContext.currentTime;
      const oscillator = audioContext.createOscillator();
      const gain = audioContext.createGain();
      const filter = audioContext.createBiquadFilter();
      oscillator.type = 'sine';
      oscillator.frequency.setValueAtTime(760, now);
      oscillator.frequency.exponentialRampToValueAtTime(520, now + 0.035);
      filter.type = 'lowpass';
      filter.frequency.setValueAtTime(1800, now);
      gain.gain.setValueAtTime(0.0001, now);
      gain.gain.exponentialRampToValueAtTime(0.018, now + 0.006);
      gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.055);
      oscillator.connect(filter); filter.connect(gain); gain.connect(audioContext.destination);
      oscillator.start(now); oscillator.stop(now + 0.06);
      oscillator.onended = () => { try { oscillator.disconnect(); filter.disconnect(); gain.disconnect(); } catch (_) {} };
    } catch (_) { /* Audio feedback is optional; never interfere with trading controls. */ }
  }
  window.toggleUiSound = function toggleUiSound(){
    const next = !soundIsEnabled();
    try { localStorage.setItem(SOUND_KEY, next ? 'on' : 'off'); } catch (_) {}
    paintSoundToggle();
    if (next) playUiTap();
  };
  document.addEventListener('click', (event) => {
    if (!soundIsEnabled()) return;
    const control = event.target && event.target.closest ? event.target.closest('button, .nav-item, .global-nav-btn') : null;
    if (!control || control.disabled || control.getAttribute('aria-disabled') === 'true') return;
    if (control.id === 'soundToggle' || control.dataset.sound === 'off' || control.classList.contains('btn-danger')) return;
    playUiTap();
  }, true);
  paintSoundToggle();
})();
