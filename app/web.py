"""The browser UI, kept as a single self contained page.

A converter with no interface is hard to demonstrate, and a build step for one
HTML file would be more machinery than the page is worth. Everything here is
plain HTML, CSS and JavaScript with no dependencies.
"""

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PDF to Markdown</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #f1f5f9;
    --surface: #ffffff;
    --border: #e2e8f0;
    --text: #0f172a;
    --muted: #64748b;
    --brand: #1c44f5;
    --danger: #dc2626;
    --warning-bg: #fef3c7;
    --warning-text: #92400e;
  }

  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #020617;
      --surface: #0f172a;
      --border: #1e293b;
      --text: #e2e8f0;
      --muted: #94a3b8;
      --brand: #588eff;
      --warning-bg: #422006;
      --warning-text: #fcd34d;
    }
  }

  * { box-sizing: border-box; }

  body {
    margin: 0;
    padding: 2rem 1rem;
    background: var(--bg);
    color: var(--text);
    font: 15px/1.6 system-ui, -apple-system, "Segoe UI", sans-serif;
  }

  main { max-width: 900px; margin: 0 auto; }

  h1 { margin: 0 0 .25rem; font-size: 1.6rem; }

  .subtitle { margin: 0 0 1.5rem; color: var(--muted); }

  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 1.5rem;
    margin-bottom: 1rem;
  }

  .dropzone {
    border: 2px dashed var(--border);
    border-radius: 12px;
    padding: 2.5rem 1.5rem;
    text-align: center;
    cursor: pointer;
    transition: border-color .15s, background .15s;
  }

  .dropzone:hover, .dropzone.dragging {
    border-color: var(--brand);
    background: color-mix(in srgb, var(--brand) 6%, transparent);
  }

  .dropzone strong { display: block; margin-bottom: .25rem; }

  .dropzone span { color: var(--muted); font-size: .9rem; }

  input[type=file] { display: none; }

  .row { display: flex; flex-wrap: wrap; gap: .75rem; align-items: center; margin-top: 1rem; }

  button {
    background: var(--brand);
    color: #fff;
    border: 0;
    border-radius: 8px;
    padding: .6rem 1.1rem;
    font: inherit;
    font-weight: 500;
    cursor: pointer;
  }

  button:disabled { opacity: .55; cursor: not-allowed; }

  button.secondary {
    background: transparent;
    color: var(--text);
    border: 1px solid var(--border);
  }

  label.checkbox { display: flex; align-items: center; gap: .4rem; color: var(--muted); font-size: .9rem; }

  pre {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 1rem;
    max-height: 480px;
    overflow: auto;
    white-space: pre-wrap;
    word-break: break-word;
    font: 13px/1.5 ui-monospace, "Cascadia Code", Consolas, monospace;
  }

  .message { padding: .75rem 1rem; border-radius: 8px; margin-bottom: 1rem; }

  .error { background: color-mix(in srgb, var(--danger) 12%, transparent); color: var(--danger); }

  .warning { background: var(--warning-bg); color: var(--warning-text); }

  .stats { display: flex; gap: 1.5rem; flex-wrap: wrap; color: var(--muted); font-size: .85rem; margin-bottom: 1rem; }

  .hidden { display: none; }

  footer { margin-top: 2rem; text-align: center; color: var(--muted); font-size: .85rem; }

  a { color: var(--brand); }
</style>
</head>
<body>
<main>
  <h1>PDF to Markdown</h1>
  <p class="subtitle">Headings are inferred from font sizes. Lists and tables are recovered where possible.</p>

  <div class="card">
    <div class="dropzone" id="dropzone">
      <strong>Drop a PDF here, or click to choose one</strong>
      <span id="limits">PDF only</span>
    </div>

    <input type="file" id="file" accept="application/pdf,.pdf">

    <div class="row">
      <button id="convert" disabled>Convert</button>
      <button class="secondary hidden" id="download">Download .md</button>
      <button class="secondary hidden" id="copy">Copy</button>
      <label class="checkbox"><input type="checkbox" id="tables" checked> Include tables</label>
      <span id="selected" style="color: var(--muted); font-size: .9rem;"></span>
    </div>
  </div>

  <div id="error" class="message error hidden"></div>
  <div id="warnings" class="message warning hidden"></div>

  <div class="card hidden" id="result">
    <div class="stats" id="stats"></div>
    <pre id="output"></pre>
  </div>

  <footer>
    <a href="/docs">API documentation</a>
  </footer>
</main>

<script>
const dropzone = document.getElementById('dropzone');
const fileInput = document.getElementById('file');
const convertButton = document.getElementById('convert');
const downloadButton = document.getElementById('download');
const copyButton = document.getElementById('copy');
const tablesCheckbox = document.getElementById('tables');
const selectedLabel = document.getElementById('selected');
const errorBox = document.getElementById('error');
const warningBox = document.getElementById('warnings');
const resultCard = document.getElementById('result');
const statsBox = document.getElementById('stats');
const output = document.getElementById('output');

let selectedFile = null;
let converted = null;

fetch('/api/limits')
  .then((response) => response.json())
  .then((limits) => {
    const megabytes = Math.round(limits.maxBytes / (1024 * 1024));
    document.getElementById('limits').textContent =
      `PDF only, up to ${megabytes}MB and ${limits.maxPages} pages`;
  })
  .catch(() => {});

dropzone.addEventListener('click', () => fileInput.click());

['dragenter', 'dragover'].forEach((event) =>
  dropzone.addEventListener(event, (e) => {
    e.preventDefault();
    dropzone.classList.add('dragging');
  }),
);

['dragleave', 'drop'].forEach((event) =>
  dropzone.addEventListener(event, (e) => {
    e.preventDefault();
    dropzone.classList.remove('dragging');
  }),
);

dropzone.addEventListener('drop', (event) => {
  if (event.dataTransfer.files.length) {
    selectFile(event.dataTransfer.files[0]);
  }
});

fileInput.addEventListener('change', () => {
  if (fileInput.files.length) {
    selectFile(fileInput.files[0]);
  }
});

function selectFile(file) {
  selectedFile = file;
  selectedLabel.textContent = file.name;
  convertButton.disabled = false;
  hide(errorBox);
}

function hide(element) {
  element.classList.add('hidden');
}

function show(element) {
  element.classList.remove('hidden');
}

function showError(message) {
  errorBox.textContent = message;
  show(errorBox);
  hide(resultCard);
  hide(warningBox);
}

convertButton.addEventListener('click', async () => {
  if (!selectedFile) return;

  convertButton.disabled = true;
  convertButton.textContent = 'Converting...';
  hide(errorBox);
  hide(warningBox);

  const body = new FormData();
  body.append('file', selectedFile);
  body.append('includeTables', tablesCheckbox.checked);

  try {
    const response = await fetch('/api/convert', { method: 'POST', body });
    const payload = await response.json();

    if (!response.ok) {
      const detail = payload.detail;
      showError(typeof detail === 'string' ? detail : detail?.message ?? 'Conversion failed.');
      return;
    }

    converted = payload;
    output.textContent = payload.content || '(no text was extracted)';

    statsBox.innerHTML = `
      <span>${payload.pageCount} page(s)</span>
      <span>${payload.blockCount} block(s)</span>
      <span>${payload.content.length.toLocaleString()} characters</span>`;

    show(resultCard);
    show(downloadButton);
    show(copyButton);

    if (payload.warnings?.length) {
      warningBox.textContent = payload.warnings.join(' ');
      show(warningBox);
    }
  } catch (error) {
    showError(error.message || 'Could not reach the server.');
  } finally {
    convertButton.disabled = false;
    convertButton.textContent = 'Convert';
  }
});

downloadButton.addEventListener('click', () => {
  if (!converted) return;

  const blob = new Blob([converted.content], { type: 'text/markdown;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');

  link.href = url;
  link.download = converted.filename;
  link.click();

  URL.revokeObjectURL(url);
});

copyButton.addEventListener('click', async () => {
  if (!converted) return;

  await navigator.clipboard.writeText(converted.content);
  copyButton.textContent = 'Copied';
  setTimeout(() => (copyButton.textContent = 'Copy'), 1500);
});
</script>
</body>
</html>
"""
