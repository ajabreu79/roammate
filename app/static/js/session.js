// Keeps Clerk's session token fresh on every signed-in page (FR-AUTH-008).
//
// Clerk's __session cookie holds a token that lives about a minute. clerk-js
// renews it in the background, but only on a page that has loaded clerk-js,
// so base.html loads it everywhere and this file starts it. Two extra guards:
//
//   - When the tab comes back into view, renew straight away: browsers slow
//     timers in background tabs, so the background renewal may have lapsed.
//   - Before any POST form is sent, renew first. Otherwise a form that took
//     more than a minute to fill in arrives with an expired token, the server
//     treats it as signed out, and what the user typed is lost.
//
// If Clerk is slow or unreachable the form is sent anyway after a few
// seconds; the server then sends the browser to /auth/refresh.
(function () {
  'use strict';

  var RENEW_TIMEOUT_MS = 4000;
  var loading = null;

  function load() {
    if (!window.Clerk) return Promise.resolve(null);
    if (!loading) {
      loading = Promise.resolve(window.Clerk.loaded ? null : window.Clerk.load())
        .then(function () { return window.Clerk; })
        .catch(function () { return null; });
    }
    return loading;
  }

  // Session.getToken returns the cached token while it is still valid and
  // fetches a new one otherwise; clerk-js writes it to the __session cookie.
  function renew() {
    return load()
      .then(function (clerk) {
        return clerk && clerk.session ? clerk.session.getToken() : null;
      })
      .catch(function () { return null; });
  }

  // Ends the Clerk session in the browser, clearing its cookies. Without this
  // the server can revoke the session but the unexpired __session token keeps
  // authenticating, so logout looked like it did nothing.
  function signOut() {
    return load()
      .then(function (clerk) { return clerk ? clerk.signOut() : null; })
      .catch(function () { return null; });
  }

  function withTimeout(promise, ms) {
    return Promise.race([promise, new Promise(function (resolve) { setTimeout(resolve, ms); })]);
  }

  window.addEventListener('load', load);

  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible') renew();
  });

  // Bubble phase, so inline onsubmit handlers (the confirm() prompts) have
  // already run: if one cancelled the submit, defaultPrevented is set and
  // the form is left alone.
  document.addEventListener('submit', function (event) {
    var form = event.target;
    if (event.defaultPrevented) return;
    if ((form.getAttribute('method') || 'get').toLowerCase() !== 'post') return;

    event.preventDefault();
    if (form.dataset.sessionPending) return;   // a second click while renewing
    form.dataset.sessionPending = '1';

    // The logout form is the one POST that must not be renewed first: that
    // would mint a fresh token for the session we are about to end, and the
    // stale cookie is exactly what kept the user signed in. Sign out of Clerk
    // first, then let the server clear our own session.
    if (form.hasAttribute('data-logout')) {
      withTimeout(signOut(), RENEW_TIMEOUT_MS).then(function () {
        HTMLFormElement.prototype.submit.call(form);
      });
      return;
    }

    // form.submit() sends without the clicked button's name and value, which
    // some forms depend on (Approve / Deny), so carry them over.
    var submitter = event.submitter;
    if (submitter && submitter.name) {
      var carried = document.createElement('input');
      carried.type = 'hidden';
      carried.name = submitter.name;
      carried.value = submitter.value;
      form.appendChild(carried);
    }

    withTimeout(renew(), RENEW_TIMEOUT_MS).then(function () {
      // HTMLFormElement.prototype.submit fires no submit event, so this
      // handler and the confirm() prompts do not run a second time.
      HTMLFormElement.prototype.submit.call(form);
    });
  });
})();
