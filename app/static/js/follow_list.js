// FR-PROF-005: the follower and following lists, shown in a modal on the
// profile page.
//
// The counts are rendered server-side; the names behind them are not, because
// a profile may have thousands and the page should not carry them until asked.
// Clicking a count fetches main.user_followers / main.user_following and fills
// one shared modal.
//
// A private account answers 403 for a viewer it has not accepted
// (`{"private": true}`), which is a real answer rather than a failure, so it
// gets its own message instead of the error one.
(function () {
  'use strict';

  var overlay = document.getElementById('follow-list-modal');
  if (!overlay) return;

  var title = document.getElementById('follow-list-title');
  var list = document.getElementById('follow-list');
  var message = document.getElementById('follow-list-message');
  var opener = null;

  function say(text) {
    message.textContent = text;
    message.hidden = false;
  }

  function clear() {
    list.replaceChildren();
    message.hidden = true;
  }

  function open(button) {
    opener = button;
    title.textContent = button.dataset.title;
    clear();
    say('Loading…');
    overlay.hidden = false;
    document.querySelector('[data-close-modal]').focus();

    // same-origin: the session cookie has to ride along.
    fetch(button.dataset.url, { credentials: 'same-origin' })
      .then(function (response) {
        return response.json().then(function (body) {
          return { ok: response.ok, body: body };
        });
      })
      .then(function (result) {
        message.hidden = true;
        if (result.body && result.body.private) {
          say('This account is private.');
          return;
        }
        if (!result.ok) {
          say('Could not load the list.');
          return;
        }
        var users = result.body.users || [];
        if (users.length === 0) {
          say('No one yet.');
          return;
        }
        users.forEach(function (user) {
          list.appendChild(row(user));
        });
      })
      .catch(function () {
        say('Could not load the list.');
      });
  }

  // Built with the DOM rather than innerHTML so a name can never be read as
  // markup (NFR-SECU: user text is data, not HTML).
  function row(user) {
    var item = document.createElement('li');
    var link = document.createElement('a');
    link.href = user.profile_url;
    link.className = 'follow-list-item';

    var photo = document.createElement('img');
    photo.src = user.photo_url;
    photo.alt = user.name;
    photo.className = 'follow-list-photo';

    var text = document.createElement('div');
    var name = document.createElement('span');
    name.className = 'follow-list-name';
    name.textContent = user.name;
    text.appendChild(name);
    if (user.school_year) {
      var year = document.createElement('span');
      year.className = 'follow-list-year';
      year.textContent = user.school_year;
      text.appendChild(year);
    }

    link.append(photo, text);
    item.appendChild(link);
    return item;
  }

  function close() {
    overlay.hidden = true;
    clear();
    if (opener) opener.focus();  // keyboard focus goes back where it came from
    opener = null;
  }

  document.querySelectorAll('[data-follow-list]').forEach(function (button) {
    button.addEventListener('click', function () { open(button); });
  });

  overlay.addEventListener('click', function (event) {
    // Clicking the backdrop closes; clicking inside the dialog does not.
    if (event.target === overlay || event.target.closest('[data-close-modal]')) {
      close();
    }
  });

  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape' && !overlay.hidden) close();
  });
})();
