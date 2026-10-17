'use strict';
const $ = id => document.getElementById(id);
const MAX_BYTES = 10 * 1024 * 1024;
// Public design-preview selector, NOT a credential. Never sent to the API.
const DEMO_KEY = 'vault-demo';

// Application state is kept in memory. The real business key is never placed
// in localStorage, cookies, URLs, or the demo data shown by the landing page.
let serverDemo = false, demo = false, pageInvoices = [], refreshGeneration = 0;
let token = '', epoch = 0, offset = 0, total = 0, busy = false;
let stream = null, cameraGeneration = 0, photo = null, previewURL = null;
const demoInvoices = [
  ['DEMO-1003', 'Oak Street Studio', 'USD 2,450.00', '2026-09-12', false, 184320],
  ['DEMO-1002', 'Juniper & Co.', 'USD 875.00', '2026-09-11', true, 126976],
  ['DEMO-1001', 'Example Customer LLC', 'USD 1,080.00', '2026-09-10', false, 96256]
].map(([number, customer, amount, date, reviewed, bytes], index) => ({
  id: 'demo-' + index, filename: number.toLowerCase() + '.pdf',
  notes: 'Fictional design sample — not payable.', content_type: 'application/pdf',
  created_at: date + 'T16:00:00Z', size: bytes, invoice_date: date,
  invoice_number: number, total_quoted_amount: amount, customer_name: customer,
  reviewed_at: reviewed ? date + 'T17:00:00Z' : '',
  extraction_message: 'Fictional preview only. Editing, uploads, and document actions are disabled.'
}));

function status(message) { $('status').textContent = message; }
function showError(error) { if (error.name !== 'AbortError') status(error.message); }
function stale() { return new DOMException('Session changed.', 'AbortError'); }
function size(bytes) { return (bytes / 1048576).toFixed(2) + ' MiB'; }
function inlineError(id, message = '') {
  $(id).textContent = message;
  $(id).hidden = !message;
}

// The single API wrapper keeps demo behavior, bearer authentication, and
// session-change checks in one place so individual UI actions stay simple.
async function api(path, options = {}) {
  const current = epoch;
  if (demo && !serverDemo) {
    if ((!options.method || options.method === 'GET') && path === '/api/me') {
      return new Response(JSON.stringify({name: 'Demo Studio · preview'}));
    }
    if ((!options.method || options.method === 'GET') && path.startsWith('/api/invoices?')) {
      return new Response(JSON.stringify({invoices: demoInvoices, total: demoInvoices.length,
        bytes: demoInvoices.reduce((sum, invoice) => sum + invoice.size, 0)}));
    }
    throw new Error('Read-only demo. Sign in with a real business key to manage invoices.');
  }
  const response = await fetch(path, {...options, headers: {...options.headers, Authorization: 'Bearer ' + token}});
  if (current !== epoch) throw stale();
  if (!response.ok) {
    let message = 'Request failed (' + response.status + ').';
    try { message = (await response.json()).error || message; } catch (_) { /* Non-JSON proxy response. */ }
    throw new Error(message);
  }
  return response;
}

function setBusy(value) {
  busy = value;
  $('upload-controls').disabled = value || demo;
  $('notes').disabled = value || demo;
  $('dropzone').setAttribute('aria-disabled', String(value || demo));
  $('logout').disabled = value;
}

function stopCamera() {
  cameraGeneration++;
  if (stream) stream.getTracks().forEach(track => track.stop());
  stream = null;
  $('video').srcObject = null;
  $('camera-panel').hidden = true;
}

function discardPhoto() {
  if (previewURL) URL.revokeObjectURL(previewURL);
  previewURL = null;
  photo = null;
  $('preview').removeAttribute('src');
  $('preview-panel').hidden = true;
}

function resetFilters() {
  $('invoice-search').value = '';
  $('review-filter').value = 'all';
}

// Theme preference is shared by both switches because they update the same root
// data attribute and the same browser preference.
function applyTheme(theme) {
  const dark = theme === 'dark';
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  document.querySelectorAll('.theme-toggle').forEach(toggle => {
    toggle.checked = dark;
    toggle.setAttribute('aria-checked', String(dark));
  });
}

function setThemeFromPreference() {
  let saved = '';
  try { saved = localStorage.getItem('invoice-vault-theme') || ''; } catch (_) { /* Storage may be unavailable. */ }
  const systemTheme = window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  applyTheme(saved || systemTheme);
}

function toggleTheme(event) {
  const toggle = event?.currentTarget || document.querySelector('.theme-toggle');
  const theme = toggle?.checked ? 'dark' : 'light';
  applyTheme(theme);
  try { localStorage.setItem('invoice-vault-theme', theme); } catch (_) { /* Keep the toggle functional without storage. */ }
}

function hideKey() {
  $('access-key').type = 'password';
  $('toggle-key').textContent = 'Show';
  $('toggle-key').setAttribute('aria-label', 'Show access key');
  $('toggle-key').setAttribute('aria-pressed', 'false');
}

function signOut() {
  epoch++;
  refreshGeneration++;
  token = '';
  demo = false;
  pageInvoices = [];
  setBusy(false);
  stopCamera();
  discardPhoto();
  $('workspace').hidden = true;
  $('login').hidden = false;
  $('access-key').value = '';
  hideKey();
  resetFilters();
  inlineError('login-error');
  inlineError('library-error');
  $('notes').value = '';
  $('rows').replaceChildren();
  $('upload-results').replaceChildren();
  $('business-name').textContent = '';
  $('files').value = '';
  $('photo').value = '';
  $('demo-banner').hidden = true;
  status('Signed out.');
  $('access-key').focus();
}

function fieldReview(invoice) {
  const cell = document.createElement('td');
  cell.className = 'invoice-fields';
  const fields = [
    ['invoice_date', 'Invoice date', 'date'],
    ['invoice_number', 'Invoice number', 'text'],
    ['total_quoted_amount', 'Total quoted amount', 'text'],
    ['customer_name', 'Customer name', 'text']
  ];
  for (const [key, label] of fields) {
    const line = document.createElement('small');
    line.textContent = label + ': ' + (invoice[key] || 'Not found / unspecified');
    cell.append(line);
  }
  const state = document.createElement('p');
  state.className = 'review-state' + (invoice.reviewed_at ? ' reviewed' : '');
  state.textContent = invoice.reviewed_at ? 'Reviewed' : 'Needs review';
  cell.append(state);
  const details = document.createElement('details');
  const summary = document.createElement('summary');
  summary.textContent = 'Review / edit fields';
  const message = document.createElement('p');
  message.textContent = invoice.extraction_message || 'This existing document has not been extracted. Use Extract fields, or enter values manually.';
  const form = document.createElement('form');
  const controls = document.createElement('fieldset');
  controls.disabled = demo;
  const inputs = {};
  for (const [key, title, type] of fields) {
    const label = document.createElement('label');
    label.textContent = title;
    const input = document.createElement('input');
    input.type = type;
    input.maxLength = 160;
    input.value = invoice[key] || '';
    if (key === 'total_quoted_amount') input.placeholder = 'For example: USD 1,250.00';
    label.append(input);
    controls.append(label);
    inputs[key] = input;
  }
  const help = document.createElement('p');
  help.className = 'muted';
  help.textContent = 'Compare with the downloaded original. Amounts retain the printed currency; nothing is calculated. Leave unknown fields blank. Save before refreshing or changing pages.';
  const save = document.createElement('button');
  save.type = 'submit';
  save.textContent = 'Save reviewed fields';
  controls.append(help, save);
  form.append(controls);
  form.onsubmit = async event => {
    event.preventDefault();
    const values = Object.fromEntries(fields.map(([key]) => [key, inputs[key].value.trim()]));
    controls.disabled = true;
    try {
      await api('/api/invoices/' + invoice.id + '/fields', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(values)
      });
      await refresh();
      status('Reviewed fields saved. The original document is unchanged.');
    } catch (error) { showError(error); }
    finally { controls.disabled = demo; }
  };
  details.append(summary, message, form);
  if (!invoice.reviewed_at && invoice.content_type === 'application/pdf') {
    const extract = document.createElement('button');
    extract.type = 'button';
    extract.className = 'secondary';
    extract.textContent = 'Extract fields';
    extract.disabled = demo;
    extract.onclick = async () => {
      if (!confirm('Extract fresh suggestions? Any unsaved edits on this page will be discarded.')) return;
      extract.disabled = true;
      controls.disabled = true;
      status('Extracting invoice fields…');
      try {
        await api('/api/invoices/' + invoice.id + '/extract', {method: 'POST', body: ''});
        await refresh();
        status('Extraction finished. Open Review / edit fields to check the result.');
      } catch (error) { showError(error); }
      finally { extract.disabled = demo; controls.disabled = demo; }
    };
    details.append(extract);
  }
  cell.append(details);
  return cell;
}

function applyFilters() {
  const query = $('invoice-search').value.trim().toLocaleLowerCase();
  const filter = $('review-filter').value;
  let matches = 0;
  Array.from($('rows').children).forEach((row, index) => {
    const invoice = pageInvoices[index];
    const searchable = [invoice.filename, invoice.notes, invoice.invoice_date,
      invoice.invoice_number, invoice.total_quoted_amount, invoice.customer_name].join(' ').toLocaleLowerCase();
    const visible = searchable.includes(query) && (filter === 'all' ||
      (filter === 'reviewed' ? Boolean(invoice.reviewed_at) : !invoice.reviewed_at));
    row.hidden = !visible;
    if (visible) matches++;
  });
  $('empty').hidden = total !== 0;
  $('no-matches').hidden = total === 0 || matches !== 0;
  $('table').hidden = matches === 0;
  $('filter-count').textContent = `${matches} of ${pageInvoices.length} on this page`;
}

// Re-render the current page from the server. A generation number prevents an
// older, slower response from replacing newer results after repeated refreshes.
async function refresh() {
  const current = epoch, generation = ++refreshGeneration;
  inlineError('library-error');
  $('library').setAttribute('aria-busy', 'true');
  $('refresh').disabled = true;
  try {
    const response = await api('/api/invoices?offset=' + offset);
    const data = await response.json();
    if (current !== epoch || generation !== refreshGeneration) throw stale();
    total = data.total;
    if (offset >= total && offset > 0) {
      offset = Math.max(0, Math.floor((total - 1) / 50) * 50);
      return await refresh();
    }
    pageInvoices = data.invoices;
    $('count').textContent = String(total);
    $('review-count').textContent = String(pageInvoices.filter(invoice => !invoice.reviewed_at).length);
    $('storage').textContent = size(data.bytes);
    $('storage-meter').value = data.bytes;
    $('previous').disabled = offset === 0;
    $('next').disabled = offset + 50 >= total;
    $('page').textContent = total ? `${offset + 1}–${Math.min(offset + 50, total)} of ${total}` : '0 invoices';
    $('rows').replaceChildren();
    for (const invoice of pageInvoices) {
      const row = document.createElement('tr');
      const name = document.createElement('td');
      const kind = document.createElement('span');
      kind.className = 'document-kind';
      kind.textContent = invoice.content_type === 'application/pdf' ? 'PDF' : 'IMAGE';
      const title = document.createElement('strong');
      title.textContent = invoice.filename;
      const notes = document.createElement('small');
      notes.textContent = invoice.notes;
      name.append(kind, title, notes);
      const date = document.createElement('td');
      date.textContent = new Date(invoice.created_at).toLocaleString();
      const bytes = document.createElement('td');
      bytes.textContent = size(invoice.size);
      const actions = document.createElement('td');
      const download = document.createElement('button');
      download.className = 'secondary';
      download.textContent = 'Download';
      download.disabled = demo && !serverDemo;
      download.setAttribute('aria-label', 'Download ' + invoice.filename);
      download.onclick = async () => {
        download.disabled = true;
        const session = epoch;
        try {
          const response = await api('/api/invoices/' + invoice.id);
          const blob = await response.blob();
          if (session !== epoch) throw stale();
          const url = URL.createObjectURL(blob);
          const link = document.createElement('a');
          link.href = url;
          link.download = invoice.filename;
          document.body.append(link);
          link.click();
          link.remove();
          setTimeout(() => URL.revokeObjectURL(url), 60000);
        } catch (error) { showError(error); }
        finally { download.disabled = demo && !serverDemo; }
      };
      const remove = document.createElement('button');
      remove.className = 'danger';
      remove.textContent = 'Delete';
      remove.disabled = demo;
      remove.setAttribute('aria-label', 'Delete ' + invoice.filename);
      if (demo) {
        download.title = remove.title = 'Unavailable for fictional design samples.';
      }
      remove.onclick = async () => {
        if (!confirm('Permanently delete “' + invoice.filename + '”? This cannot be undone in the app.')) return;
        remove.disabled = true;
        try {
          await api('/api/invoices/' + invoice.id, {method: 'DELETE'});
          await refresh();
          status('Invoice deleted.');
        } catch (error) { showError(error); }
        finally { remove.disabled = demo; }
      };
      if (serverDemo) {
        const view = document.createElement('a');
        view.className = 'button secondary';
        view.textContent = 'View';
        view.href = '/api/invoices/' + invoice.id + '/view';
        view.target = '_blank';
        view.rel = 'noopener';
        actions.append(view);
        download.title = 'Download fictional invoice';
      }
      actions.append(download, remove);
      row.append(name, fieldReview(invoice), date, bytes, actions);
      $('rows').append(row);
    }
    applyFilters();
  } catch (error) {
    if (current === epoch && generation === refreshGeneration && error.name !== 'AbortError') {
      inlineError('library-error', error.message + ' Use Refresh to try again. Any visible invoices are from the last successful load.');
    }
    throw error;
  } finally {
    if (generation === refreshGeneration) {
      $('library').setAttribute('aria-busy', 'false');
      $('refresh').disabled = false;
    }
  }
}

// Uploads use XMLHttpRequest rather than fetch so the progress bar can report
// bytes sent. The server deduplicates retries by the file digest.
function uploadOne(file, notes) {
  if (demo) return Promise.reject(new Error('Uploads are disabled in the read-only demo.'));
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const current = epoch;
    xhr.open('POST', '/api/invoices');
    xhr.timeout = 120000;
    xhr.setRequestHeader('Authorization', 'Bearer ' + token);
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    xhr.setRequestHeader('X-Filename', encodeURIComponent(file.name));
    xhr.setRequestHeader('X-Notes', encodeURIComponent(notes));
    xhr.upload.onprogress = event => {
      if (event.lengthComputable) $('progress').value = event.loaded / event.total * 100;
    };
    xhr.onload = () => {
      if (current !== epoch) return reject(stale());
      let result;
      try { result = JSON.parse(xhr.responseText); }
      catch (_) { return reject(new Error('Unexpected response. Refresh to check whether this upload saved.')); }
      if (xhr.status >= 200 && xhr.status < 300) resolve(result);
      else reject(new Error(result.error || 'Upload failed.'));
    };
    xhr.onerror = xhr.ontimeout = () => reject(new Error('Connection interrupted. Refresh to check whether this upload saved; identical retries are deduplicated.'));
    xhr.onabort = () => reject(stale());
    xhr.send(file);
  });
}

async function uploadFiles(files) {
  if (demo) { status('Read-only design demo. Sign in with a business key to upload real documents.'); return false; }
  if (busy || !token) return false;
  files = Array.from(files);
  if (!files.length) return false;
  if (files.length > 20) { status('Choose at most 20 files per batch.'); return false; }
  stopCamera();
  setBusy(true);
  $('upload-results').replaceChildren();
  $('progress').hidden = false;
  const notes = $('notes').value;
  let saved = 0;
  try {
    for (const file of files) {
      $('progress').value = 0;
      status('Uploading ' + file.name + '…');
      const item = document.createElement('li');
      try {
        if (!file.size || file.size > MAX_BYTES) throw new Error('File must be nonempty and at most 10 MiB.');
        const result = await uploadOne(file, notes);
        item.textContent = file.name + (result.duplicate ? ' — already in your vault; no duplicate created.' : ' — saved; review the extracted fields below.');
        saved++;
      } catch (error) {
        item.textContent = file.name + ' — ' + error.message;
        item.className = 'error';
      }
      $('upload-results').append(item);
    }
    status(`${saved} of ${files.length} files saved or already present.`);
    offset = 0;
    resetFilters();
    await refresh();
  } catch (error) { showError(error); }
  finally {
    setBusy(false);
    $('progress').hidden = true;
    $('files').value = '';
    $('photo').value = '';
  }
  return saved === files.length;
}

$('login-form').onsubmit = async event => {
  event.preventDefault();
  const button = $('login-submit');
  if (button.disabled) return;
  button.disabled = true;
  inlineError('login-error');
  const entered = $('access-key').value.trim();
  demo = serverDemo || entered === DEMO_KEY;
  token = demo ? '' : entered;
  const current = ++epoch;
  status('Opening workspace…');
  try {
    const response = await api('/api/me');
    const business = await response.json();
    if (current !== epoch) throw stale();
    $('business-name').textContent = business.name;
    $('business-initial').textContent = business.name.charAt(0).toUpperCase();
    offset = 0;
    resetFilters();
    await refresh();
    if (current !== epoch) throw stale();
    setBusy(false);
    $('demo-banner').hidden = !demo;
    $('session-label').textContent = demo ? 'Fictional data · read-only demo' : 'Private business workspace';
    $('login').hidden = true;
    $('workspace').hidden = false;
    $('access-key').value = '';
    hideKey();
    updateNavigation();
    $('overview').focus();
    status(demo ? 'Demo ready. Fictional data only; uploads and changes are disabled.' : 'Workspace ready.');
  } catch (error) {
    if (current === epoch) {
      token = '';
      demo = false;
      if (error.name !== 'AbortError') inlineError('login-error', error.message);
      showError(error);
    }
  } finally { button.disabled = false; }
};
setThemeFromPreference();
document.querySelectorAll('.theme-toggle').forEach(toggle => { toggle.onchange = toggleTheme; });
$('toggle-key').onclick = () => {
  const show = $('access-key').type === 'password';
  $('access-key').type = show ? 'text' : 'password';
  $('toggle-key').textContent = show ? 'Hide' : 'Show';
  $('toggle-key').setAttribute('aria-label', (show ? 'Hide' : 'Show') + ' access key');
  $('toggle-key').setAttribute('aria-pressed', String(show));
};
$('invoice-search').oninput = applyFilters;
$('review-filter').onchange = applyFilters;
$('clear-filters').onclick = () => { resetFilters(); applyFilters(); $('invoice-search').focus(); };
function updateNavigation() {
  const section = ['#overview', '#library', '#add-invoices'].includes(location.hash) ? location.hash : '#overview';
  document.querySelectorAll('.side-nav a').forEach(link => {
    if (link.getAttribute('href') === section) {
      link.setAttribute('aria-current', 'location');
      $('section-label').textContent = link.textContent.trim().replace(/^[^A-Za-z]+/, '');
    } else link.removeAttribute('aria-current');
  });
}
window.addEventListener('hashchange', updateNavigation);
$('logout').onclick = signOut;
$('refresh').onclick = () => refresh().catch(showError);
$('previous').onclick = () => { offset = Math.max(0, offset - 50); refresh().catch(showError); };
$('next').onclick = () => { if (offset + 50 < total) offset += 50; refresh().catch(showError); };
$('files').onchange = event => uploadFiles(event.target.files);
$('photo').onchange = event => uploadFiles(event.target.files);
$('camera-file').onclick = () => { if (!demo) { stopCamera(); $('photo').click(); } };
$('dropzone').onclick = () => { if (!busy && !demo) $('files').click(); };
$('dropzone').onkeydown = event => {
  if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); if (!busy && !demo) $('files').click(); }
};
for (const type of ['dragenter', 'dragover']) {
  $('dropzone').addEventListener(type, event => { event.preventDefault(); if (!busy && !demo) $('dropzone').classList.add('dragging'); });
}
for (const type of ['dragleave', 'drop']) {
  $('dropzone').addEventListener(type, event => { event.preventDefault(); $('dropzone').classList.remove('dragging'); });
}
$('dropzone').addEventListener('drop', event => uploadFiles(event.dataTransfer.files));
window.addEventListener('dragover', event => event.preventDefault());
window.addEventListener('drop', event => event.preventDefault());
$('camera-start').onclick = async () => {
  if (demo || busy || !token) return;
  stopCamera();
  discardPhoto();
  if (!navigator.mediaDevices?.getUserMedia) {
    status('Live camera needs HTTPS or localhost. Use “Take or choose a photo” instead.');
    return;
  }
  const generation = cameraGeneration;
  $('camera-start').disabled = true;
  try {
    const acquired = await navigator.mediaDevices.getUserMedia({video: {facingMode: {ideal: 'environment'}}, audio: false});
    if (generation !== cameraGeneration || !token || busy) { acquired.getTracks().forEach(track => track.stop()); return; }
    stream = acquired;
    $('video').srcObject = stream;
    $('camera-panel').hidden = false;
    await $('video').play();
    status('Position the invoice so all text is visible, then capture.');
  } catch (_) { stopCamera(); status('Camera unavailable or permission denied. Use “Take or choose a photo” or upload a file.'); }
  finally { $('camera-start').disabled = false; }
};
$('camera-stop').onclick = stopCamera;
$('capture').onclick = () => {
  const video = $('video');
  if (!video.videoWidth || !video.videoHeight) { status('Wait for the camera to be ready.'); return; }
  const current = epoch;
  const generation = cameraGeneration;
  const canvas = document.createElement('canvas');
  const scale = Math.min(1, 2400 / Math.max(video.videoWidth, video.videoHeight));
  canvas.width = Math.round(video.videoWidth * scale);
  canvas.height = Math.round(video.videoHeight * scale);
  canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
  canvas.toBlob(blob => {
    if (current !== epoch || generation !== cameraGeneration) return;
    if (!blob) { status('Could not capture this photo. Please try again.'); return; }
    stopCamera();
    discardPhoto();
    photo = new File([blob], 'invoice-' + new Date().toISOString().replace(/[:.]/g, '-') + '.jpg', {type: 'image/jpeg'});
    previewURL = URL.createObjectURL(blob);
    $('preview').src = previewURL;
    $('preview-panel').hidden = false;
    status('Check that the invoice is readable before uploading.');
  }, 'image/jpeg', 0.92);
};
$('save-photo').onclick = async () => { if (photo && await uploadFiles([photo])) discardPhoto(); };
$('discard-photo').onclick = discardPhoto;
document.addEventListener('visibilitychange', () => { if (document.hidden) stopCamera(); });
window.addEventListener('pagehide', () => { stopCamera(); discardPhoto(); token = ''; demo = false; epoch++; });
window.addEventListener('pageshow', event => { if (event.persisted) signOut(); });

// Detect the server-enforced public demo; the private key flow stays unchanged.
fetch('/api/config').then(response => response.json()).then(config => {
  if (!config.demo) return;
  serverDemo = true;
  $('access-key').value = DEMO_KEY;
  $('login-form').requestSubmit();
}).catch(() => {});
