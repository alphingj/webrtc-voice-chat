// Signaling protocol shared with the web client (voip-video/server.py).
// Pure Dart so it is unit-testable without a device.
import 'dart:convert';

const int maxName = 24;
const int maxRoom = 32;
const int maxChat = 2000;

String cleanName(String s) {
  final t = s.trim();
  final cut = t.length > maxName ? t.substring(0, maxName) : t;
  return cut.isEmpty ? 'user' : cut;
}

String cleanRoom(String s) {
  final t = s.trim();
  final cut = t.length > maxRoom ? t.substring(0, maxRoom) : t;
  return cut.isEmpty ? 'main' : cut;
}

String cleanChat(String s) {
  final t = s.trim();
  return t.length > maxChat ? t.substring(0, maxChat) : t;
}

String joinJson(String room, String name, String mode) => jsonEncode({
      'type': 'join',
      'room': cleanRoom(room),
      'name': cleanName(name),
      'mode': mode == 'chat' ? 'chat' : 'full',
    });

String sdpJson(String type, String to, String kind, String sdp) => jsonEncode({
      'type': type,
      'to': to,
      'sdp': {'type': kind, 'sdp': sdp},
    });

String candidateJson(String to, Map<String, dynamic> candidate) =>
    jsonEncode({'type': 'candidate', 'to': to, 'candidate': candidate});

class IceServer {
  final List<String> urls;
  final String? username;
  final String? credential;
  const IceServer(this.urls, {this.username, this.credential});

  Map<String, dynamic> toRtc() => {
        'urls': urls,
        if (username != null) 'username': username,
        if (credential != null) 'credential': credential,
      };
}

/// Parses GET /config. Tolerates urls as string or array; never throws.
List<IceServer> parseIceServers(String body) {
  try {
    final root = jsonDecode(body);
    if (root is! Map || root['iceServers'] is! List) return const [];
    final out = <IceServer>[];
    for (final e in root['iceServers'] as List) {
      if (e is! Map) continue;
      final raw = e['urls'];
      final urls = <String>[];
      if (raw is String) {
        urls.addAll(raw.split(',').map((s) => s.trim()).where((s) => s.isNotEmpty));
      } else if (raw is List) {
        urls.addAll(raw.whereType<String>().map((s) => s.trim()).where((s) => s.isNotEmpty));
      }
      if (urls.isEmpty) continue;
      final u = e['username'];
      final c = e['credential'];
      out.add(IceServer(
        urls,
        username: (u is String && u.isNotEmpty) ? u : null,
        credential: (c is String && c.isNotEmpty) ? c : null,
      ));
    }
    return out;
  } catch (_) {
    return const [];
  }
}

/// Default ICE points at the self-hosted Pi (used only if /config fails).
const List<IceServer> defaultIce = [
  IceServer(['stun:100.87.117.55:3478', 'stun:192.168.1.42:3478']),
];
