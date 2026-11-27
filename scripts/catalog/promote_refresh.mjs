#!/usr/bin/env node
// sun.bdfz.net 教材下载 refresh — promotion driver (runs on the owner's Mac; launchd daily, or by hand).
//   node promote.mjs run                         promote the host's newest `ready` candidate, if any
//   node promote.mjs approve <release_id> "<basis>"   promote a `ready` or `held` candidate after the owner approved it
//   node promote.mjs status                      print the host state and the channel record
// The host job (catalog_refresh_host.py) builds and checks candidates; only this driver switches
// data/catalog/CURRENT, so production, the release registry and Status change together. It never touches
// the image or the container. A switch the site does not confirm within 3 minutes is written back.
// Registered as release-authority projects[sun-bdfz-net].catalog_refresh_channel; this file's sha256 must
// match channel.mac.driver_sha256 before any production write.
import fs from 'node:fs'; import path from 'node:path'; import crypto from 'node:crypto'; import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const REG = '/Users/ylsuen/CF/platform/release-authority.json', PID = 'sun-bdfz-net';
const SELF = fileURLToPath(import.meta.url), HERE = path.dirname(SELF);
const EVIDENCE_ROOT = '/Users/ylsuen/CF/reports/operations/sun-catalog-refresh/runs';
const LOCAL_STATE = HERE + '/driver-state.json';
const HOST = 'gcsjc13', HROOT = '/root/cross-subject-knowledge', HSTATE = HROOT + '/state/catalog-refresh';
const SITE = 'https://sun.bdfz.net';
const PUBLISHER = '/Users/ylsuen/CF/sites/infra/status/tools/publish-update.mjs';
const READBACK = '/Users/ylsuen/CF/sites/infra/status/tools/vps-sun-readback.py';
const PROJECT_STATE = '/Users/ylsuen/CF/platforms/cross-subject-knowledge/PROJECT_STATE.md';
const sha = (b) => crypto.createHash('sha256').update(b).digest('hex');
const now = () => new Date().toISOString();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const readJson = (p, d) => { try { return JSON.parse(fs.readFileSync(p, 'utf8')); } catch { return d; } };
const say = (o) => console.log(JSON.stringify({ at: now(), ...o }));

// Traditional glyph forms for the Status record (glyph conversion only, no vocabulary change)
const HANT = { '小学': '小學', '初中': '初中', '高中': '高中', '语文': '語文', '数学': '數學', '英语': '英語', '道德与法治': '道德與法治',
  '思想政治': '思想政治', '历史': '歷史', '地理': '地理', '物理': '物理', '化学': '化學', '生物学': '生物學', '科学': '科學',
  '信息技术': '信息技術', '信息科技': '信息科技', '通用技术': '通用技術', '艺术': '藝術', '音乐': '音樂', '美术': '美術',
  '体育与健康': '體育與健康', '日语': '日語', '俄语': '俄語', '法语': '法語', '德语': '德語', '西班牙语': '西班牙語' };

function ssh(cmd, input) {
  return execFileSync('ssh', ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=20', '-o', 'ControlMaster=no', '-o', 'ControlPath=none', HOST, cmd],
    { input, encoding: 'utf8', timeout: 300000, maxBuffer: 64 * 1024 * 1024 });
}
const project = () => JSON.parse(fs.readFileSync(REG, 'utf8')).projects.find((p) => p.project_id === PID);

async function withRegistry(change, fn) {
  let fd;
  for (let i = 0; ; i++) {
    try { fd = fs.openSync(REG + '.lock', 'wx', 0o600); break; } catch (e) { if (e.code !== 'EEXIST' || i >= 120) throw e; await sleep(5000); }
  }
  try {
    fs.writeFileSync(fd, JSON.stringify({ pid: process.pid, change_id: change, at: now() }));
    const before = fs.readFileSync(REG), reg = JSON.parse(before), p = reg.projects.find((x) => x.project_id === PID);
    const out = fn(reg, p);
    if (sha(before) !== sha(fs.readFileSync(REG))) throw new Error('registry changed underneath');
    reg.updated_at = now(); fs.writeFileSync(REG + '.tmp', JSON.stringify(reg, null, 2) + '\n'); fs.renameSync(REG + '.tmp', REG);
    return out;
  } finally { fs.closeSync(fd); fs.unlinkSync(REG + '.lock'); }
}

async function getJson(url) {
  const r = await fetch(url + (url.includes('?') ? '&' : '?') + 'nocache=' + Date.now(), { cache: 'no-store', signal: AbortSignal.timeout(30000) });
  if (!r.ok) throw new Error(url + ' ' + r.status);
  return r.json();
}

function channelCheck(mode) {
  const ch = project().catalog_refresh_channel;
  if (!ch) throw new Error('catalog_refresh_channel is not registered');
  if (sha(fs.readFileSync(SELF)) !== ch.mac.driver_sha256) throw new Error('driver differs from the registered sha256');
  if (ch.status === 'paused') throw new Error('channel paused');
  if (mode === 'run' && ch.status !== 'active') return null;   // manual_only: approve only
  return ch;
}

function noteHeld(rec, local) {
  local.noted_held = local.noted_held || [];
  if (local.noted_held.includes(rec.release_id)) return;
  const line = `## 教材下載更新待審 — ${rec.built_at}\n\n候選目錄 \`${rec.release_id}\`（新增 ${rec.added}、不再列出 ${rec.withdrawn}，平臺版本 ${rec.module_version}）未自動發布：${(rec.reasons || []).join('；')}。` +
    `差異見 gcsjc13 \`${HSTATE}/latest.json\`。核准後執行：\`node ${SELF} approve ${rec.release_id} "<核准依據>"\`。\n\n`;
  fs.writeFileSync(PROJECT_STATE, line + fs.readFileSync(PROJECT_STATE, 'utf8'));
  local.noted_held.push(rec.release_id);
}

function statusNote(rec, runDir, acc) {
  const byText = (m) => Object.entries(m || {}).sort().map(([k, n]) => k.split('/').map((x) => HANT[x] || x).join('·') + ' ' + n).join('、');
  const removedOffline = rec.withdrawn - (rec.superseded_withdrawn || 0);
  const update = {
    schemaVersion: 'bdfz-public-update-v1', public: true, id: rec.release_id.slice(0, 8) + '-sun-catalog-' + rec.release_id.slice(-8), revision: 1, kind: 'maintenance', siteKeys: ['sun'],
    summary: `AI 教材（sun.bdfz.net）「教材下載」書目與國家中小學智慧教育平臺同步：新增 ${rec.added} 冊，${rec.withdrawn} 冊不再列出（平臺已下架或同一冊的舊紀錄）。`,
    details: (rec.added ? `新增：${byText(rec.added_by_stage_subject)}。` : '') + (rec.withdrawn ? `不再列出：${byText(rec.withdrawn_by_stage_subject)}（其中平臺已下架 ${removedOffline} 冊，其餘為同一冊重複上架的舊紀錄，保留較新的一筆）。` : '') +
      ((rec.changed || {}).publisher ? `另有 ${rec.changed.publisher} 冊依平臺紀錄更正出版社。` : '') +
      `平臺資料版本 ${rec.module_version}。每冊下載時仍直接取官方平臺當前的文件。`,
    effectiveAt: acc.effective_at, publishedAt: now(), verification: 'verified' };
  const evidencePath = acc.evidence;
  return { update, releases: [{ projectId: PID, sourceCommit: acc.source_commit, deployment: acc.deployment, siteKeys: ['sun'], evidencePath, evidenceSha256: sha(fs.readFileSync(evidencePath)) }] };
}

async function promote(rec, basis, local) {
  const p0 = project(), acc0 = p0.accepted;
  const live = ssh(`cat ${HROOT}/data/catalog/CURRENT`).trim();
  if (rec.parent !== live) throw new Error(`candidate was built on ${rec.parent}, live is ${live}`);
  if (acc0.catalog_release !== live) throw new Error(`registry names ${acc0.catalog_release}, live is ${live}`);
  const image = ssh(`docker inspect ${'textbook-knowledge'} --format '{{.Image}}'`).trim();
  if (image !== acc0.image_id) throw new Error('running image is not the accepted one');
  const health = await getJson(SITE + '/api/health'); if (health.status !== 'ok') throw new Error('site health ' + health.status);
  const runDir = path.join(EVIDENCE_ROOT, rec.release_id); fs.mkdirSync(runDir, { recursive: true });
  fs.writeFileSync(runDir + '/candidate.json', JSON.stringify(rec, null, 2) + '\n');
  // switch (host verifies the candidate's checksums and that CURRENT is still the parent)
  const SWITCH = `import json,hashlib,os,sys
root='${HROOT}/data/catalog'; cand,parent=sys.argv[1],sys.argv[2]
cur=open(root+'/CURRENT').read().strip()
assert cur==parent, 'CURRENT moved: '+cur
rel=root+'/releases/'+cand
for line in open(rel+'/SHA256SUMS').read().splitlines():
    d,n=line.split('  ',1); assert hashlib.sha256(open(rel+'/'+n,'rb').read()).hexdigest()==d, n
tmp=root+'/.CURRENT.refresh.tmp'; open(tmp,'w').write(cand+'\\n'); os.replace(tmp,root+'/CURRENT')
keep='${HSTATE}/keep.json'; json.dump({'ids':[parent,cand],'at':'${now()}'},open(keep,'w'))
print('switched')`;
  const switched = ssh(`python3 - ${rec.release_id} ${live}`, SWITCH).trim();
  if (switched !== 'switched') throw new Error('switch not confirmed: ' + switched);
  const t0 = Date.now(); let seen = null;
  while (Date.now() - t0 < 180000) { try { seen = await getJson(SITE + '/api/library/release'); if (seen.release_id === rec.release_id) break; } catch { /* retry */ } await sleep(10000); }
  const ok = seen && seen.release_id === rec.release_id && seen.counts?.downloads === rec.loaded.downloads;
  if (!ok) {
    ssh(`python3 -c "import os;r='${HROOT}/data/catalog';open(r+'/.CURRENT.refresh.tmp','w').write('${live}\\n');os.replace(r+'/.CURRENT.refresh.tmp',r+'/CURRENT')"`);
    fs.writeFileSync(runDir + '/rolled-back.json', JSON.stringify({ at: now(), seen, expected: { release_id: rec.release_id, downloads: rec.loaded.downloads }, restored: live }, null, 2) + '\n');
    throw new Error('site did not confirm the switch; CURRENT written back to ' + live);
  }
  const effectiveAt = now();
  const facets = await getJson(SITE + '/api/library/downloads/facets?region=CN');
  const sample = (rec.added_ids_sample || [])[0];
  let link = null;
  if (sample) { const r = await fetch('https://jks-ai.bdfz.net/pdf/' + sample, { signal: AbortSignal.timeout(60000) }); link = { id: sample, status: r.status, type: r.headers.get('content-type') }; try { await r.body?.cancel(); } catch { /* */ } }
  fs.writeFileSync(runDir + '/production.json', JSON.stringify({ at: effectiveAt, release: seen, facets: { total: facets.total, subjects: facets.subjects.length }, sample_link: link, basis }, null, 2) + '\n');
  // live identity readback (the Status adapter compares it with the registry)
  ssh(`cat > ${HSTATE}/vps-sun-readback.py`, fs.readFileSync(READBACK, 'utf8'));
  const readback = ssh(`python3 ${HSTATE}/vps-sun-readback.py`); const rb = JSON.parse(readback);
  if (rb.catalog_release !== rec.release_id || rb.image_id !== acc0.image_id || !rb.health_ok) throw new Error('readback does not match the switch');
  const evidence = runDir + '/sun-live-readback.json'; fs.writeFileSync(evidence, readback);
  const acc = await withRegistry('sun-catalog-refresh-' + rec.release_id, (reg, p) => {
    if (p.accepted.catalog_release !== live) throw new Error('registry catalog moved');
    p.accepted = { ...p.accepted, catalog_release: rec.release_id, catalog_previous: live, catalog_accepted_at: now(), catalog_effective_at: effectiveAt,
      evidence, evidence_sha256: sha(fs.readFileSync(evidence)), live_readback_evidence_sha256: sha(fs.readFileSync(evidence)) };
    for (const k of ['public_update', 'status_publication', 'status_public_url', 'status_evidence']) delete p.accepted[k];
    const ch = p.catalog_refresh_channel;
    ch.runs = [...(ch.runs || []), { at: now(), release_id: rec.release_id, parent: live, module_version: rec.module_version, added: rec.added, withdrawn: rec.withdrawn,
      held_reasons: rec.reasons || [], approved_by: basis || null, evidence: runDir }].slice(-20);
    ch.last_promotion = { at: now(), release_id: rec.release_id };
    p.observed = { at: now(), health: 'ok', host: HOST, identity_evidence: evidence };
    return { ...p.accepted, effective_at: effectiveAt };
  });
  local.promoted = [...(local.promoted || []), rec.release_id].slice(-20);
  // Status, only when the list changed
  if (rec.added || rec.withdrawn || (rec.changed || {}).publisher) {
    const note = statusNote(rec, runDir, acc); const notePath = runDir + '/STATUS-NOTE.json';
    fs.writeFileSync(notePath, JSON.stringify(note, null, 1) + '\n', { mode: 0o600 });
    execFileSync(process.execPath, [PUBLISHER, notePath, '--check'], { encoding: 'utf8', timeout: 300000 });
    execFileSync(process.execPath, [PUBLISHER, notePath, '--publish'], { encoding: 'utf8', timeout: 300000 });
    const u = note.update, checks = [];
    for (const pth of ['/api/updates/' + u.id + '/' + u.revision, '/rss.xml', '/rss.xml?site=sun']) {
      const r = await fetch('https://status.bdfz.net' + pth, { cache: 'no-store', signal: AbortSignal.timeout(20000) }); const b = await r.text();
      if (r.status !== 200 || !b.includes(u.id) || (pth.startsWith('/api/') ? JSON.parse(b).summary !== u.summary : !b.includes(u.summary))) throw new Error('Status read-back failed: ' + pth);
      checks.push({ path: pth, status: 200, matched: true });
    }
    const url = 'https://status.bdfz.net/?update=' + u.id + '&revision=' + u.revision + '#updates';
    fs.writeFileSync(runDir + '/STATUS-PUBLIC.json', JSON.stringify({ at: now(), passed: true, url, checks }, null, 2) + '\n');
    await withRegistry('sun-catalog-refresh-status-' + rec.release_id, (reg, p) => {
      if (p.accepted.catalog_release !== rec.release_id) throw new Error('catalog moved before Status fields');
      Object.assign(p.accepted, { public_update: url, status_publication: 'published', status_public_url: url, status_evidence: [notePath, runDir + '/STATUS-PUBLIC.json'] });
    });
  }
  say({ promoted: rec.release_id, parent: live, added: rec.added, withdrawn: rec.withdrawn, evidence: runDir });
}

const [mode, arg, basis] = process.argv.slice(2);
const local = readJson(LOCAL_STATE, {});
try {
  if (mode === 'status') { console.log(ssh(`python3 ${HSTATE}/tool/catalog_refresh_host.py status`)); console.log(JSON.stringify(project().catalog_refresh_channel, null, 1)); process.exit(0); }
  if (!['run', 'approve'].includes(mode)) throw new Error('usage: run | approve <release_id> "<basis>" | status');
  const ch = channelCheck(mode);
  if (!ch) { say({ skipped: 'channel is not active for automatic promotion' }); process.exit(0); }
  const rec = JSON.parse(ssh(`cat ${HSTATE}/latest.json 2>/dev/null || echo '{}'`) || '{}');
  if (!rec.release_id) { say({ nothing: 'no candidate yet' }); process.exit(0); }
  const live = ssh(`cat ${HROOT}/data/catalog/CURRENT`).trim();
  if (rec.release_id === live || (local.promoted || []).includes(rec.release_id)) { say({ nothing: 'newest candidate is live', live }); process.exit(0); }
  if (mode === 'run') {
    if (rec.status === 'held') { noteHeld(rec, local); say({ held: rec.release_id, reasons: rec.reasons }); }
    else if (rec.status !== 'ready') say({ nothing: `newest candidate is ${rec.status}`, candidate: rec.release_id });
    else if (rec.subjects_new?.some((s) => !HANT[s])) { rec.status = 'held'; rec.reasons = [...(rec.reasons || []), 'new subject without a reviewed name: ' + rec.subjects_new.join(', ')]; noteHeld(rec, local); say({ held: rec.release_id, reasons: rec.reasons }); }
    else await promote(rec, null, local);
  } else {
    if (arg !== rec.release_id) throw new Error(`newest candidate is ${rec.release_id}, not ${arg}`);
    if (!['ready', 'held'].includes(rec.status)) throw new Error('candidate status ' + rec.status);
    if (!basis || basis.length < 10) throw new Error('approval basis required');
    await promote(rec, basis, local);
  }
} catch (e) {
  say({ error: String(e.message || e).slice(0, 400) }); process.exitCode = 1;
} finally {
  fs.writeFileSync(LOCAL_STATE, JSON.stringify(local, null, 1) + '\n');
}
