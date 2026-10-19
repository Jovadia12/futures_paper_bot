const form = document.getElementById('login');
const err = document.getElementById('error');
const btn = document.getElementById('submit');

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  err.textContent = '';
  btn.disabled = true;
  btn.textContent = 'Signing in…';
  try {
    const r = await fetch('/api/login', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: form.password.value }),
    });
    if (r.ok) { location.href = '/'; return; }
    const body = await r.json().catch(() => ({}));
    err.textContent = typeof body.detail === 'string' ? body.detail : `Sign-in failed (HTTP ${r.status})`;
  } catch {
    err.textContent = 'Cannot reach the dashboard service.';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Sign in';
  }
});
