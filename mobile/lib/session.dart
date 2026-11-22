// Call session: WebSocket signaling + flutter_webrtc mesh.
// Same protocol as the web client; media is DTLS-SRTP peer-to-peer
// (or via the self-hosted TURN from /config).
import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart';
import 'package:http/http.dart' as http;
import 'package:web_socket_channel/io.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

import 'protocol.dart';

const String kHost = 'call.carematch.co.in';
const String _tag = 'VoiceCall';

void _log(String m) => debugPrint('$_tag: $m');

class ChatMsg {
  final String from, name, text;
  final int ts;
  const ChatMsg(this.from, this.name, this.text, this.ts);
  factory ChatMsg.fromJson(Map<String, dynamic> j) => ChatMsg(
        '${j['from'] ?? ''}',
        '${j['name'] ?? ''}',
        '${j['text'] ?? ''}',
        (j['ts'] as num?)?.toInt() ?? 0,
      );
}

class RemotePeer {
  final String id;
  String name;
  String mode;
  String conn = 'connecting';
  bool muted = false;
  bool cameraOff = false;
  MediaStream? stream;
  final RTCVideoRenderer renderer = RTCVideoRenderer();
  RemotePeer(this.id, this.name, this.mode);
}

class CallSession {
  final String room, userName, mode;
  final bool audioOnly;
  final String quality;
  final VoidCallback onChanged;
  final void Function(String) onNotice;

  CallSession({
    required this.room,
    required this.userName,
    required this.mode,
    required this.audioOnly,
    required this.quality,
    required this.onChanged,
    required this.onNotice,
  });

  WebSocketChannel? _ws;
  StreamSubscription? _sub;
  Timer? _reconnect;
  bool _live = false;
  bool _wasLive = false;
  bool manualClose = false;

  String selfId = '';
  String conn = 'connecting';
  final Map<String, RemotePeer> peers = {};
  final Map<String, RTCPeerConnection> _pcs = {};
  final List<ChatMsg> chat = [];
  final Map<String, String> typing = {};

  MediaStream? localStream;
  final RTCVideoRenderer localRenderer = RTCVideoRenderer();
  bool muted = false;
  bool cameraOff = false;
  List<IceServer> _ice = defaultIce;

  static const _qmap = {
    'high': {'width': 1280, 'height': 720},
    'med': {'width': 640, 'height': 480},
    'low': {'width': 352, 'height': 288},
  };

  Future<void> start() async {
    _live = true;
    await localRenderer.initialize();
    if (mode != 'chat') {
      try {
        final q = _qmap[quality] ?? _qmap['med']!;
        localStream = await navigator.mediaDevices.getUserMedia({
          'audio': true,
          'video': audioOnly
              ? false
              : {
                  'mandatory': {
                    'minWidth': '${q['width']}',
                    'minHeight': '${q['height']}',
                    'minFrameRate': '30',
                  },
                  'facingMode': 'user',
                  'optional': [],
                },
        });
        cameraOff = audioOnly;
        localRenderer.srcObject = localStream;
        _log('media started audio=${localStream!.getAudioTracks().length} video=${localStream!.getVideoTracks().length}');
      } catch (e) {
        _log('media failed: $e');
        onNotice('Media failed: $e');
        return;
      }
    } else {
      muted = true;
    }
    await _fetchIce();
    _connect();
  }

  Future<void> _fetchIce() async {
    final t0 = DateTime.now().millisecondsSinceEpoch;
    try {
      final r = await http
          .get(Uri.parse('https://$kHost/config'))
          .timeout(const Duration(seconds: 8));
      final parsed = parseIceServers(r.body);
      _log('ice fetch ${DateTime.now().millisecondsSinceEpoch - t0}ms status=${r.statusCode} servers=${parsed.length}');
      if (parsed.isNotEmpty) _ice = parsed;
    } catch (e) {
      _log('ice fetch failed: $e');
    }
  }

  void _connect() {
    if (!_live || manualClose) return;
    _log('ws connecting');
    try {
      _ws = IOWebSocketChannel.connect(
        Uri.parse('wss://$kHost/ws'),
        pingInterval: const Duration(seconds: 20),
      );
    } catch (e) {
      _log('ws connect threw: $e');
      _scheduleReconnect();
      return;
    }
    _sub = _ws!.stream.listen(
      (data) {
        if (data is String) _onMessage(data);
      },
      onError: (e) {
        _log('ws error: $e');
        _onClosed();
      },
      onDone: () {
        _log('ws done');
        _onClosed();
      },
      cancelOnError: true,
    );
    _sendRaw(joinJson(room, userName, mode));
    conn = 'live';
    onChanged();
  }

  void _onClosed() {
    conn = 'connecting';
    onChanged();
    if (_wasLive) onNotice('Connection lost — reconnecting…');
    _scheduleReconnect();
  }

  void _scheduleReconnect() {
    if (!_live || manualClose) return;
    _reconnect?.cancel();
    _reconnect = Timer(const Duration(seconds: 3), _connect);
  }

  void _sendRaw(String s) {
    try {
      _ws?.sink.add(s);
    } catch (e) {
      _log('send failed: $e');
    }
  }

  void _send(Map<String, dynamic> m) => _sendRaw(jsonEncode(m));

  // ---------------------------------------------------------- signaling ---
  Future<void> _onMessage(String text) async {
    _log('rx ${text.length > 120 ? text.substring(0, 120) : text}');
    Map<String, dynamic> m;
    try {
      m = jsonDecode(text) as Map<String, dynamic>;
    } catch (_) {
      return;
    }
    switch (m['type']) {
      case 'joined':
        selfId = '${m['id']}';
        _wasLive = true;
        conn = 'live';
        for (final p in (m['peers'] as List? ?? [])) {
          if (p is Map) _addPeer('${p['id']}', '${p['name'] ?? p['id']}', '${p['mode'] ?? 'full'}');
        }
        for (final h in (m['history'] as List? ?? [])) {
          if (h is Map) chat.add(ChatMsg.fromJson(h.cast<String, dynamic>()));
        }
        _log('joined id=$selfId peers=${peers.length} history=${chat.length}');
        onChanged();
        if (mode != 'chat') {
          for (final id in peers.keys.toList()) {
            _offerTo(id);
          }
        }
        break;
      case 'peer-joined':
        _addPeer('${m['id']}', '${m['name'] ?? m['id']}', '${m['mode'] ?? 'full'}');
        onNotice('${m['name'] ?? 'Someone'} joined');
        break;
      case 'peer-left':
        _removePeer('${m['id']}');
        break;
      case 'offer':
        _answerFrom('${m['from']}', (m['sdp'] as Map).cast<String, dynamic>());
        break;
      case 'answer':
        final pc = _pcs['${m['from']}'];
        final sdp = (m['sdp'] as Map).cast<String, dynamic>();
        try {
          await pc?.setRemoteDescription(RTCSessionDescription('${sdp['sdp']}', '${sdp['type'] ?? 'answer'}'));
        } catch (e) {
          _log('setRemote answer failed: $e');
        }
        break;
      case 'candidate':
        final pc = _pcs['${m['from']}'];
        final c = (m['candidate'] as Map).cast<String, dynamic>();
        try {
          await pc?.addCandidate(RTCIceCandidate('${c['candidate']}', c['sdpMid'] as String?, (c['sdpMLineIndex'] as num?)?.toInt() ?? 0));
        } catch (e) {
          _log('addCandidate failed: $e');
        }
        break;
      case 'mute-state':
        peers['${m['id']}']?.muted = m['muted'] == true;
        onChanged();
        break;
      case 'camera-state':
        peers['${m['id']}']?.cameraOff = m['cameraOff'] == true;
        onChanged();
        break;
      case 'screen-state':
        onChanged();
        break;
      case 'chat':
        chat.add(ChatMsg.fromJson(m));
        onChanged();
        break;
      case 'typing':
        if (m['typing'] == true) {
          typing['${m['from']}'] = '${m['name']}';
        } else {
          typing.remove('${m['from']}');
        }
        onChanged();
        break;
      case 'force-mute':
        setMuted(m['muted'] == true, broadcast: false);
        onNotice(m['muted'] == true ? 'Muted by admin' : 'Unmuted by admin');
        break;
      case 'kicked':
        onNotice('${m['reason'] ?? 'Removed by admin'}');
        leave();
        break;
      case 'join-denied':
        onNotice('${m['reason'] ?? 'Join denied'}');
        leave();
        break;
    }
  }

  // ---------------------------------------------------------------- rtc ---
  void _addPeer(String id, String name, String mode_) {
    if (id.isEmpty || id == selfId) return;
    final cur = peers[id];
    if (cur != null) {
      cur.name = name;
      cur.mode = mode_;
    } else {
      final p = RemotePeer(id, name, mode_);
      p.renderer.initialize();
      peers[id] = p;
    }
    onChanged();
  }

  Future<void> _removePeer(String id) async {
    peers.remove(id);
    try {
      await _pcs.remove(id)?.close();
    } catch (_) {}
    onChanged();
  }

  Future<RTCPeerConnection> _pcFor(String id) async {
    final existing = _pcs[id];
    if (existing != null) return existing;
    final pc = await createPeerConnection({
      'iceServers': _ice.map((s) => s.toRtc()).toList(),
      'sdpSemantics': 'unified-plan',
    });
    _pcs[id] = pc;
    final ls = localStream;
    if (ls != null) {
      for (final t in ls.getTracks()) {
        await pc.addTrack(t, ls);
      }
    }
    pc.onIceCandidate = (c) {
      _sendRaw(candidateJson(id, c.toMap()));
    };
    pc.onTrack = (e) {
      _log('track ${e.track.kind} from $id streams=${e.streams.length}');
      if (e.streams.isNotEmpty) {
        final p = peers[id];
        if (p != null) {
          p.stream = e.streams[0];
          p.renderer.srcObject = e.streams[0];
          p.conn = 'live';
          onChanged();
        }
      }
    };
    pc.onConnectionState = (s) {
      _log('peer $id conn $s');
      final p = peers[id];
      if (p != null) {
        p.conn = s == RTCPeerConnectionState.RTCPeerConnectionStateConnected
            ? 'live'
            : (s == RTCPeerConnectionState.RTCPeerConnectionStateFailed ||
                    s == RTCPeerConnectionState.RTCPeerConnectionStateDisconnected
                ? 'retrying'
                : 'connecting');
        onChanged();
      }
    };
    pc.onIceConnectionState = (s) => _log('peer $id ice $s');
    return pc;
  }

  Future<void> _offerTo(String id) async {
    try {
      final pc = await _pcFor(id);
      final offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      _sendRaw(sdpJson('offer', id, offer.type ?? 'offer', offer.sdp ?? ''));
    } catch (e) {
      _log('offer to $id failed: $e');
      onNotice('Call setup failed: $e');
    }
  }

  Future<void> _answerFrom(String id, Map<String, dynamic> sdp) async {
    try {
      final pc = await _pcFor(id);
      await pc.setRemoteDescription(RTCSessionDescription('${sdp['sdp']}', '${sdp['type'] ?? 'offer'}'));
      final answer = await pc.createAnswer();
      await pc.setLocalDescription(answer);
      _sendRaw(sdpJson('answer', id, answer.type ?? 'answer', answer.sdp ?? ''));
    } catch (e) {
      _log('answer to $id failed: $e');
      onNotice('Answer failed: $e');
    }
  }

  // ----------------------------------------------------------------- ui ---
  void setMuted(bool m, {bool broadcast = true}) {
    muted = m;
    for (final t in localStream?.getAudioTracks() ?? []) {
      t.enabled = !m;
    }
    if (broadcast) _send({'type': 'mute', 'muted': m});
    onChanged();
  }

  void toggleMute() => setMuted(!muted);

  void toggleCamera() {
    cameraOff = !cameraOff;
    for (final t in localStream?.getVideoTracks() ?? []) {
      t.enabled = !cameraOff;
    }
    _send({'type': 'camera', 'cameraOff': cameraOff});
    onChanged();
  }

  void sendChat(String text) {
    final t = cleanChat(text);
    if (t.isEmpty) return;
    _send({'type': 'chat', 'text': t});
  }

  void sendTyping(bool t) => _send({'type': 'typing', 'typing': t});

  Future<void> leave() async {
    _live = false;
    manualClose = true;
    _reconnect?.cancel();
    try {
      _send({'type': 'leave'});
    } catch (_) {}
    try {
      await _sub?.cancel();
    } catch (_) {}
    try {
      await _ws?.sink.close();
    } catch (_) {}
    for (final pc in _pcs.values) {
      try {
        await pc.close();
      } catch (_) {}
    }
    _pcs.clear();
    try {
      await localStream?.dispose();
    } catch (_) {}
    localStream = null;
    for (final p in peers.values) {
      try {
        await p.renderer.dispose();
      } catch (_) {}
    }
    peers.clear();
    conn = 'closed';
    onChanged();
  }

  Future<void> dispose() async {
    await leave();
    try {
      await localRenderer.dispose();
    } catch (_) {}
  }
}
