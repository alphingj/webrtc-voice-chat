package carematch.voice.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawing
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.material3.AssistChip
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.produceState
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import carematch.voice.call.CallSession
import carematch.voice.call.RemotePeer
import org.webrtc.SurfaceViewRenderer
import org.webrtc.VideoTrack

/** A WebRTC video track rendered into a SurfaceView. Releases the renderer on dispose. */
@Composable
private fun VideoView(track: VideoTrack?, mirror: Boolean, eglContext: org.webrtc.EglBase.Context, modifier: Modifier = Modifier) {
    var renderer by remember { mutableStateOf<SurfaceViewRenderer?>(null) }
    Box(modifier = modifier.background(MaterialTheme.colorScheme.surfaceVariant)) {
        AndroidView(
            factory = { ctx ->
                SurfaceViewRenderer(ctx).apply {
                    init(eglContext, null)
                    setMirror(mirror)
                    setEnableHardwareScaler(true)
                    renderer = this
                }
            },
            update = { view ->
                track?.let { t ->
                    try {
                        t.addSink(view)
                    } catch (_: Exception) {
                    }
                }
            },
            modifier = Modifier.fillMaxSize()
        )
        DisposableEffect(Unit) {
            onDispose {
                try {
                    track?.removeSink(renderer)
                } catch (_: Exception) {
                }
                try {
                    renderer?.release()
                } catch (_: Exception) {
                }
                renderer = null
            }
        }
        if (track == null) {
            Text(
                "No video",
                modifier = Modifier.align(Alignment.Center),
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
        }
    }
}

@Composable
fun CallScreen(session: CallSession, onLeave: () -> Unit) {
    val peers by session.peers.collectAsState()
    val chat by session.chat.collectAsState()
    val typing by session.typing.collectAsState()
    val conn by session.conn.collectAsState()
    val room by session.room.collectAsState()
    val muted by session.muted.collectAsState()
    val cameraOff by session.cameraOff.collectAsState()
    val events by session.events.collectAsState()
    var chatOpen by remember { mutableStateOf(false) }
    var draft by remember { mutableStateOf("") }
    val snacks = remember { SnackbarHostState() }
    val egl = remember { session.rtcManager.egl.eglBaseContext }

    // Local preview track appears once the camera starts; poll briefly, then settle.
    val localVideo by produceState<VideoTrack?>(initialValue = null) {
        repeat(40) {
            value = session.rtcManager.localVideo
            if (value != null) return@produceState
            kotlinx.coroutines.delay(250)
        }
    }

    events.firstOrNull()?.let { e ->
        LaunchedEffect(e.id) {
            snacks.showSnackbar(e.text)
            session.consumeEvent(e.id)
        }
    }

    val list = peers.values.sortedBy { it.name }
    val chatState = rememberLazyListState()
    LaunchedEffect(chat.size) {
        if (chat.isNotEmpty()) chatState.animateScrollToItem(chat.size - 1)
    }

    Box(modifier = Modifier.fillMaxSize().windowInsetsPadding(WindowInsets.safeDrawing)) {
        Column(modifier = Modifier.fillMaxSize().padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Column {
                    Text(room.ifBlank { "Room" }, style = MaterialTheme.typography.titleMedium)
                    Text(
                        "${list.size} peer${if (list.size == 1) "" else "s"} · $conn",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
                FilledTonalButton(onClick = { chatOpen = !chatOpen }) {
                    Text(if (chatOpen) "Hide chat" else "Chat")
                }
            }

            Row(modifier = Modifier.horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                list.forEach { p: RemotePeer ->
                    AssistChip(
                        onClick = {},
                        label = {
                            Text(
                                p.name + when {
                                    p.muted -> " · muted"
                                    p.conn == "live" -> " · live"
                                    else -> " · ${p.conn}"
                                }
                            )
                        }
                    )
                }
            }

            if (chatOpen) {
                LazyColumn(
                    state = chatState,
                    modifier = Modifier.fillMaxWidth().weight(1f).heightIn(min = 120.dp),
                    verticalArrangement = Arrangement.spacedBy(4.dp)
                ) {
                    items(chat, key = { it.from + it.ts }) { m ->
                        Card {
                            Column(modifier = Modifier.padding(8.dp)) {
                                Text(m.name, style = MaterialTheme.typography.labelSmall)
                                Text(m.text, style = MaterialTheme.typography.bodyMedium)
                            }
                        }
                    }
                }
                if (typing.isNotEmpty()) {
                    Text(
                        "${typing.values.joinToString()} typing…",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                    OutlinedTextField(
                        value = draft,
                        onValueChange = { draft = it.take(2000) },
                        placeholder = { Text("Message…") },
                        singleLine = true,
                        modifier = Modifier.weight(1f)
                    )
                    Button(onClick = { session.sendChat(draft); draft = "" }) { Text("Send") }
                }
            } else {
                Box(modifier = Modifier.fillMaxWidth().weight(1f)) {
                    if (list.isEmpty()) {
                        Text(
                            "Waiting for others to join…",
                            modifier = Modifier.align(Alignment.Center),
                            color = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    } else {
                        LazyVerticalGrid(
                            columns = if (list.size == 1) GridCells.Fixed(1) else GridCells.Adaptive(160.dp),
                            horizontalArrangement = Arrangement.spacedBy(8.dp),
                            verticalArrangement = Arrangement.spacedBy(8.dp),
                            modifier = Modifier.fillMaxSize()
                        ) {
                            items(list, key = { it.id }) { p ->
                                Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                                    VideoView(
                                        track = p.video,
                                        mirror = false,
                                        eglContext = egl,
                                        modifier = Modifier.fillMaxWidth().weight(1f).clip(MaterialTheme.shapes.medium)
                                    )
                                    Text(p.name, style = MaterialTheme.typography.labelMedium)
                                }
                            }
                        }
                    }
                    // Floating self preview, FaceTime-style.
                    if (localVideo != null && !cameraOff) {
                        VideoView(
                            track = localVideo,
                            mirror = true,
                            eglContext = egl,
                            modifier = Modifier.align(Alignment.BottomEnd).size(width = 112.dp, height = 150.dp)
                                .clip(MaterialTheme.shapes.medium)
                        )
                    }
                }
            }

            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(8.dp, Alignment.CenterHorizontally)
            ) {
                val btn = Modifier.heightIn(min = 52.dp)
                Button(
                    onClick = { session.toggleMute() },
                    colors = if (muted) ButtonDefaults.buttonColors(containerColor = MaterialTheme.colorScheme.error) else ButtonDefaults.buttonColors(),
                    modifier = btn
                ) { Text(if (muted) "Unmute" else "Mute") }
                Button(onClick = { session.toggleCamera() }, modifier = btn) {
                    Text(if (cameraOff) "Cam on" else "Cam off")
                }
                Button(
                    onClick = {
                        session.leave()
                        onLeave()
                    },
                    colors = ButtonDefaults.buttonColors(containerColor = MaterialTheme.colorScheme.error),
                    modifier = btn
                ) { Text("Leave") }
            }
        }
        SnackbarHost(hostState = snacks, modifier = Modifier.align(Alignment.BottomCenter))
    }
}
