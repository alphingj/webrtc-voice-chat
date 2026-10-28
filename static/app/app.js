'use strict';

/*
 * Voice client. Media is full-mesh: the newest participant offers to every
 * peer already in the room, so only one side ever creates the initial offer.
 * ICE candidates are buffered until the matching remote description exists.
 */
(() => {
  const $ = (id) => document.getElementById(id);

  const MIC_CONSTRAINTS = {
    audio: {
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
      channelCount: 1,
      sampleRate: 48000,
    },
    video: false,
  };
  const OPUS_FMTP = 'maxaveragebitrate=24000;stereo=0;sprop-stereo=0;useinbandfec=1;usedtx=1';
  const SPEAKING_RMS = 0.04;
  const SPEAKING_HOLD_MS = 280;
  const ICE_RESTART_DELAY_MS = 2000;
  const MAX_RECONNECT_MS = 15000;

  const S = {
    me: null,
    csrf: null,
    iceServers: [],
    limits: { max_participants: 6, chat_max_length: 500 },
    rooms: [],
    room: null,
    joinPassword: '',
    inviteToken: '',
    ws: null,
    conn: 'idle',
    myPeerId: null,
    roster: new Map(),
    peers: new Map(),
    volumes: new Map(),
    localStream: null,
    audioCtx: null,
    analysers: new Map(),
    speakingTimer: null,
    muted: false,
    forceMuted: false,
    ptt: false,
    pttHeld: false,
    leaving: false,
    joining: false,
    reconnectAttempt: 0,
    reconnectTimer: null,
    pingTimer: null,
    wakeLock: null,
    toastTimer: null,
    chatUnread: 0,
  };

  // ------------------------------------------------------------------ utils

  function showView(name) {
    $('view-login').hidden = name !== 'login';
    $('view-lobby').hidden = name !== 'lobby';
    $('view-room').hidden = name !== 'room';
    if (name !== 'room') {
      closeChat(true);
    }
  }

  function showBanner(text, kind) {
    const banner = $('banner');
    $('banner-text').textContent = text;
    banner.classList.toggle('info', kind === 'info');
    banner.hidden = false;
  }

  function hideBanner() {
    $('banner').hidden = true;
  }

  function toast(text) {
    const node = $('toast');
    node.textContent = text;
    node.hidden = false;
    if (S.toastTimer) clearTimeout(S.toastTimer);
    S.toastTimer = setTimeout(() => {
      node.hidden = true;
      S.toastTimer = null;
    }, 2600);
  }

  function setError(id, text) {
    $(id).textContent = text || '';
  }

  function initial(name) {
    const clean = String(name || '?').trim();
    return clean ? clean.charAt(0).toUpperCase() : '?';
  }

  async function api(path, options) {
    const opts = Object.assign({ method: 'GET', credentials: 'same-origin' }, options || {});
    const headers = Object.assign({}, opts.headers || {});
    if (opts.body !== undefined && typeof opts.body !== 'string') {
      headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(opts.body);
    }
    if (S.csrf && opts.method !== 'GET') {
      headers['X-CSRF-Token'] = S.csrf;
    }
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
      throw error;
    }
    return data || {};
  }

  function send(payload) {
    if (S.ws && S.ws.readyState === WebSocket.OPEN) {
      try {
        S.ws.send(JSON.stringify(payload));
      } catch (err) {
        /* socket closed between the check and the send */
      }
    }
  }

  function setConn(state) {
    S.conn = state;
    const badge = $('conn-badge');
    const labels = { connecting: 'connecting', live: 'live', offline: 'reconnecting' };
    badge.textContent = labels[state] || state;
    badge.className = 'badge badge-' + (state === 'live' ? 'live' : state === 'offline' ? 'offline' : 'connecting');
  }

  // -------------------------------------------------------------------- auth

  function onAuthed(data) {
    S.me = data.user;
    S.csrf = data.csrf;
    if (data.ice_servers) S.iceServers = data.ice_servers;
    if (data.limits) S.limits = data.limits;
    $('who').textContent = S.me.username;
    showView('lobby');
    loadRooms();
  }

  async function loadRooms() {
    setError('lobby-error', '');
    try {
      const data = await api('/api/rooms');
      S.rooms = data.rooms || [];
      renderRooms();
    } catch (err) {
      if (err.status === 401) {
        resetToLogin();
        return;
      }
      setError('lobby-error', err.message);
    }
  }

  function renderRooms() {
    const list = $('room-list');
    list.textContent = '';
    $('room-empty').hidden = S.rooms.length > 0;
    for (const room of S.rooms) {
      const card = document.createElement('article');
      card.className = 'room-card' + (room.closed ? ' is-closed' : '');

      const avatar = document.createElement('div');
      avatar.className = 'room-avatar';
      avatar.textContent = initial(room.name);

      const info = document.createElement('div');
      info.className = 'room-info';
      const title = document.createElement('h3');
      title.textContent = room.name;
      const sub = document.createElement('p');
      sub.className = 'room-sub';
      const bits = [room.occupants + '/' + room.max_participants + ' connected'];
      if (room.has_password) bits.push('password');
      if (room.closed) bits.push('closed');
      sub.textContent = bits.join(' · ');
      info.appendChild(title);
      info.appendChild(sub);

      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'btn primary small';
      if (room.closed) {
        button.textContent = 'Closed';
        button.disabled = true;
      } else if (!room.can_join) {
        button.textContent = 'Full';
        button.disabled = true;
      } else {
        button.textContent = 'Join';
        button.addEventListener('click', () => joinRoom(room));
      }

      card.appendChild(avatar);
      card.appendChild(info);
      card.appendChild(button);
      list.appendChild(card);
    }
  }

  async function doLogin(event) {
    event.preventDefault();
    setError('login-error', '');
    const button = $('login-submit');
    button.disabled = true;
    try {
      const data = await api('/api/login', {
        method: 'POST',
        body: {
          username: $('login-username').value.trim(),
          password: $('login-password').value,
        },
      });
      $('login-password').value = '';
      onAuthed(data);
    } catch (err) {
      setError('login-error', err.message);
    } finally {
      button.disabled = false;
    }
  }

  async function doRegister(event) {
    event.preventDefault();
    setError('invite-error', '');
    const token = ($('invite-token').value || S.inviteToken || '').trim();
    if (!token) {
      setError('invite-error', 'Paste the invite token from your link, or open the full link again.');
      return;
    }
    const button = $('invite-submit');
    button.disabled = true;
    try {
      const data = await api('/api/register', {
        method: 'POST',
        body: {
          token,
          username: $('invite-username').value.trim(),
          password: $('invite-password').value,
        },
      });
      $('invite-password').value = '';
      history.replaceState({}, '', location.pathname);
      S.inviteToken = '';
      onAuthed(data);
    } catch (err) {
      setError('invite-error', err.message);
    } finally {
      button.disabled = false;
    }
  }

  async function doLogout() {
    try {
      await api('/api/logout', { method: 'POST', body: {} });
    } catch (err) {
      /* the cookie is gone either way */
    }
    resetToLogin();
  }

  function resetToLogin() {
    leaveRoom(null, true);
    S.me = null;
    S.csrf = null;
    S.rooms = [];
    showView('login');
    $('who').textContent = '';
  }

  function showInviteForm(token) {
    $('login-form').hidden = true;
    $('invite-form').hidden = false;
    $('switch-mode').textContent = 'Back to sign in';
    if (token) {
      $('invite-token').value = token;
      $('invite-token').hidden = true;
      $('invite-token-label').hidden = true;
    } else {
      $('invite-token').hidden = false;
      $('invite-token-label').hidden = false;
    }
    showView('login');
  }

  function showLoginForm() {
    $('login-form').hidden = false;
    $('invite-form').hidden = true;
    $('switch-mode').textContent = 'I have an invite link';
    showView('login');
  }

  // ------------------------------------------------------------------- lobby

  async function joinRoom(room) {
    if (S.joining) return;
    S.joining = true;
    setError('lobby-error', '');
    try {
      let password = '';
      if (room.has_password) {
        const entered = await askPassword(room.name);
        if (entered === null) return;
        password = entered;
      }
      const granted = await requestMicrophone();
      if (!granted) return;
      enterRoom(room, password);
    } finally {
      S.joining = false;
    }
  }

  async function requestMicrophone() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      showBanner(
        'The microphone API is not available. This page must be opened as https://' +
          location.host +
          ' and you must accept the certificate warning. See README.md, section "Certificate warning on Android".'
      );
      return false;
    }
    try {
      S.localStream = await navigator.mediaDevices.getUserMedia(MIC_CONSTRAINTS);
      return true;
    } catch (err) {
      setError('lobby-error', microphoneError(err));
      return false;
    }
  }

  function microphoneError(err) {
    const name = err && err.name;
    if (name === 'NotAllowedError' || name === 'SecurityError') {
      return (
        'Microphone access was denied. Tap the lock icon in the address bar, allow the microphone, then try again. ' +
        'On Android also check Settings > Apps > Chrome > Permissions > Microphone.'
      );
    }
    if (name === 'NotFoundError' || name === 'OverconstrainedError') {
      return 'No microphone was found. Connect a headset or check that Chrome can see the phone microphone.';
    }
    if (name === 'NotReadableError' || name === 'AbortError') {
      return 'The microphone is busy in another app. Close other calling apps and try again.';
    }
    if (!window.isSecureContext) {
      return (
        'This page is not a secure context. Open https://' +
        location.host +
        ' exactly, accept the certificate warning, and try again.'
      );
    }
    return 'Could not open the microphone: ' + (err && err.message ? err.message : String(err));
  }

  function askPassword(roomName) {
    const dialog = $('password-dialog');
    const input = $('password-input');
    $('password-title').textContent = roomName;
    input.value = '';
    dialog.returnValue = '';
    if (typeof dialog.showModal === 'function') {
      dialog.showModal();
      setTimeout(() => input.focus(), 40);
    }
    return new Promise((resolve) => {
      dialog.addEventListener(
        'close',
        () => {
          resolve(dialog.returnValue === 'ok' ? input.value : null);
        },
        { once: true }
      );
    });
  }

  function enterRoom(room, password) {
    S.room = {
      id: room.id,
      name: room.name,
      max_participants: room.max_participants || S.limits.max_participants,
    };
    S.joinPassword = password || '';
    S.leaving = false;
    S.muted = false;
    S.forceMuted = false;
    S.ptt = false;
    S.pttHeld = false;
    S.myPeerId = null;
    S.reconnectAttempt = 0;
    if (S.reconnectTimer) {
      clearTimeout(S.reconnectTimer);
      S.reconnectTimer = null;
    }

    $('room-name').textContent = room.name;
    $('room-count').textContent = '1/' + S.room.max_participants;
    setError('room-error', '');
    $('audio-blocked').hidden = true;
    $('ptt-bar').hidden = true;
    $('ptt-toggle').setAttribute('aria-pressed', 'false');
    clearChat();
    resetRoster();
    showView('room');
    setConn('connecting');
    applyMicState();
    renderParticipants();
    setupAnalyser(S.localStream, 'self');
    acquireWakeLock();
    startPing();
    connectSocket();
    unlockAudio();
  }

  function leaveRoom(message, silent) {
    if (!S.room && !S.ws) {
      if (message) setError('lobby-error', message);
      return;
    }
    S.leaving = true;
    send({ type: 'leave' });
    clearTimers();
    teardownPeers();
    clearRoster();
    stopLocalMedia();
    releaseWakeLock();
    if (S.ws) {
      try {
        S.ws.close(1000, 'leave');
      } catch (err) {
        /* already closing */
      }
      S.ws = null;
    }
    S.room = null;
    S.joinPassword = '';
    S.myPeerId = null;
    S.reconnectAttempt = 0;
    setConn('offline');
    showView('lobby');
    if (!silent) {
      if (message) setError('lobby-error', message);
      loadRooms();
    }
  }

  // ----------------------------------------------------------------- websocket

  function connectSocket() {
    if (!S.room || S.leaving) return;
    if (S.ws && (S.ws.readyState === WebSocket.OPEN || S.ws.readyState === WebSocket.CONNECTING)) return;
    setConn('connecting');
    const scheme = location.protocol === 'https:' ? 'wss://' : 'ws://';
    let ws;
    try {
      ws = new WebSocket(scheme + location.host + '/ws');
    } catch (err) {
      setError('room-error', 'Could not open the signaling socket: ' + err.message);
      scheduleReconnect();
      return;
    }
    S.ws = ws;

    ws.addEventListener('open', () => {
      if (S.ws !== ws) return;
      setConn('connecting');
      applyMicState();
    });

    ws.addEventListener('message', (event) => {
      if (S.ws !== ws) return;
      let message;
      try {
        message = JSON.parse(event.data);
      } catch (err) {
        return;
      }
      if (message && typeof message.type === 'string') {
        handleMessage(message);
      }
    });

    ws.addEventListener('close', (event) => {
      if (S.ws === ws) S.ws = null;
      teardownPeers();
      if (S.leaving || !S.room) return;
      if (event.code === 4001) {
        handleRemoved('An administrator removed you from the room.');
        return;
      }
      if (event.code === 4002) {
        handleRemoved('An administrator closed this room.');
        return;
      }
      if (event.code === 1008) {
        handleRemoved('The server rejected this session. Sign in again.');
        return;
      }
      setConn('offline');
      updateAllStates();
      if (event.code === 1012) {
        toast('Server restarting, reconnecting…');
      }
      if (event.code === 1006) {
        verifySessionThenReconnect();
      } else {
        scheduleReconnect();
      }
    });

    ws.addEventListener('error', () => {
      /* close handles the retry */
    });
  }

  async function verifySessionThenReconnect() {
    if (!S.room || S.leaving) return;
    try {
      await api('/api/me');
      scheduleReconnect();
    } catch (err) {
      if (err.status === 401) {
        resetToLogin();
        showBanner('Your session expired. Sign in again.', 'info');
        return;
      }
      scheduleReconnect();
    }
  }

  function handleRemoved(message) {
    S.leaving = true;
    leaveRoom(message, false);
  }

  function scheduleReconnect() {
    if (!S.room || S.leaving || S.reconnectTimer) return;
    const delay = Math.min(MAX_RECONNECT_MS, 500 * Math.pow(2, S.reconnectAttempt));
    S.reconnectAttempt += 1;
    setConn('offline');
    S.reconnectTimer = setTimeout(() => {
      S.reconnectTimer = null;
      connectSocket();
    }, delay);
  }

  function startPing() {
    if (S.pingTimer) clearInterval(S.pingTimer);
    S.pingTimer = setInterval(() => {
      if (S.ws && S.ws.readyState === WebSocket.OPEN) send({ type: 'ping' });
    }, 20000);
  }

  function clearTimers() {
    if (S.reconnectTimer) clearTimeout(S.reconnectTimer);
    if (S.pingTimer) clearInterval(S.pingTimer);
    S.reconnectTimer = null;
    S.pingTimer = null;
  }

  function handleMessage(message) {
    switch (message.type) {
      case 'hello':
        S.myPeerId = message.peer_id;
        if (Array.isArray(message.ice_servers)) S.iceServers = message.ice_servers;
        if (message.limits) S.limits = message.limits;
        if (S.room) {
          send({
            type: 'join',
            room_id: S.room.id,
            password: S.joinPassword,
            muted: S.muted || S.forceMuted,
          });
        }
        break;

      case 'joined':
        onJoined(message);
        break;

      case 'peer-joined':
        if (message.peer) {
          S.roster.set(message.peer.peer_id, message.peer);
          renderParticipants();
          updateRoomCount();
          toast(message.peer.username + ' joined');
        }
        break;

      case 'peer-left': {
        const peer = S.roster.get(message.peer_id);
        S.roster.delete(message.peer_id);
        closePeer(message.peer_id);
        renderParticipants();
        updateRoomCount();
        if (peer) toast(peer.username + ' left');
        break;
      }

      case 'offer':
        acceptOffer(message).catch((err) => setError('room-error', 'Audio setup failed: ' + err.message));
        break;

      case 'answer':
        acceptAnswer(message).catch((err) => setError('room-error', 'Audio setup failed: ' + err.message));
        break;

      case 'candidate':
        addRemoteCandidate(message).catch(() => {});
        break;

      case 'mute-state': {
        const peer = S.roster.get(message.peer_id);
        if (peer) {
          peer.muted = Boolean(message.muted);
          renderParticipants();
        }
        break;
      }

      case 'force-mute':
        S.forceMuted = true;
        S.muted = true;
        applyMicState();
        showBanner('An administrator muted your microphone. Rejoin the room to reset it.', 'info');
        break;

      case 'chat':
        appendChat(message.username, message.text, message.from === S.myPeerId);
        break;

      case 'kicked':
        handleRemoved(message.message || 'An administrator removed you from the room.');
        break;

      case 'room-closed':
        handleRemoved(message.message || 'An administrator closed this room.');
        break;

      case 'error':
        handleServerError(message);
        break;

      case 'pong':
        break;

      default:
        break;
    }
  }

  function handleServerError(message) {
    const code = message.code || 'error';
    const text = message.message || 'Server error.';
    if (['bad_password', 'room_full', 'forbidden', 'room_closed', 'not_found'].includes(code)) {
      S.leaving = true;
      leaveRoom(code === 'bad_password' ? 'Wrong room password.' : text, false);
      return;
    }
    if (code === 'rate_limited') {
      toast('You are sending messages too quickly.');
      return;
    }
    setError('room-error', text);
  }

  function onJoined(message) {
    S.myPeerId = message.you;
    S.reconnectAttempt = 0;
    if (message.room) {
      S.room = Object.assign({}, S.room, message.room);
      $('room-name').textContent = S.room.name;
      $('room-count').textContent =
        (1 + (message.peers ? message.peers.length : 0)) + '/' + S.room.max_participants;
    }
    if (Array.isArray(message.ice_servers)) S.iceServers = message.ice_servers;
    if (message.limits) S.limits = message.limits;

    teardownPeers();
    S.roster.clear();
    for (const peer of message.peers || []) {
      S.roster.set(peer.peer_id, peer);
    }
    renderParticipants();
    updateRoomCount();

    // Deterministic scheme: the newcomer offers to everyone already here.
    for (const peer of message.peers || []) {
      const entry = makePeer(peer.peer_id, true, peer);
      startOffer(entry).catch((err) => setError('room-error', 'Audio setup failed: ' + err.message));
    }

    setConn('live');
    setError('room-error', '');
    updateAllStates();
    if (S.muted || S.forceMuted) sendMute();
    acquireWakeLock();
  }

  // --------------------------------------------------------------- peer logic

  function makePeer(peerId, initiator, info) {
    const existing = S.peers.get(peerId);
    if (existing) return existing;
    const pc = new RTCPeerConnection({
      iceServers: S.iceServers || [],
      bundlePolicy: 'max-bundle',
      rtcpMuxPolicy: 'require',
    });
    const entry = {
      id: peerId,
      pc,
      initiator: Boolean(initiator),
      pending: [],
      state: pc.connectionState,
      stream: null,
      audio: null,
      source: null,
      restartTimer: null,
      info: info || S.roster.get(peerId) || null,
    };
    S.peers.set(peerId, entry);

    if (S.localStream) {
      for (const track of S.localStream.getTracks()) {
        pc.addTrack(track, S.localStream);
      }
    }

    pc.addEventListener('icecandidate', (event) => {
      if (!event.candidate) return;
      let candidate;
      try {
        candidate = event.candidate.toJSON();
      } catch (err) {
        candidate = {
          candidate: event.candidate.candidate,
          sdpMid: event.candidate.sdpMid,
          sdpMLineIndex: event.candidate.sdpMLineIndex,
        };
      }
      send({ type: 'candidate', to: peerId, candidate });
    });

    pc.addEventListener('track', (event) => {
      entry.stream = event.streams[0] || new MediaStream([event.track]);
      attachRemoteAudio(entry);
    });

    const onState = () => {
      entry.state = pc.connectionState;
      updateParticipantState(peerId);
      if (pc.connectionState === 'failed') {
        schedulePeerRestart(entry);
      }
    };
    pc.addEventListener('connectionstatechange', onState);
    pc.addEventListener('iceconnectionstatechange', () => {
      if (pc.iceConnectionState === 'failed') schedulePeerRestart(entry);
    });

    updateParticipantState(peerId);
    return entry;
  }

  async function startOffer(entry, iceRestart) {
    if (!entry || entry.pc.signalingState === 'closed') return;
    const offer = await entry.pc.createOffer(iceRestart ? { iceRestart: true } : undefined);
    await entry.pc.setLocalDescription(offer);
    tuneSender(entry.pc);
    const local = entry.pc.localDescription;
    send({ type: 'offer', to: entry.id, sdp: { type: 'offer', sdp: tuneOpusSdp(local.sdp) } });
  }

  async function acceptOffer(message) {
    let entry = S.peers.get(message.from);
    if (entry && (entry.pc.connectionState === 'failed' || entry.pc.connectionState === 'closed')) {
      closePeer(message.from);
      entry = null;
    }
    if (!entry) {
      entry = makePeer(message.from, false, S.roster.get(message.from));
      if (!S.roster.has(message.from)) {
        S.roster.set(message.from, { peer_id: message.from, username: 'Guest', muted: false });
        renderParticipants();
      }
    }
    if (entry.pc.signalingState !== 'stable') {
      try {
        await entry.pc.setLocalDescription({ type: 'rollback' });
      } catch (err) {
        /* older browsers: continue and let setRemoteDescription decide */
      }
    }
    await entry.pc.setRemoteDescription(new RTCSessionDescription(message.sdp));
    await flushCandidates(entry);
    const answer = await entry.pc.createAnswer();
    await entry.pc.setLocalDescription(answer);
    tuneSender(entry.pc);
    const local = entry.pc.localDescription;
    send({ type: 'answer', to: message.from, sdp: { type: 'answer', sdp: tuneOpusSdp(local.sdp) } });
  }

  async function acceptAnswer(message) {
    const entry = S.peers.get(message.from);
    if (!entry) return;
    if (entry.pc.signalingState !== 'have-local-offer') return;
    await entry.pc.setRemoteDescription(new RTCSessionDescription(message.sdp));
    await flushCandidates(entry);
  }

  async function addRemoteCandidate(message) {
    const entry = S.peers.get(message.from);
    if (!entry) return;
    const candidate = message.candidate;
    if (!candidate) return;
    if (!entry.pc.remoteDescription) {
      entry.pending.push(candidate);
      return;
    }
    try {
      await entry.pc.addIceCandidate(candidate);
    } catch (err) {
      /* a candidate for a description we no longer use */
    }
  }

  async function flushCandidates(entry) {
    const pending = entry.pending.splice(0);
    for (const candidate of pending) {
      try {
        await entry.pc.addIceCandidate(candidate);
      } catch (err) {
        /* ignore stale candidate */
      }
    }
  }

  function schedulePeerRestart(entry) {
    if (entry.restartTimer || S.leaving) return;
    entry.restartTimer = setTimeout(() => {
      entry.restartTimer = null;
      if (S.leaving || !S.room || !S.peers.has(entry.id)) return;
      if (entry.initiator) {
        closePeer(entry.id);
        const fresh = makePeer(entry.id, true, S.roster.get(entry.id));
        startOffer(fresh).catch(() => {});
      } else {
        updateParticipantState(entry.id);
      }
    }, ICE_RESTART_DELAY_MS);
  }

  function tuneOpusSdp(sdp) {
    if (typeof sdp !== 'string' || sdp.indexOf(' opus/48000') < 0) return sdp;
    if (sdp.indexOf('maxaveragebitrate') >= 0) return sdp;
    const lines = sdp.split('\r\n');
    let payload = null;
    let rtpmapIndex = -1;
    let fmtpIndex = -1;
    for (let i = 0; i < lines.length; i += 1) {
      const match = /^a=rtpmap:(\d+) opus\/48000/i.exec(lines[i]);
      if (match) {
        payload = match[1];
        rtpmapIndex = i;
        continue;
      }
      if (payload && lines[i].startsWith('a=fmtp:' + payload + ' ')) {
        fmtpIndex = i;
      }
    }
    if (!payload) return sdp;
    if (fmtpIndex >= 0) {
      lines[fmtpIndex] += ';' + OPUS_FMTP;
    } else if (rtpmapIndex >= 0) {
      lines.splice(rtpmapIndex + 1, 0, 'a=fmtp:' + payload + ' ' + OPUS_FMTP);
      fmtpIndex = rtpmapIndex + 1;
    }
    if (!lines.some((line) => line.startsWith('a=ptime:'))) {
      lines.splice(fmtpIndex + 1, 0, 'a=ptime:20');
    }
    return lines.join('\r\n');
  }

  function tuneSender(pc) {
    for (const sender of pc.getSenders()) {
      if (!sender.track || sender.track.kind !== 'audio') continue;
      try {
        const params = sender.getParameters();
        if (!params.encodings || params.encodings.length === 0) params.encodings = [{}];
        params.encodings[0].maxBitrate = 32000;
        const result = sender.setParameters(params);
        if (result && typeof result.catch === 'function') result.catch(() => {});
      } catch (err) {
        /* setParameters is best-effort; SDP munging already lowered the bitrate */
      }
    }
  }

  function closePeer(peerId) {
    const entry = S.peers.get(peerId);
    if (!entry) return;
    S.peers.delete(peerId);
    if (entry.restartTimer) clearTimeout(entry.restartTimer);
    try {
      entry.pc.ontrack = null;
      entry.pc.onicecandidate = null;
      entry.pc.close();
    } catch (err) {
      /* already closed */
    }
    if (entry.audio) {
      entry.audio.pause();
      entry.audio.srcObject = null;
      entry.audio.remove();
    }
    removeAnalyser(peerId);
    updateParticipantState(peerId);
  }

  function teardownPeers() {
    for (const peerId of Array.from(S.peers.keys())) {
      const entry = S.peers.get(peerId);
      if (entry && entry.restartTimer) clearTimeout(entry.restartTimer);
      if (entry && entry.audio) {
        entry.audio.pause();
        entry.audio.srcObject = null;
        entry.audio.remove();
      }
      try {
        if (entry) entry.pc.close();
      } catch (err) {
        /* already closed */
      }
      removeAnalyser(peerId);
    }
    S.peers.clear();
    const container = $('remote-audio');
    while (container.firstChild) container.removeChild(container.firstChild);
    updateAllStates();
  }

  function resetRoster() {
    S.roster.clear();
  }

  function clearRoster() {
    S.roster.clear();
    renderParticipants();
  }

  // ------------------------------------------------------------------- audio

  function unlockAudio() {
    const ctx = ensureAudioContext();
    if (ctx && ctx.state === 'suspended') {
      ctx.resume().catch(() => {});
    }
    for (const entry of S.peers.values()) {
      if (entry.audio) playAudio(entry.audio);
    }
  }

  function ensureAudioContext() {
    if (!S.audioCtx) {
      const Ctor = window.AudioContext || window.webkitAudioContext;
      if (!Ctor) return null;
      try {
        S.audioCtx = new Ctor();
      } catch (err) {
        return null;
      }
    }
    if (S.audioCtx.state === 'suspended') {
      S.audioCtx.resume().catch(() => {});
    }
    return S.audioCtx;
  }

  function playAudio(element) {
    const attempt = element.play();
    if (attempt && typeof attempt.catch === 'function') {
      attempt.catch(() => {
        $('audio-blocked').hidden = false;
      });
    }
  }

  function attachRemoteAudio(entry) {
    let element = document.getElementById('audio-' + entry.id);
    if (!element) {
      element = document.createElement('audio');
      element.id = 'audio-' + entry.id;
      element.autoplay = true;
      element.playsInline = true;
      element.setAttribute('playsinline', '');
      $('remote-audio').appendChild(element);
    }
    element.srcObject = entry.stream;
    entry.audio = element;
    const volume = S.volumes.has(entry.id) ? S.volumes.get(entry.id) : 1;
    element.volume = volume;
    playAudio(element);
    setupAnalyser(entry.stream, entry.id);
  }

  function setupAnalyser(stream, key) {
    if (!stream) return;
    const ctx = ensureAudioContext();
    if (!ctx) return;
    removeAnalyser(key);
    try {
      const source = ctx.createMediaStreamSource(stream);
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 512;
      analyser.smoothingTimeConstant = 0.55;
      source.connect(analyser);
      S.analysers.set(key, {
        source,
        analyser,
        data: new Uint8Array(analyser.fftSize),
        speaking: false,
        until: 0,
      });
      startSpeakingLoop();
    } catch (err) {
      /* analyser is cosmetic; audio still plays */
    }
  }

  function removeAnalyser(key) {
    const item = S.analysers.get(key);
    if (!item) return;
    try {
      item.source.disconnect();
      item.analyser.disconnect();
    } catch (err) {
      /* already disconnected */
    }
    S.analysers.delete(key);
  }

  function startSpeakingLoop() {
    if (S.speakingTimer) return;
    S.speakingTimer = setInterval(() => {
      const current = performance.now();
      for (const [key, item] of S.analysers) {
        item.analyser.getByteTimeDomainData(item.data);
        let sum = 0;
        for (let i = 0; i < item.data.length; i += 1) {
          const value = (item.data[i] - 128) / 128;
          sum += value * value;
        }
        const rms = Math.sqrt(sum / item.data.length);
        if (rms > SPEAKING_RMS) item.until = current + SPEAKING_HOLD_MS;
        const speaking = item.until > current;
        if (speaking !== item.speaking) {
          item.speaking = speaking;
          setSpeaking(key, speaking);
        }
      }
    }, 180);
  }

  function stopSpeakingLoop() {
    if (S.speakingTimer) clearInterval(S.speakingTimer);
    S.speakingTimer = null;
  }

  function setSpeaking(key, speaking) {
    const row = document.querySelector('.participant[data-peer="' + cssEscape(key) + '"]');
    if (row) row.classList.toggle('speaking', speaking);
  }

  function cssEscape(value) {
    if (window.CSS && typeof window.CSS.escape === 'function') return window.CSS.escape(value);
    return String(value).replace(/["\\]/g, '\\$&');
  }

  function stopLocalMedia() {
    stopSpeakingLoop();
    for (const key of Array.from(S.analysers.keys())) removeAnalyser(key);
    if (S.localStream) {
      for (const track of S.localStream.getTracks()) track.stop();
      S.localStream = null;
    }
    if (S.audioCtx) {
      try {
        S.audioCtx.close();
      } catch (err) {
        /* already closed */
      }
      S.audioCtx = null;
    }
    $('audio-blocked').hidden = true;
  }

  // -------------------------------------------------------------- participants

  function renderParticipants() {
    const list = $('participants');
    list.textContent = '';
    if (S.me) {
      list.appendChild(buildParticipantRow({ peer_id: 'self', username: S.me.username, muted: S.muted }, true));
    }
    for (const peer of S.roster.values()) {
      list.appendChild(buildParticipantRow(peer, false));
    }
    updateAllStates();
  }

  function buildParticipantRow(peer, isSelf) {
    const row = document.createElement('li');
    row.className = 'participant';
    row.dataset.peer = isSelf ? 'self' : peer.peer_id;

    const avatar = document.createElement('div');
    avatar.className = 'avatar';
    avatar.textContent = initial(peer.username);

    const info = document.createElement('div');
    const name = document.createElement('p');
    name.className = 'name';
    name.textContent = peer.username + (isSelf ? ' (you)' : '');
    const state = document.createElement('p');
    state.className = 'state';
    state.textContent = isSelf ? 'you' : 'connecting';
    info.appendChild(name);
    info.appendChild(state);

    const side = document.createElement('div');
    side.className = 'volume';
    if (isSelf) {
      const mark = document.createElement('span');
      mark.className = 'muted-mark';
      mark.textContent = peer.muted ? 'muted' : 'mic on';
      mark.dataset.role = 'mic-mark';
      side.appendChild(mark);
    } else {
      if (peer.muted) {
        const mark = document.createElement('span');
        mark.className = 'muted-mark';
        mark.textContent = 'muted';
        side.appendChild(mark);
      }
      const slider = document.createElement('input');
      slider.type = 'range';
      slider.min = '0';
      slider.max = '100';
      slider.step = '1';
      slider.value = String(Math.round((S.volumes.has(peer.peer_id) ? S.volumes.get(peer.peer_id) : 1) * 100));
      slider.setAttribute('aria-label', 'Volume for ' + peer.username);
      slider.addEventListener('input', () => {
        const value = Number(slider.value) / 100;
        S.volumes.set(peer.peer_id, value);
        const entry = S.peers.get(peer.peer_id);
        if (entry && entry.audio) entry.audio.volume = value;
      });
      side.appendChild(slider);
    }

    row.appendChild(avatar);
    row.appendChild(info);
    row.appendChild(side);
    return row;
  }

  function updateRoomCount() {
    if (!S.room) return;
    $('room-count').textContent = (S.roster.size + 1) + '/' + S.room.max_participants;
  }

  function updateAllStates() {
    updateParticipantState('self');
    for (const peer of S.roster.values()) updateParticipantState(peer.peer_id);
  }

  function updateParticipantState(peerId) {
    const row = document.querySelector('.participant[data-peer="' + cssEscape(peerId) + '"]');
    if (!row) return;
    const label = row.querySelector('.state');
    if (!label) return;
    if (peerId === 'self') {
      label.textContent = S.forceMuted ? 'muted by admin' : S.muted ? 'muted' : 'live';
      label.className = 'state' + (S.muted || S.forceMuted ? '' : ' live');
      const mark = row.querySelector('[data-role="mic-mark"]');
      if (mark) mark.textContent = S.forceMuted ? 'muted by admin' : S.muted ? 'muted' : 'mic on';
      return;
    }
    const entry = S.peers.get(peerId);
    if (!entry) {
      label.textContent = S.conn === 'live' ? 'connecting' : 'reconnecting';
      label.className = 'state';
      return;
    }
    const map = {
      new: 'connecting',
      connecting: 'connecting',
      connected: 'live',
      disconnected: 'reconnecting',
      failed: 'retrying',
      closed: 'left',
    };
    const text = map[entry.state] || entry.state;
    label.textContent = text;
    label.className = 'state' + (text === 'live' ? ' live' : text === 'retrying' || text === 'left' ? ' failed' : '');
  }

  // --------------------------------------------------------------------- mic

  function localAudible() {
    if (S.forceMuted) return false;
    if (S.ptt) return S.pttHeld;
    return !S.muted;
  }

  function applyMicState() {
    const audible = localAudible();
    if (S.localStream) {
      for (const track of S.localStream.getAudioTracks()) track.enabled = audible;
    }
    const muteButton = $('mute-btn');
    muteButton.classList.toggle('is-muted', !audible);
    muteButton.setAttribute('aria-pressed', String(S.muted || S.forceMuted ? 'true' : 'false'));
    muteButton.disabled = S.forceMuted || S.ptt;
    $('mute-label').textContent = S.forceMuted
      ? 'Muted by admin'
      : S.ptt
        ? 'Push to talk'
        : S.muted
          ? 'Unmute'
          : 'Mute';
    $('mute-icon').textContent = audible ? '🎙️' : '🔇';
    $('ptt-btn').classList.toggle('is-live', S.ptt && S.pttHeld);
    updateParticipantState('self');
  }

  function toggleMute() {
    if (S.forceMuted || S.ptt) return;
    S.muted = !S.muted;
    applyMicState();
    sendMute();
  }

  function sendMute() {
    send({ type: 'mute', muted: Boolean(S.muted || S.forceMuted) });
  }

  function togglePtt() {
    S.ptt = !S.ptt;
    S.pttHeld = false;
    $('ptt-toggle').setAttribute('aria-pressed', S.ptt ? 'true' : 'false');
    $('ptt-bar').hidden = !S.ptt;
    applyMicState();
  }

  function setPttHeld(held) {
    if (!S.ptt) return;
    if (S.pttHeld === held) return;
    S.pttHeld = held;
    applyMicState();
  }

  // -------------------------------------------------------------------- chat

  function openChat() {
    $('chat-panel').hidden = false;
    $('chat-toggle').setAttribute('aria-expanded', 'true');
    S.chatUnread = 0;
    $('chat-unread').hidden = true;
    $('chat-input').focus();
    const log = $('chat-log');
    log.scrollTop = log.scrollHeight;
  }

  function closeChat(silent) {
    $('chat-panel').hidden = true;
    $('chat-toggle').setAttribute('aria-expanded', 'false');
    if (!silent) $('chat-input').blur();
  }

  function toggleChat() {
    if ($('chat-panel').hidden) openChat();
    else closeChat(false);
  }

  function appendChat(author, text, mine) {
    const log = $('chat-log');
    const line = document.createElement('p');
    line.className = 'chat-line';
    const who = document.createElement('span');
    who.className = 'chat-author';
    who.textContent = (mine ? 'You' : author) + ': ';
    line.appendChild(who);
    line.appendChild(document.createTextNode(text));
    log.appendChild(line);
    while (log.childElementCount > 200) log.removeChild(log.firstChild);
    log.scrollTop = log.scrollHeight;
    if ($('chat-panel').hidden && !mine) {
      S.chatUnread += 1;
      $('chat-unread').textContent = String(S.chatUnread);
      $('chat-unread').hidden = false;
    }
  }

  function clearChat() {
    $('chat-log').textContent = '';
    S.chatUnread = 0;
    $('chat-unread').hidden = true;
  }

  function submitChat(event) {
    event.preventDefault();
    const input = $('chat-input');
    const text = input.value.trim();
    if (!text) return;
    if (text.length > S.limits.chat_max_length) {
      toast('Message is too long.');
      return;
    }
    send({ type: 'chat', text });
    input.value = '';
  }

  // ------------------------------------------------------- wake lock / reload

  async function acquireWakeLock() {
    if (!S.room || !('wakeLock' in navigator)) return;
    if (S.wakeLock && !S.wakeLock.released) return;
    try {
      S.wakeLock = await navigator.wakeLock.request('screen');
      S.wakeLock.addEventListener('release', () => {
        S.wakeLock = null;
      });
    } catch (err) {
      /* battery saver or background tab; not fatal */
    }
  }

  function releaseWakeLock() {
    if (!S.wakeLock) return;
    try {
      S.wakeLock.release();
    } catch (err) {
      /* already released */
    }
    S.wakeLock = null;
  }

  function onVisibilityChange() {
    if (document.visibilityState !== 'visible') return;
    if (!S.room) return;
    acquireWakeLock();
    if (S.audioCtx && S.audioCtx.state === 'suspended') S.audioCtx.resume().catch(() => {});
    if (!S.ws || S.ws.readyState > WebSocket.OPEN) {
      if (S.reconnectTimer) {
        clearTimeout(S.reconnectTimer);
        S.reconnectTimer = null;
      }
      connectSocket();
    }
  }

  // ------------------------------------------------------------------ events

  function bindEvents() {
    $('login-form').addEventListener('submit', doLogin);
    $('invite-form').addEventListener('submit', doRegister);
    $('switch-mode').addEventListener('click', () => {
      if ($('invite-form').hidden) showInviteForm(S.inviteToken);
      else showLoginForm();
    });
    $('logout').addEventListener('click', doLogout);
    $('refresh-rooms').addEventListener('click', loadRooms);
    $('leave').addEventListener('click', () => leaveRoom(null, false));
    $('mute-btn').addEventListener('click', toggleMute);
    $('ptt-toggle').addEventListener('click', togglePtt);

    const ptt = $('ptt-btn');
    ptt.addEventListener('pointerdown', (event) => {
      event.preventDefault();
      setPttHeld(true);
      try {
        ptt.setPointerCapture(event.pointerId);
      } catch (err) {
        /* pointer capture is optional */
      }
    });
    const release = () => setPttHeld(false);
    ptt.addEventListener('pointerup', release);
    ptt.addEventListener('pointercancel', release);
    ptt.addEventListener('pointerleave', release);
    ptt.addEventListener('contextmenu', (event) => event.preventDefault());

    $('chat-toggle').addEventListener('click', toggleChat);
    $('chat-form').addEventListener('submit', submitChat);
    $('enable-audio').addEventListener('click', () => {
      unlockAudio();
      $('audio-blocked').hidden = true;
    });
    $('banner-close').addEventListener('click', hideBanner);
    $('password-form').addEventListener('submit', () => {
      /* dialog returnValue comes from the submitter; nothing else to do */
    });

    document.addEventListener('pointerdown', unlockAudio, true);
    document.addEventListener('visibilitychange', onVisibilityChange);
    window.addEventListener('online', () => {
      if (S.room && (!S.ws || S.ws.readyState > WebSocket.OPEN)) connectSocket();
    });

    document.addEventListener('keydown', (event) => {
      if (!S.ptt || event.repeat) return;
      const tag = (event.target && event.target.tagName) || '';
      if (tag === 'INPUT' || tag === 'TEXTAREA' || (event.target && event.target.isContentEditable)) return;
      if (event.code === 'Space') {
        event.preventDefault();
        setPttHeld(true);
      }
    });
    document.addEventListener('keyup', (event) => {
      if (!S.ptt) return;
      if (event.code === 'Space') setPttHeld(false);
    });
  }

  async function registerServiceWorker() {
    if (!('serviceWorker' in navigator) || !window.isSecureContext) return;
    try {
      await navigator.serviceWorker.register('/sw.js', { scope: '/' });
    } catch (err) {
      /* a certificate error blocks SW registration; the app still works */
    }
  }

  async function boot() {
    bindEvents();
    registerServiceWorker();

    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      showBanner(
        'The microphone API is unavailable because this is not a secure context. Open https://' +
          location.host +
          ' (not http://), accept the certificate warning, then reload. See README.md for the Android steps.'
      );
    }

    const params = new URLSearchParams(location.search);
    const invite = params.get('invite');
    if (invite) {
      S.inviteToken = invite;
      showInviteForm(invite);
    }

    try {
      const data = await api('/api/me');
      onAuthed(data);
    } catch (err) {
      if (!invite) showLoginForm();
      if (err.status && err.status !== 401) {
        showBanner('Could not reach the server: ' + err.message);
      }
    }
  }

  document.addEventListener('DOMContentLoaded', boot);
})();
