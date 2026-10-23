// Fonctions communes aux pages de l'enregistreur.
const $ = id => document.getElementById(id);
const fmt = s => [Math.floor(s/3600), Math.floor(s%3600/60), Math.floor(s%60)].map(n => String(n).padStart(2,'0')).join(':');
const mb = b => (b/1048576).toFixed(b > 1e9 ? 0 : 1) + ' Mio';

async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}

// Éléments d'état facultatifs (présents selon la page) : state, session, elapsed, free, overruns, cfg, error
function renderStatus(s) {
  const st = $('state');
  if (st) { st.textContent = {recording:'Enregistrement', idle:'Prêt', error:'Erreur'}[s.state] || s.state; st.className = 'state ' + s.state; }
  const set = (id, v) => { const e = $(id); if (e) e.textContent = v; };
  set('session', s.session || '');
  set('elapsed', fmt(s.elapsed_s));
  set('free', s.free_gb);
  set('overruns', s.overruns);
  set('cfg', `${s.config.device} · ${s.config.channels} voies · ${s.config.rate} Hz · ${s.config.format}`);
  set('error', s.error || '');
  document.title = (s.state === 'recording' ? '● ' : '') + 'Enregistreur XR18';
}

async function pollStatus(onData) {
  try { const s = await api('/api/status'); renderStatus(s); if (onData) onData(s); }
  catch (e) { const el = $('error'); if (el) el.textContent = 'Connexion perdue : ' + e.message; }
}

// Bouton unique #recbtn : « ● REC » au repos, « ■ STOP » pendant la prise (arrêt en deux temps pour éviter
// de couper un concert d'un tap accidentel). Le champ #name est masqué pendant l'enregistrement.
// Renvoie render(s) à appeler à chaque état reçu.
function initRecordControls(afterAction) {
  let busy = false, armed = null, last = null;
  const btn = $('recbtn'), name = $('name');
  const paint = () => {
    const rec = !!last && last.state === 'recording';
    btn.disabled = busy || !last;
    btn.textContent = armed ? 'Confirmer ?' : rec ? '■ STOP' : '● REC';
    btn.classList.toggle('stopping', rec && !armed);
    btn.classList.toggle('confirm', !!armed);
    name.hidden = rec;
  };
  const disarm = () => { if (armed) { clearTimeout(armed); armed = null; } paint(); };
  const arm = () => { armed = setTimeout(disarm, 4000); paint(); };
  const render = s => { last = s; if (s.state !== 'recording' && armed) { clearTimeout(armed); armed = null; } paint(); };
  const act = async (path, body) => {
    busy = true; paint(); $('error').textContent = '';
    try { await api(path, body); } catch (e) { $('error').textContent = e.message; }
    busy = false;
    await pollStatus(render);
    if (afterAction) afterAction();
  };
  btn.onclick = () => {
    if (!last) return;
    if (last.state !== 'recording') return act('/api/start', {name: name.value});
    if (!armed) return arm();
    disarm(); act('/api/stop', {});
  };
  return {render};
}
