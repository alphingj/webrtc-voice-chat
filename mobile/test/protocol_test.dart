import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:voice/protocol.dart';

void main() {
  test('cleaners mirror server limits', () {
    expect(cleanName('   '), 'user');
    expect(cleanName('a' * 40).length, 24);
    expect(cleanRoom(''), 'main');
    expect(cleanRoom('r' * 50).length, 32);
    expect(cleanChat('x' * 5000).length, 2000);
  });

  test('join json matches web client shape', () {
    final m = jsonDecode(joinJson('main', 'amy', 'full')) as Map;
    expect(m['type'], 'join');
    expect(m['room'], 'main');
    expect(m['name'], 'amy');
    expect(m['mode'], 'full');
    final c = jsonDecode(joinJson('x', 'amy', 'chat')) as Map;
    expect(c['mode'], 'chat');
  });

  test('ice parsing tolerates string urls and bad input', () {
    const body = '{"iceServers":['
        '{"urls":["stun:100.87.117.55:3478","stun:192.168.1.42:3478"]},'
        '{"urls":["turn:100.87.117.55:3478"],"username":"voice","credential":"secret"},'
        '{"urls":"stun:192.168.1.42:3478"}'
        '],"turnConfigured":true}';
    final ice = parseIceServers(body);
    expect(ice.length, 3);
    expect(ice[0].urls, ['stun:100.87.117.55:3478', 'stun:192.168.1.42:3478']);
    expect(ice[1].username, 'voice');
    expect(ice[2].urls, ['stun:192.168.1.42:3478']);
    expect(parseIceServers(''), isEmpty);
    expect(parseIceServers('garbage'), isEmpty);
    expect(parseIceServers('{}'), isEmpty);
  });
}
