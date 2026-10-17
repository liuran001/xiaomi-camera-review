'use strict';
(async () => {
  const script = new URL(document.currentScript.src);
  const index = new URL('../', script);
  const endpoint = new URL('token', script);
  const status = document.getElementById('dsm-status');
  const signIn = document.getElementById('dsm-signin');
  const open = document.getElementById('dsm-open');
  const attemptKey = 'camera-dsm-bridge:' + index.href;
  let storageAvailable = false;
  try {
    // Nonsecret cooldown only. No token or authenticated identity is stored here.
    const lastAttempt = Number(sessionStorage.getItem(attemptKey));
    if (lastAttempt && Date.now() - lastAttempt < 60000) {
      throw new Error('recent bridge attempt');
    }
    storageAvailable = true;
  } catch (error) {
    if (error.message === 'recent bridge attempt') {
      status.textContent = 'DSM 登录验证未完成，请重新登录 DSM 后打开录像回看。';
      signIn.hidden = false;
      return;
    }
  }
  try {
    const response = await fetch('/webapi/entry.cgi?api=SYNO.API.Auth&version=6&method=token', {
      credentials:'same-origin', cache:'no-store', redirect:'error',
      // DSM 7.4 rejects token retrieval without its same-origin referrer check.
      referrerPolicy:'same-origin',
    });
    if (!response.ok) throw new Error('DSM token unavailable');
    const result = await response.json();
    if (!result.success || !result.data || typeof result.data.synotoken !== 'string' || !result.data.synotoken) {
      throw new Error('DSM token unavailable');
    }
    const bridge = await fetch(endpoint.href, {
      method:'POST', credentials:'same-origin', cache:'no-store', redirect:'error',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({token:result.data.synotoken}),
    });
    if (!bridge.ok) throw new Error('DSM login unavailable');
    // Limit automatic reloads even when cookies are disabled or the session changes.
    if (storageAvailable) {
      try {
        sessionStorage.setItem(attemptKey, String(Date.now()));
        location.reload();
        return;
      } catch (_) { /* Show a manual action when storage is disabled. */ }
    }
    status.textContent = 'DSM 登录已验证，请打开录像回看。';
    open.href = index.href;
    open.hidden = false;
  } catch (_) {
    status.textContent = '请先登录 DSM，再打开录像回看。';
    signIn.hidden = false;
  }
})();
