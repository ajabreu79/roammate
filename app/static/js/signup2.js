// Registration step 2: the filterable university dropdown (FR-AUTH-013) and
// the description counter (FR-AUTH-006).
(function () {
  'use strict';

  function debounce(fn, wait) {
    var timer;
    return function () {
      var args = arguments;
      clearTimeout(timer);
      timer = setTimeout(function () { fn.apply(null, args); }, wait);
    };
  }

  function setUpUniversityFilter() {
    var universitySelect = document.getElementById('university');
    var countrySelect = document.getElementById('country');
    if (!universitySelect) return;

    var endpoint = universitySelect.dataset.universityUrl;
    var filterBox = document.createElement('input');
    filterBox.type = 'search';
    filterBox.id = 'university-filter';
    filterBox.placeholder = 'Type to filter universities…';
    filterBox.setAttribute('aria-controls', 'university');
    universitySelect.parentNode.insertBefore(filterBox, universitySelect);

    function reload() {
      var params = new URLSearchParams();
      if (filterBox.value.trim()) params.set('q', filterBox.value.trim());
      if (countrySelect && countrySelect.value) params.set('country', countrySelect.value);

      fetch(endpoint + '?' + params.toString(), { credentials: 'same-origin' })
        .then(function (response) { return response.json(); })
        .then(function (rows) {
          // Keep the current choice selectable even if it falls outside the
          // new filter, so a stray keystroke cannot silently clear it.
          var chosen = universitySelect.value;
          universitySelect.innerHTML = '<option value="">Select…</option>';

          var names = rows.map(function (row) { return row.name; });
          if (chosen && names.indexOf(chosen) === -1) names.unshift(chosen);

          names.forEach(function (name) {
            var option = document.createElement('option');
            option.value = name;
            option.textContent = name;
            if (name === chosen) option.selected = true;
            universitySelect.appendChild(option);
          });
        })
        .catch(function () { /* leave the server-rendered list in place */ });
    }

    filterBox.addEventListener('input', debounce(reload, 200));
    if (countrySelect) countrySelect.addEventListener('change', reload);
  }

  function setUpDescriptionCounter() {
    var description = document.getElementById('description');
    var counter = document.getElementById('desc-count');
    if (!description || !counter) return;

    function update() { counter.textContent = description.value.length; }
    description.addEventListener('input', update);
    update();
  }

  function setUpFileLabel() {
    var input = document.getElementById('profile_pic');
    if (!input) return;
    var text = document.querySelector('.profile-pic-button .file-text');
    var clear = document.querySelector('.profile-pic-button .file-clear');
    if (!text) return;

    input.addEventListener('change', function () {
      var chosen = input.files && input.files[0];
      text.textContent = chosen ? chosen.name : 'Choose a file';
      if (clear) clear.style.display = chosen ? 'inline' : 'none';
    });

    if (clear) {
      clear.addEventListener('click', function (event) {
        event.preventDefault();
        input.value = '';
        text.textContent = 'Choose a file';
        clear.style.display = 'none';
      });
    }
  }

  window.addEventListener('load', function () {
    setUpUniversityFilter();
    setUpDescriptionCounter();
    setUpFileLabel();
  });
})();
