import 'package:flutter/material.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart';
import 'package:http/http.dart' as http;
import 'package:permission_handler/permission_handler.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'protocol.dart';
import 'session.dart';

void main() => runApp(const VoiceApp());

class VoiceApp extends StatelessWidget {
  const VoiceApp({super.key});
  @override
  Widget build(BuildContext context) => MaterialApp(
        title: 'Voice + Video',
        debugShowCheckedModeBanner: false,
        theme: ThemeData(useMaterial3: true, colorSchemeSeed: const Color(0xFF0071E3)),
        darkTheme: ThemeData(useMaterial3: true, brightness: Brightness.dark, colorSchemeSeed: const Color(0xFF0071E3)),
        home: const LoginPage(),
      );
}

class LoginPage extends StatefulWidget {
  const LoginPage({super.key});
  @override
  State<LoginPage> createState() => _LoginPageState();
}

class _LoginPageState extends State<LoginPage> {
  final _name = TextEditingController();
  final _room = TextEditingController();
  final _nameFocus = FocusNode();
  bool _video = true;
  String _quality = 'med';
  String _error = '';
  String _turn = 'checking TURN…';
  List<String> _recent = [];
  bool _busy = false;

  static const _qualities = [
    ('high', 'High — 720p'),
    ('med', 'Medium — 480p'),
    ('low', 'Low — 360p'),
    ('audio', 'Audio only'),
  ];

  @override
  void initState() {
    super.initState();
    _load();
    _checkTurn();
  }

  Future<void> _load() async {
    final p = await SharedPreferences.getInstance();
    if (!mounted) return;
    setState(() {
      _name.text = p.getString('name') ?? '';
      _recent = p.getStringList('recent') ?? [];
    });
  }

  Future<void> _checkTurn() async {
    try {
      final r = await http.get(Uri.parse('https://$kHost/config')).timeout(const Duration(seconds: 8));
      if (!mounted) return;
      setState(() => _turn = parseIceServers(r.body).isNotEmpty ? 'TURN: relay ready (self-hosted)' : 'TURN: direct only');
    } catch (_) {
      if (mounted) setState(() => _turn = 'TURN: unreachable');
    }
  }

  Future<void> _join(String targetRoom) async {
    final name = _name.text.trim();
    final room = targetRoom.trim();
    if (name.isEmpty) {
      setState(() => _error = 'Enter a username first');
      _nameFocus.requestFocus();
      return;
    }
    if (room.isEmpty) {
      setState(() => _error = 'Enter a room name');
      return;
    }
    setState(() {
      _error = '';
      _busy = true;
    });
    // Permissions: mic always; camera unless chat-only or audio-only.
    final need = <Permission>[Permission.microphone];
    if (_video && _quality != 'audio') need.add(Permission.camera);
    final res = await need.request();
    if (!mounted) return;
    if (res.values.any((s) => !s.isGranted)) {
      setState(() {
        _busy = false;
        _error = 'Microphone/camera permission required';
      });
      return;
    }
    final p = await SharedPreferences.getInstance();
    await p.setString('name', name);
    final recent = [room, ..._recent.where((r) => r != room)].take(8).toList();
    await p.setStringList('recent', recent);
    if (!mounted) return;
    setState(() => _busy = false);
    Navigator.of(context).push(MaterialPageRoute(
      builder: (_) => CallPage(
        name: name,
        room: room,
        mode: _video ? 'full' : 'chat',
        audioOnly: _quality == 'audio',
        quality: _quality,
      ),
    ));
  }

  @override
  void dispose() {
    _name.dispose();
    _room.dispose();
    _nameFocus.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: SafeArea(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(20),
          child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
            Text('Voice + Video', style: Theme.of(context).textTheme.headlineMedium),
            const SizedBox(height: 4),
            Text('Private rooms · $_turn', style: Theme.of(context).textTheme.bodyMedium),
            const SizedBox(height: 16),
            Card(
              child: Padding(
                padding: const EdgeInsets.all(16),
                child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
                  TextField(
                    controller: _name,
                    focusNode: _nameFocus,
                    maxLength: 24,
                    decoration: const InputDecoration(labelText: 'Username', hintText: 'you', counterText: ''),
                    onChanged: (_) => setState(() => _error = ''),
                  ),
                  const SizedBox(height: 10),
                  TextField(
                    controller: _room,
                    maxLength: 32,
                    decoration: const InputDecoration(labelText: 'Room', hintText: 'room name', counterText: ''),
                    onChanged: (_) => setState(() => _error = ''),
                  ),
                  if (_error.isNotEmpty)
                    Padding(
                      padding: const EdgeInsets.only(top: 8),
                      child: Text(_error, style: TextStyle(color: Theme.of(context).colorScheme.error)),
                    ),
                  const SizedBox(height: 12),
                  SegmentedButton<bool>(
                    segments: const [
                      ButtonSegment(value: true, label: Text('Video')),
                      ButtonSegment(value: false, label: Text('Chat only')),
                    ],
                    selected: {_video},
                    onSelectionChanged: (s) => setState(() => _video = s.first),
                  ),
                  if (_video) ...[
                    const SizedBox(height: 12),
                    DropdownButtonFormField<String>(
                      value: _quality,
                      decoration: const InputDecoration(labelText: 'Camera quality'),
                      items: _qualities.map((q) => DropdownMenuItem(value: q.$1, child: Text(q.$2))).toList(),
                      onChanged: (v) => setState(() => _quality = v ?? 'med'),
                    ),
                  ],
                  const SizedBox(height: 16),
                  FilledButton(
                    onPressed: _busy ? null : () => _join(_room.text),
                    child: const Text('Join room'),
                  ),
                  if (_recent.isNotEmpty) ...[
                    const SizedBox(height: 12),
                    Text('Recent rooms — tap to join', style: Theme.of(context).textTheme.labelMedium),
                    const SizedBox(height: 6),
                    Wrap(
                      spacing: 8,
                      runSpacing: 4,
                      children: _recent
                          .map((r) => ActionChip(label: Text(r), onPressed: _busy ? null : () => _join(r)))
                          .toList(),
                    ),
                  ],
                ]),
              ),
            ),
          ]),
        ),
      ),
    );
  }
}

class CallPage extends StatefulWidget {
  final String name, room, mode, quality;
  final bool audioOnly;
  const CallPage({super.key, required this.name, required this.room, required this.mode, required this.audioOnly, required this.quality});
  @override
  State<CallPage> createState() => _CallPageState();
}

class _CallPageState extends State<CallPage> {
  late final CallSession _s;
  bool _chatOpen = false;
  final _draft = TextEditingController();
  final _chatScroll = ScrollController();

  @override
  void initState() {
    super.initState();
    _s = CallSession(
      room: widget.room,
      userName: widget.name,
      mode: widget.mode,
      audioOnly: widget.audioOnly,
      quality: widget.quality,
      onChanged: () {
        if (mounted) setState(() {});
      },
      onNotice: (m) {
        if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(m)));
      },
    );
    _s.start();
  }

  @override
  void dispose() {
    _s.dispose();
    _draft.dispose();
    _chatScroll.dispose();
    super.dispose();
  }

  Future<void> _leave() async {
    await _s.leave();
    if (mounted) Navigator.of(context).pop();
  }

  @override
  Widget build(BuildContext context) {
    final peers = _s.peers.values.toList();
    return Scaffold(
      body: SafeArea(
        child: Column(children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(12, 8, 12, 4),
            child: Row(children: [
              Expanded(
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Text(widget.room, style: Theme.of(context).textTheme.titleMedium),
                  Text('${peers.length} peer${peers.length == 1 ? '' : 's'} · ${_s.conn}',
                      style: Theme.of(context).textTheme.bodySmall),
                ]),
              ),
              IconButton(
                icon: Icon(_chatOpen ? Icons.videocam : Icons.chat_bubble_outline),
                tooltip: 'Chat',
                onPressed: () => setState(() => _chatOpen = !_chatOpen),
              ),
            ]),
          ),
          if (peers.isNotEmpty)
            SizedBox(
              height: 36,
              child: ListView(
                scrollDirection: Axis.horizontal,
                padding: const EdgeInsets.symmetric(horizontal: 12),
                children: peers
                    .map((p) => Padding(
                          padding: const EdgeInsets.only(right: 6),
                          child: Chip(label: Text('${p.name} · ${p.muted ? 'muted' : p.conn}')),
                        ))
                    .toList(),
              ),
            ),
          Expanded(child: _chatOpen ? _chatPanel() : _videoGrid(peers)),
          Padding(
            padding: const EdgeInsets.all(12),
            child: Row(mainAxisAlignment: MainAxisAlignment.center, children: [
              FilledButton(
                style: FilledButton.styleFrom(
                    backgroundColor: _s.muted ? Theme.of(context).colorScheme.error : null,
                    minimumSize: const Size(64, 52)),
                onPressed: _s.toggleMute,
                child: Text(_s.muted ? 'Unmute' : 'Mute'),
              ),
              const SizedBox(width: 8),
              FilledButton(
                style: const ButtonStyle(minimumSize: WidgetStatePropertyAll(Size(64, 52))),
                onPressed: _s.toggleCamera,
                child: Text(_s.cameraOff ? 'Cam on' : 'Cam off'),
              ),
              const SizedBox(width: 8),
              FilledButton(
                style: FilledButton.styleFrom(
                    backgroundColor: Theme.of(context).colorScheme.error,
                    minimumSize: const Size(64, 52)),
                onPressed: _leave,
                child: const Text('Leave'),
              ),
            ]),
          ),
        ]),
      ),
    );
  }

  Widget _videoGrid(List<RemotePeer> peers) {
    return Stack(children: [
      if (peers.isEmpty)
        const Center(child: Text('Waiting for others to join…'))
      else
        GridView.count(
          crossAxisCount: peers.length == 1 ? 1 : 2,
          childAspectRatio: 0.9,
          padding: const EdgeInsets.all(8),
          mainAxisSpacing: 8,
          crossAxisSpacing: 8,
          children: peers
              .map((p) => ClipRRect(
                    borderRadius: BorderRadius.circular(16),
                    child: Stack(fit: StackFit.expand, children: [
                      Container(color: Colors.black),
                      if (p.stream != null && !p.cameraOff)
                        RTCVideoView(p.renderer, objectFit: RTCVideoViewObjectFit.RTCVideoViewObjectFitCover),
                      Positioned(
                        left: 8,
                        bottom: 8,
                        child: Chip(label: Text(p.name)),
                      ),
                    ]),
                  ))
              .toList(),
        ),
      if (_s.localStream != null && !_s.cameraOff)
        Positioned(
          right: 12,
          bottom: 12,
          child: ClipRRect(
            borderRadius: BorderRadius.circular(12),
            child: SizedBox(
              width: 100,
              height: 134,
              child: RTCVideoView(_s.localRenderer, mirror: true, objectFit: RTCVideoViewObjectFit.RTCVideoViewObjectFitCover),
            ),
          ),
        ),
    ]);
  }

  Widget _chatPanel() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_chatScroll.hasClients) _chatScroll.jumpTo(_chatScroll.position.maxScrollExtent);
    });
    return Column(children: [
      Expanded(
        child: ListView.builder(
          controller: _chatScroll,
          padding: const EdgeInsets.all(12),
          itemCount: _s.chat.length,
          itemBuilder: (_, i) {
            final m = _s.chat[i];
            final me = m.from == _s.selfId;
            return Align(
              alignment: me ? Alignment.centerRight : Alignment.centerLeft,
              child: Card(
                color: me ? Theme.of(context).colorScheme.primaryContainer : null,
                child: Padding(
                  padding: const EdgeInsets.all(8),
                  child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                    Text(m.name, style: Theme.of(context).textTheme.labelSmall),
                    Text(m.text),
                  ]),
                ),
              ),
            );
          },
        ),
      ),
      if (_s.typing.isNotEmpty)
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 12),
          child: Align(
            alignment: Alignment.centerLeft,
            child: Text('${_s.typing.values.join(', ')} typing…', style: Theme.of(context).textTheme.bodySmall),
          ),
        ),
      Padding(
        padding: const EdgeInsets.all(12),
        child: Row(children: [
          Expanded(
            child: TextField(
              controller: _draft,
              maxLength: 2000,
              decoration: const InputDecoration(hintText: 'Message…', counterText: ''),
              onChanged: (v) => _s.sendTyping(v.trim().isNotEmpty),
              onSubmitted: (_) => _send(),
            ),
          ),
          const SizedBox(width: 8),
          FilledButton(onPressed: _send, child: const Text('Send')),
        ]),
      ),
    ]);
  }

  void _send() {
    _s.sendChat(_draft.text);
    _draft.clear();
    _s.sendTyping(false);
  }
}
