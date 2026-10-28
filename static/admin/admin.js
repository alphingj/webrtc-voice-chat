'use strict';

/* Admin panel: vanilla JS over the JSON API on this port only. */

(() => {
  const $ = (id) => document.getElementById(id);

  const S = {
    me: null,
    csrf: null,
    server: null,
    stats: null,
    tab: 'users',
    users: [],
    rooms: [],
    auditOffset: 0,
    auditTotal: 0,
    liveTimer: null,
    modalCloseResolver: null,
    toastTimer: null,
  };

  // ------------------------------------------------------------------ helpers

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function button(label, className, onClick) {
    const node = el('button', className, label);
    node.type = 'button';
    if (onClick) node.addEventListener('click', onClick);
    return node;
  }

  function pill(text, kind) {
    return el('span', 'pill' + (kind ? ' ' + kind : ''), text);
  }

  function fmtTime(ts) {
    if (!ts) return '—';
    return new Date(Number(ts) * 1000).toLocaleString();
  }

  function fmtDuration(seconds) {
    const total = Math.max(0, Math.floor(Number(seconds) || 0));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const secs = total % 60;
    if (hours > 0) return hours + 'h ' + minutes + 'm';
    if (minutes > 0) return minutes + 'm ' + secs + 's';
    return secs + 's';
  }

  function setError(id, text) {
    $(id).textContent = text || '';
  }

  function toast(text) {
    const node = $('toast');
    node.textContent = text;
    node.hidden = false;
    if (S.toastTimer) clearTimeout(S.toastTimer);
    S.toastTimer = setTimeout(() => {
      node.hidden = true;
      S.toastTimer = null;
    }, 2800);
  }

  async function api(path, options) {
    const opts = Object.assign({ method: 'GET', credentials: 'same-origin' }, options || {});
    const headers = Object.assign({}, opts.headers || {});
    if (opts.body !== undefined) {
      headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(opts.body);
    }
    if (S.csrf && opts.method !== 'GET') headers['X-CSRF-Token'] = S.csrf;
    opts.headers = headers;
    const response = await fetch(path, opts);
    let data = null;
    try {
      data = await response.json();
    } catch (err) {
      data = null;
    }
    if (!response.ok) {
      const error = new Error((data && data.message) || 'Request failed (' + response.status + ')');
      error.status = response.status;
      error.code = data && data.error;
      if (response.status === 401 && path !== '/api/login') toLogin();
      throw error;
    }
    return data || {};
  }

  // -------------------------------------------------------------------- modal

  function openModal(title, build) {
    $('modal-title').textContent = title;
    const body = $('modal-body');
    body.textContent = '';
    build(body);
    $('modal').hidden = false;
    const focusable = body.querySelector('input, select, textarea, button');
    if (focusable) setTimeout(() => focusable.focus(), 40);
  }

  function closeModal() {
    $('modal').hidden = true;
    const resolver = S.modalCloseResolver;
    S.modalCloseResolver = null;
    if (resolver) resolver();
  }

  function confirmDialog(message, label) {
    return new Promise((resolve) => {
      let settled = false;
      const finish = (value) => {
        if (settled) return;
        settled = true;
        S.modalCloseResolver = null;
        closeModal();
        resolve(value);
      };
      S.modalCloseResolver = () => {
        if (settled) return;
        settled = true;
        resolve(false);
      };
      openModal('Confirm', (body) => {
        body.appendChild(el('p', 'hint', message));
        const row = el('div', 'row');
        row.style.marginTop = '14px';
        row.style.justifyContent = 'flex-end';
        row.appendChild(button('Cancel', 'btn', () => finish(false)));
        row.appendChild(button(label || 'Confirm', 'btn danger', () => finish(true)));
        body.appendChild(row);
      });
    });
  }

  function field(labelText, input) {
    const label = el('label', null, labelText);
    label.appendChild(input);
    return label;
  }

  function formError() {
    return el('p', 'error');
  }

  // --------------------------------------------------------------------- auth

  async function boot() {
    bindEvents();
    try {
      const me = await api('/api/me');
      onAuthed(me);
    } catch (err) {
      $('view-login').hidden = false;
      $('view-app').hidden = true;
    }
  }

  function onAuthed(me) {
    S.me = me.user;
    S.csrf = me.csrf;
    S.server = me.server;
    S.stats = me.stats;
    $('admin-who').textContent = me.user.username;
    $('view-login').hidden = true;
    $('view-app').hidden = false;
    renderServerInfo();
    selectTab('users');
  }

  function toLogin() {
    stopLive();
    closeModal();
    S.me = null;
    S.csrf = null;
    $('view-app').hidden = true;
    $('view-login').hidden = false;
    $('admin-password').value = '';
  }

  async function doLogin(event) {
    event.preventDefault();
    setError('admin-login-error', '');
    const submit = $('admin-login-submit');
    submit.disabled = true;
    try {
      await api('/api/login', {
        method: 'POST',
        body: {
          username: $('admin-username').value.trim(),
          password: $('admin-password').value,
        },
      });
      $('admin-password').value = '';
      const me = await api('/api/me');
      onAuthed(me);
    } catch (err) {
      setError('admin-login-error', err.message);
    } finally {
      submit.disabled = false;
    }
  }

  async function doLogout() {
    try {
      await api('/api/logout', { method: 'POST', body: {} });
    } catch (err) {
      /* the session is discarded client-side either way */
    }
    toLogin();
  }

  // --------------------------------------------------------------------- tabs

  function selectTab(name) {
    S.tab = name;
    for (const tab of document.querySelectorAll('.tab')) {
      tab.setAttribute('aria-selected', tab.dataset.tab === name ? 'true' : 'false');
    }
    for (const panel of document.querySelectorAll('.panel')) {
      panel.hidden = panel.id !== 'tab-' + name;
    }
    setError('global-error', '');
    stopLive();
    if (name === 'users') {
      loadUsers();
      loadInvites();
    } else if (name === 'rooms') {
      loadRooms();
    } else if (name === 'live') {
      loadLive();
      startLive();
    } else if (name === 'audit') {
      loadAudit(true);
    } else if (name === 'settings') {
      loadSettings();
    }
  }

  // -------------------------------------------------------------------- users

  async function loadUsers() {
    setError('users-error', '');
    try {
      const query = $('user-search').value.trim();
      const data = await api('/api/users' + (query ? '?q=' + encodeURIComponent(query) : ''));
      S.users = data.users || [];
      renderUsers();
    } catch (err) {
      setError('users-error', err.message);
    }
  }

  function renderUsers() {
    const body = $('users-body');
    body.textContent = '';
    if (S.users.length === 0) {
      const row = el('tr');
      const cell = el('td', 'hint', 'No users match.');
      cell.colSpan = 6;
      row.appendChild(cell);
      body.appendChild(row);
      return;
    }
    for (const user of S.users) {
      const row = el('tr');
      row.appendChild(el('td', null, user.username));
      row.appendChild(el('td')).appendChild(pill(user.role, user.role === 'admin' ? 'ok' : ''));
      row.appendChild(el('td')).appendChild(pill(user.disabled ? 'disabled' : 'active', user.disabled ? 'bad' : 'ok'));
      row.appendChild(el('td', 'mono', fmtTime(user.last_login_at)));
      row.appendChild(el('td', 'mono', user.session_count));

      const actions = el('div', 'actions');
      actions.appendChild(button('Edit', 'btn small', () => editUser(user)));
      actions.appendChild(button('Password', 'btn small', () => passwordUser(user)));
      actions.appendChild(
        button(user.disabled ? 'Enable' : 'Disable', 'btn small' + (user.disabled ? '' : ' warn'), () =>
          toggleUser(user)
        )
      );
      actions.appendChild(button('Delete', 'btn small danger', () => deleteUser(user)));
      const cell = el('td');
      cell.appendChild(actions);
      row.appendChild(cell);
      body.appendChild(row);
    }
  }

  async function createUser(event) {
    event.preventDefault();
    setError('users-error', '');
    try {
      await api('/api/users', {
        method: 'POST',
        body: {
          username: $('user-new-name').value.trim(),
          password: $('user-new-password').value,
          role: $('user-new-role').value,
        },
      });
      $('user-new-name').value = '';
      $('user-new-password').value = '';
      toast('User created.');
      loadUsers();
    } catch (err) {
      setError('users-error', err.message);
    }
  }

  function editUser(user) {
    openModal('Edit ' + user.username, (body) => {
      const form = el('form', 'modal-body-form');
      const name = el('input');
      name.type = 'text';
      name.value = user.username;
      name.maxLength = 32;
      const role = el('select');
      for (const value of ['user', 'admin']) {
        const option = el('option', null, value === 'admin' ? 'Admin' : 'User');
        option.value = value;
        role.appendChild(option);
      }
      role.value = user.role;
      const disabled = el('input');
      disabled.type = 'checkbox';
      disabled.checked = Boolean(user.disabled);
      const disabledWrap = el('label', 'check', 'Disabled (cannot sign in)');
      disabledWrap.prepend(disabled);

      const error = formError();
      const actions = el('div', 'row');
      actions.appendChild(button('Cancel', 'btn', closeModal));
      const save = el('button', 'btn primary', 'Save');
      save.type = 'submit';
      actions.appendChild(save);

      form.appendChild(field('Username', name));
      form.appendChild(field('Role', role));
      form.appendChild(disabledWrap);
      form.appendChild(error);
      form.appendChild(actions);

      form.addEventListener('submit', async (event) => {
        event.preventDefault();
        save.disabled = true;
        error.textContent = '';
        try {
          await api('/api/users/' + user.id, {
            method: 'PATCH',
            body: { username: name.value.trim(), role: role.value, disabled: disabled.checked },
          });
          closeModal();
          toast('User updated.');
          loadUsers();
        } catch (err) {
          error.textContent = err.message;
        } finally {
          save.disabled = false;
        }
      });
      body.appendChild(form);
    });
  }

  function passwordUser(user) {
    openModal('Set password for ' + user.username, (body) => {
      const form = el('form', 'modal-body-form');
      const password = el('input');
      password.type = 'password';
      password.autocomplete = 'new-password';
      password.maxLength = 200;
      const error = formError();
      const actions = el('div', 'row');
      actions.appendChild(button('Cancel', 'btn', closeModal));
      const save = el('button', 'btn primary', 'Set password');
      save.type = 'submit';
      actions.appendChild(save);
      form.appendChild(field('New password (8+ characters)', password));
      form.appendChild(el('p', 'hint', 'All of this user’s sessions are revoked.'));
      form.appendChild(error);
      form.appendChild(actions);
      form.addEventListener('submit', async (event) => {
        event.preventDefault();
        save.disabled = true;
        error.textContent = '';
        try {
          await api('/api/users/' + user.id + '/password', {
            method: 'POST',
            body: { password: password.value },
          });
          closeModal();
          toast('Password updated.');
          loadUsers();
        } catch (err) {
          error.textContent = err.message;
        } finally {
          save.disabled = false;
        }
      });
      body.appendChild(form);
    });
  }

  async function toggleUser(user) {
    try {
      await api('/api/users/' + user.id, {
        method: 'PATCH',
        body: { disabled: !user.disabled },
      });
      toast(user.disabled ? 'User enabled.' : 'User disabled.');
      loadUsers();
    } catch (err) {
      setError('users-error', err.message);
    }
  }

  async function deleteUser(user) {
    const ok = await confirmDialog(
      'Delete ' + user.username + '? Their sessions end immediately. This cannot be undone.',
      'Delete user'
    );
    if (!ok) return;
    try {
      await api('/api/users/' + user.id, { method: 'DELETE' });
      toast('User deleted.');
      loadUsers();
    } catch (err) {
      setError('users-error', err.message);
    }
  }

  // ------------------------------------------------------------------ invites

  async function loadInvites() {
    try {
      const data = await api('/api/invites');
      renderInvites(data.invites || []);
    } catch (err) {
      setError('users-error', err.message);
    }
  }

  function renderInvites(invites) {
    const list = $('invite-list');
    list.textContent = '';
    if (invites.length === 0) {
      list.appendChild(el('p', 'hint', 'No invites yet.'));
      return;
    }
    for (const invite of invites.slice(0, 8)) {
      const row = el('div', 'invite-row');
      const info = el('div', 'grow');
      const title = el('div', 'mono', invite.token_hint + '…');
      info.appendChild(title);
      const state = invite.used_at
        ? 'used ' + fmtTime(invite.used_at) + (invite.used_by_name ? ' by ' + invite.used_by_name : '')
        : invite.expired
          ? 'expired ' + fmtTime(invite.expires_at)
          : 'expires ' + fmtTime(invite.expires_at);
      info.appendChild(el('div', 'hint', state));
      row.appendChild(info);
      if (!invite.used_at && !invite.expired) {
        row.appendChild(
          button('Revoke', 'btn small danger', async () => {
            const ok = await confirmDialog('Revoke this invite link?', 'Revoke');
            if (!ok) return;
            try {
              await api('/api/invites/' + invite.id, { method: 'DELETE' });
              toast('Invite revoked.');
              loadInvites();
            } catch (err) {
              setError('users-error', err.message);
            }
          })
        );
      }
      list.appendChild(row);
    }
  }

  async function createInvite() {
    try {
      const data = await api('/api/invites', {
        method: 'POST',
        body: { expires_hours: Number($('invite-hours').value) },
      });
      loadInvites();
      openModal('Invite link created', (body) => {
        body.appendChild(el('p', 'hint', 'Send this link to the new user. It works once and is not stored in readable form.'));
        const box = el('div', 'link-box');
        const input = el('input');
        input.type = 'text';
        input.readOnly = true;
        input.value = data.url;
        box.appendChild(input);
        box.appendChild(
          button('Copy', 'btn primary', async () => {
            try {
              await navigator.clipboard.writeText(data.url);
              toast('Copied.');
            } catch (err) {
              input.select();
              document.execCommand('copy');
              toast('Copied.');
            }
          })
        );
        body.appendChild(box);
        const actions = el('div', 'row');
        actions.style.justifyContent = 'flex-end';
        actions.style.marginTop = '12px';
        actions.appendChild(button('Done', 'btn', closeModal));
        body.appendChild(actions);
      });
    } catch (err) {
      setError('users-error', err.message);
    }
  }

  // -------------------------------------------------------------------- rooms

  async function loadRooms() {
    setError('rooms-error', '');
    try {
      const data = await api('/api/rooms');
      S.rooms = data.rooms || [];
      renderRooms();
    } catch (err) {
      setError('rooms-error', err.message);
    }
  }

  function renderRooms() {
    const list = $('rooms-list');
    list.textContent = '';
    if (S.rooms.length === 0) {
      list.appendChild(el('p', 'hint', 'No rooms yet.'));
      return;
    }
    for (const room of S.rooms) {
      const row = el('div', 'room-row');
      const info = el('div');
      const title = el('h3', null, room.name + ' ');
      title.appendChild(pill(room.closed ? 'closed' : 'open', room.closed ? 'bad' : 'ok'));
      info.appendChild(title);
      const bits = [
        room.occupants + '/' + room.max_participants + ' connected',
        room.access_mode === 'all' ? 'all users' : room.access_user_ids.length + ' selected user(s)',
        room.has_password ? 'password protected' : 'no password',
      ];
      info.appendChild(el('div', 'meta', bits.join(' · ')));
      row.appendChild(info);

      const actions = el('div', 'actions');
      actions.appendChild(button('Edit', 'btn small', () => editRoom(room)));
      actions.appendChild(button('Access', 'btn small', () => roomAccess(room)));
      actions.appendChild(
        button(room.closed ? 'Open' : 'Close', 'btn small' + (room.closed ? '' : ' warn'), () =>
          toggleRoomClosed(room)
        )
      );
      actions.appendChild(button('Delete', 'btn small danger', () => deleteRoom(room)));
      row.appendChild(actions);
      list.appendChild(row);
    }
  }

  async function createRoom(event) {
    event.preventDefault();
    setError('rooms-error', '');
    try {
      await api('/api/rooms', {
        method: 'POST',
        body: {
          name: $('room-new-name').value.trim(),
          max_participants: Number($('room-new-max').value),
          password: $('room-new-password').value || null,
          access_mode: $('room-new-access').value,
        },
      });
      $('room-new-name').value = '';
      $('room-new-password').value = '';
      toast('Room created.');
      loadRooms();
    } catch (err) {
      setError('rooms-error', err.message);
    }
  }

  function editRoom(room) {
    openModal('Edit ' + room.name, (body) => {
      const form = el('form', 'modal-body-form');
      const name = el('input');
      name.type = 'text';
      name.value = room.name;
      name.maxLength = 48;
      const max = el('input');
      max.type = 'number';
      max.min = '2';
      max.max = '12';
      max.value = String(room.max_participants);
      const password = el('input');
      password.type = 'password';
      password.placeholder = room.has_password ? 'Leave blank to keep current' : 'No password';
      password.autocomplete = 'new-password';
      const clearPassword = el('input');
      clearPassword.type = 'checkbox';
      const clearWrap = el('label', 'check', 'Remove room password');
      clearWrap.prepend(clearPassword);
      const closed = el('input');
      closed.type = 'checkbox';
      closed.checked = Boolean(room.closed);
      const closedWrap = el('label', 'check', 'Closed (no new joins)');
      closedWrap.prepend(closed);

      const error = formError();
      const actions = el('div', 'row');
      actions.appendChild(button('Cancel', 'btn', closeModal));
      const save = el('button', 'btn primary', 'Save');
      save.type = 'submit';
      actions.appendChild(save);

      form.appendChild(field('Name', name));
      form.appendChild(field('Max participants', max));
      form.appendChild(field('New password', password));
      form.appendChild(clearWrap);
      form.appendChild(closedWrap);
      form.appendChild(error);
      form.appendChild(actions);

      form.addEventListener('submit', async (event) => {
        event.preventDefault();
        save.disabled = true;
        error.textContent = '';
        const payload = {
          name: name.value.trim(),
          max_participants: Number(max.value),
          closed: closed.checked,
        };
        if (password.value) payload.password = password.value;
        if (clearPassword.checked) payload.clear_password = true;
        try {
          await api('/api/rooms/' + room.id, { method: 'PATCH', body: payload });
          closeModal();
          toast('Room updated.');
          loadRooms();
        } catch (err) {
          error.textContent = err.message;
        } finally {
          save.disabled = false;
        }
      });
      body.appendChild(form);
    });
  }

  async function roomAccess(room) {
    let users;
    try {
      const data = await api('/api/users');
      users = data.users || [];
    } catch (err) {
      setError('rooms-error', err.message);
      return;
    }
    openModal('Access to ' + room.name, (body) => {
      const form = el('form', 'modal-body-form');
      const mode = el('select');
      for (const value of ['all', 'selected']) {
        const option = el('option', null, value === 'all' ? 'All users' : 'Selected users only');
        option.value = value;
        mode.appendChild(option);
      }
      mode.value = room.access_mode;
      const list = el('div', 'check-list');
      const boxes = [];
      for (const user of users) {
        const box = el('input');
        box.type = 'checkbox';
        box.value = String(user.id);
        box.checked = room.access_user_ids.includes(user.id);
        const label = el('label', null, user.username + (user.disabled ? ' (disabled)' : ''));
        label.prepend(box);
        list.appendChild(label);
        boxes.push(box);
      }
      if (users.length === 0) list.appendChild(el('p', 'hint', 'No users yet.'));

      const error = formError();
      const actions = el('div', 'row');
      actions.appendChild(button('Cancel', 'btn', closeModal));
      const save = el('button', 'btn primary', 'Save access');
      save.type = 'submit';
      actions.appendChild(save);

      form.appendChild(field('Who may join', mode));
      form.appendChild(list);
      form.appendChild(error);
      form.appendChild(actions);
      form.addEventListener('submit', async (event) => {
        event.preventDefault();
        save.disabled = true;
        error.textContent = '';
        try {
          await api('/api/rooms/' + room.id + '/access', {
            method: 'PUT',
            body: {
              mode: mode.value,
              user_ids: boxes.filter((box) => box.checked).map((box) => Number(box.value)),
            },
          });
          closeModal();
          toast('Access updated.');
          loadRooms();
        } catch (err) {
          error.textContent = err.message;
        } finally {
          save.disabled = false;
        }
      });
      body.appendChild(form);
    });
  }

  async function toggleRoomClosed(room) {
    const closing = !room.closed;
    if (closing) {
      const ok = await confirmDialog(
        'Close ' + room.name + '? Everyone in it is disconnected and no one can rejoin until you reopen it.',
        'Close room'
      );
      if (!ok) return;
    }
    try {
      await api('/api/rooms/' + room.id + (closing ? '/close' : '/open'), { method: 'POST' });
      toast(closing ? 'Room closed.' : 'Room opened.');
      loadRooms();
    } catch (err) {
      setError('rooms-error', err.message);
    }
  }

  async function deleteRoom(room) {
    const ok = await confirmDialog('Delete ' + room.name + '? Everyone in it is disconnected.', 'Delete room');
    if (!ok) return;
    try {
      await api('/api/rooms/' + room.id, { method: 'DELETE' });
      toast('Room deleted.');
      loadRooms();
    } catch (err) {
      setError('rooms-error', err.message);
    }
  }

  // --------------------------------------------------------------------- live

  async function loadLive() {
    setError('live-error', '');
    try {
      const data = await api('/api/live');
      renderLive(data);
    } catch (err) {
      setError('live-error', err.message);
    }
  }

  function renderLive(data) {
    $('live-meta').textContent =
      (data.online || 0) + ' peer(s) online · updated ' + new Date().toLocaleTimeString();
    const content = $('live-content');
    content.textContent = '';
    const rooms = data.rooms || [];
    if (rooms.length === 0) {
      content.appendChild(el('p', 'hint', 'No rooms configured.'));
      return;
    }
    for (const room of rooms) {
      const card = el('div', 'live-room');
      const head = el('div', 'live-room-head');
      const title = el('h3', null, room.name + ' ');
      title.appendChild(pill(room.count + '/' + room.max_participants, room.count ? 'ok' : ''));
      head.appendChild(title);
      head.appendChild(
        button('Close room', 'btn small danger', async () => {
          const ok = await confirmDialog(
            'Close ' + room.name + ' and disconnect ' + room.count + ' peer(s)?',
            'Close room'
          );
          if (!ok) return;
          try {
            await api('/api/rooms/' + room.id + '/close', { method: 'POST' });
            toast('Room closed.');
            loadLive();
            loadRooms();
          } catch (err) {
            setError('live-error', err.message);
          }
        })
      );
      card.appendChild(head);

      if (!room.participants || room.participants.length === 0) {
        card.appendChild(el('p', 'live-empty', room.closed ? 'Closed, nobody inside.' : 'Empty.'));
      } else {
        for (const peer of room.participants) {
          const row = el('div', 'live-peer');
          const info = el('div');
          const nameLine = el('div', null, peer.username + ' ');
          if (peer.muted) nameLine.appendChild(pill('muted', 'warn'));
          info.appendChild(nameLine);
          info.appendChild(
            el('div', 'sub', fmtDuration(peer.duration) + ' · ' + peer.ip + ' · ' + peer.peer_id.slice(0, 6))
          );
          row.appendChild(info);

          const actions = el('div', 'actions');
          actions.appendChild(
            button('Mute', 'btn small warn', async () => {
              try {
                await api('/api/live/' + peer.peer_id + '/mute', { method: 'POST' });
                toast('Microphone muted.');
                loadLive();
              } catch (err) {
                setError('live-error', err.message);
              }
            })
          );
          actions.appendChild(
            button('Kick', 'btn small danger', async () => {
              const ok = await confirmDialog('Kick ' + peer.username + ' from ' + room.name + '?', 'Kick');
              if (!ok) return;
              try {
                await api('/api/live/' + peer.peer_id + '/kick', { method: 'POST' });
                toast('Peer kicked.');
                loadLive();
              } catch (err) {
                setError('live-error', err.message);
              }
            })
          );
          const actionCell = el('div');
          actionCell.appendChild(actions);
          row.appendChild(actionCell);
          card.appendChild(row);
        }
      }
      content.appendChild(card);
    }
  }

  function startLive() {
    stopLive();
    if (!$('live-auto').checked) return;
    S.liveTimer = setInterval(() => {
      if (document.visibilityState === 'visible' && S.tab === 'live') loadLive();
    }, 5000);
  }

  function stopLive() {
    if (S.liveTimer) clearInterval(S.liveTimer);
    S.liveTimer = null;
  }

  // -------------------------------------------------------------------- audit

  async function loadAudit(reset) {
    setError('audit-error', '');
    if (reset) {
      S.auditOffset = 0;
      $('audit-body').textContent = '';
    }
    try {
      const params = new URLSearchParams();
      params.set('limit', '50');
      params.set('offset', String(S.auditOffset));
      const action = $('audit-action').value;
      const query = $('audit-q').value.trim();
      if (action) params.set('action', action);
      if (query) params.set('q', query);
      const data = await api('/api/audit?' + params.toString());
      renderAudit(data, reset);
    } catch (err) {
      setError('audit-error', err.message);
    }
  }

  function renderAudit(data, reset) {
    const body = $('audit-body');
    if (reset) body.textContent = '';
    const entries = data.entries || [];
    for (const entry of entries) {
      const row = el('tr');
      row.appendChild(el('td', 'mono', fmtTime(entry.ts)));
      row.appendChild(el('td', null, entry.actor_name || '—'));
      row.appendChild(el('td', 'mono', entry.action));
      row.appendChild(el('td', null, entry.target || '—'));
      row.appendChild(el('td', 'mono', entry.detail || '—'));
      row.appendChild(el('td', 'mono', entry.ip || '—'));
      body.appendChild(row);
    }
    if (entries.length === 0 && reset) {
      const row = el('tr');
      const cell = el('td', 'hint', 'No entries match.');
      cell.colSpan = 6;
      row.appendChild(cell);
      body.appendChild(row);
    }
    S.auditTotal = data.total || 0;
    S.auditOffset += entries.length;
    $('audit-meta').textContent = 'Showing ' + body.childElementCount + ' of ' + S.auditTotal + ' entries.';
    $('audit-more').hidden = S.auditOffset >= S.auditTotal;

    const select = $('audit-action');
    if (select.options.length <= 1 && Array.isArray(data.actions)) {
      for (const action of data.actions) {
        const option = el('option', null, action);
        option.value = action;
        select.appendChild(option);
      }
      select.value = new URLSearchParams(location.search).get('action') || '';
    }
  }

  // ----------------------------------------------------------------- settings

  async function loadSettings() {
    setError('settings-error', '');
    try {
      const data = await api('/api/me');
      S.server = data.server;
      S.stats = data.stats;
      renderServerInfo();
      const settings = data.settings || {};
      $('set-max-users').value = settings.max_users;
      $('set-max-rooms').value = settings.max_rooms;
      $('set-max-participants').value = settings.max_participants_per_room;
      $('set-session-hours').value = settings.session_lifetime_hours;
      $('set-admin-session-hours').value = settings.admin_session_lifetime_hours;
      $('set-audit-days').value = settings.audit_retention_days;
      $('set-login-attempts').value = settings.login_max_attempts;
      $('set-lockout').value = settings.login_lockout_seconds;
      $('set-chat-length').value = settings.chat_max_length;
      $('set-ice-servers').value = JSON.stringify(settings.ice_servers || [], null, 2);
      $('set-origins').value = (settings.allowed_origins || []).join('\n');
    } catch (err) {
      setError('settings-error', err.message);
    }
  }

  function renderServerInfo() {
    const info = $('server-info');
    info.textContent = '';
    if (!S.server) return;
    const rows = [
      ['Voice app', S.server.voice_url],
      ['Admin panel', S.server.admin_url],
      ['Bind IP', S.server.bind_ip],
      ['Ports', S.server.voice_port + ' (voice), ' + S.server.admin_port + ' (admin)'],
      ['Database', S.server.db_path],
    ];
    if (S.stats) {
      rows.push(['Users', S.stats.users + ' (' + S.stats.disabled_users + ' disabled)']);
      rows.push(['Rooms', String(S.stats.rooms)]);
      rows.push(['Online peers', String(S.stats.online)]);
    }
    for (const [key, value] of rows) {
      info.appendChild(el('dt', null, key));
      info.appendChild(el('dd', 'mono', value));
    }
  }

  async function saveSettings(event) {
    event.preventDefault();
    setError('settings-error', '');
    let ice;
    try {
      ice = JSON.parse($('set-ice-servers').value || '[]');
    } catch (err) {
      setError('settings-error', 'ICE servers must be valid JSON.');
      return;
    }
    const origins = $('set-origins')
      .value.split('\n')
      .map((line) => line.trim())
      .filter(Boolean);
    const payload = {
      max_users: Number($('set-max-users').value),
      max_rooms: Number($('set-max-rooms').value),
      max_participants_per_room: Number($('set-max-participants').value),
      session_lifetime_hours: Number($('set-session-hours').value),
      admin_session_lifetime_hours: Number($('set-admin-session-hours').value),
      audit_retention_days: Number($('set-audit-days').value),
      login_max_attempts: Number($('set-login-attempts').value),
      login_lockout_seconds: Number($('set-lockout').value),
      chat_max_length: Number($('set-chat-length').value),
      ice_servers: ice,
      allowed_origins: origins,
    };
    try {
      await api('/api/settings', { method: 'PUT', body: payload });
      toast('Settings saved.');
      loadSettings();
    } catch (err) {
      setError('settings-error', err.message);
    }
  }

  // ------------------------------------------------------------------- events

  function bindEvents() {
    $('admin-login-form').addEventListener('submit', doLogin);
    $('logout').addEventListener('click', doLogout);
    for (const tab of document.querySelectorAll('.tab')) {
      tab.addEventListener('click', () => selectTab(tab.dataset.tab));
    }
    $('user-create-form').addEventListener('submit', createUser);
    $('user-refresh').addEventListener('click', loadUsers);
    $('user-search').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        loadUsers();
      }
    });
    $('invite-create').addEventListener('click', createInvite);
    $('room-create-form').addEventListener('submit', createRoom);
    $('room-refresh').addEventListener('click', loadRooms);
    $('live-refresh').addEventListener('click', loadLive);
    $('live-auto').addEventListener('change', startLive);
    $('audit-refresh').addEventListener('click', () => loadAudit(true));
    $('audit-action').addEventListener('change', () => loadAudit(true));
    $('audit-q').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        loadAudit(true);
      }
    });
    $('audit-more').addEventListener('click', () => loadAudit(false));
    $('settings-form').addEventListener('submit', saveSettings);

    $('modal-close').addEventListener('click', closeModal);
    $('modal').addEventListener('click', (event) => {
      if (event.target === $('modal')) closeModal();
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && !$('modal').hidden) closeModal();
    });
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible' && S.tab === 'live' && $('live-auto').checked) {
        loadLive();
      }
    });
  }

  document.addEventListener('DOMContentLoaded', boot);
})();
