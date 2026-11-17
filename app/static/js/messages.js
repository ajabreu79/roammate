// FR-MESS-009: new messages appear on the board without a reload.
//
// The board is rendered server-side. This script then polls
// main.poll_messages every few seconds for anything newer than the last
// message id it has, and appends what comes back. A hidden tab skips its
// polls and catches up as soon as it is shown again.
//
// FR-MESS-004 is re-checked on every poll, so a 403 means the viewer left
// or was removed (MemberLeft). Polling stops there rather than retrying. A
// 401 means the Clerk token lapsed; session.js renews it, so the next poll
// goes through.
//
// Posting goes through fetch, so the sender's own message appears without
// the page navigating. Starting a new topic is still a normal form post,
// because the page has to move to that topic's tab.
//
// Messages are built with the DOM rather than innerHTML, so a message body
// can never be read as markup (NFR-SECU: user text is data, not HTML).
(function () {
  'use strict';

  var list = document.getElementById('messages');
  if (!list || !list.dataset.pollUrl) return;

  var POLL_MS = 3000;
  var lastId = Number(list.dataset.lastId) || 0;
  var seen = new Set();
  list.querySelectorAll('li[data-id]').forEach(function (item) {
    seen.add(Number(item.dataset.id));
  });
  var timer = null;
  var inFlight = false;

  function render(message) {
    if (seen.has(message.id)) return;
    seen.add(message.id);

    var empty = list.querySelector('[data-empty]');
    if (empty) empty.remove();

    var item = document.createElement('li');
    item.dataset.id = message.id;

    var meta = document.createElement('div');
    meta.className = 'meta';
    meta.textContent = message.author_name + ' \u00b7 ' + message.created_at;

    var body = document.createElement('div');
    body.textContent = message.body;

    item.append(meta, body);
    list.appendChild(item);

    lastId = Math.max(lastId, message.id);
  }

  function poll() {
    if (inFlight || document.hidden) return;
    inFlight = true;

    fetch(list.dataset.pollUrl + '?after=' + lastId, {
      credentials: 'same-origin',
      headers: { Accept: 'application/json' }
    })
      .then(function (response) {
        if (response.status === 403 || response.status === 404) {
          stop('You no longer have access to this board.');
          return;
        }
        if (!response.ok) return; // 401 or a server error: try again next tick
        return response.json().then(function (body) {
          body.messages.forEach(render);
        });
      })
      .catch(function () {
        // Network error: also just wait for the next tick.
      })
      .then(function () {
        inFlight = false;
      });
  }

  function stop(text) {
    clearInterval(timer);
    var note = document.createElement('li');
    note.className = 'muted';
    note.textContent = text;
    list.appendChild(note);
  }

  timer = setInterval(poll, POLL_MS);
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) poll();
  });

  var form = document.getElementById('message-form');
  var textarea = document.getElementById('message-content');
  var newTopic = document.getElementById('new_forum_topic');
  var error = document.getElementById('message-error');
  var button = form.querySelector('button[type="submit"]');

  function showError(text) {
    error.textContent = text;
    error.hidden = false;
  }

  form.addEventListener('submit', function (event) {
    // A new topic goes through the normal post, which lands on its tab.
    if (newTopic.value.trim()) return;

    event.preventDefault();
    error.hidden = true;
    button.disabled = true;

    fetch(form.action, {
      method: 'POST',
      body: new FormData(form),
      credentials: 'same-origin',
      headers: { Accept: 'application/json' }
    })
      .then(function (response) {
        if (response.redirected) throw new Error('redirected');
        return response.json().then(function (body) {
          if (response.status !== 201) {
            showError(body.error || 'Could not send your message.');
            return;
          }
          render(body);
          textarea.value = '';
          textarea.focus();
        });
      })
      .catch(function () {
        showError('Could not send your message. Please try again.');
      })
      .then(function () {
        button.disabled = false;
      });
  });

})();
