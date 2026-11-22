package carematch.voice.call

import android.content.Context
import android.util.Log
import carematch.voice.net.ChatItem
import carematch.voice.net.IceServer
import carematch.voice.net.Peer
import carematch.voice.net.SignalingClient
import carematch.voice.net.Signal
import carematch.voice.net.answerMsg
import carematch.voice.net.candidateMsg
import carematch.voice.net.cameraMsg
import carematch.voice.net.chatMsg
import carematch.voice.net.joinMsg
import carematch.voice.net.leaveMsg
import carematch.voice.net.muteMsg
import carematch.voice.net.offerMsg
import carematch.voice.net.parseIceServers
import carematch.voice.net.screenMsg
import carematch.voice.net.typingMsg
import carematch.voice.rtc.Quality
import carematch.voice.rtc.RtcManager
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.intOrNull
import okhttp3.OkHttpClient
import okhttp3.Request
import org.webrtc.IceCandidate
import org.webrtc.MediaStream
import org.webrtc.PeerConnection
import org.webrtc.RtpReceiver
import org.webrtc.SessionDescription
import org.webrtc.VideoTrack

data class RemotePeer(
    val id: String,
    val name: String,
    val mode: String = "full",
    val conn: String = "connecting",
    val muted: Boolean = false,
    val cameraOff: Boolean = false,
    val sharing: Boolean = false,
    val video: VideoTrack? = null,
)

data class UiEvent(val text: String, val id: Long = System.currentTimeMillis())

/**
 * One call lifetime. Owns the socket, the PeerConnections and the UI state.
 * Signaling reconnects automatically; on rejoin the server sends a fresh roster
 * and we re-offer to every peer (same as the web client).
 */
class CallSession(
    appContext: Context,
    private val http: OkHttpClient,
    private val scope: CoroutineScope,
) : SignalingClient.Listener {

    private companion object {
        const val TAG = "VoiceCall"
    }

    private val rtc = RtcManager(appContext)
    private val signaling = SignalingClient(http, this)

    private val pcs = mutableMapOf<String, PeerConnection>()

    private val _conn = MutableStateFlow("connecting")
    val conn: StateFlow<String> = _conn.asStateFlow()

    private val _selfId = MutableStateFlow("")
    val selfId: StateFlow<String> = _selfId.asStateFlow()

    private val _room = MutableStateFlow("")
    val room: StateFlow<String> = _room.asStateFlow()

    private val _peers = MutableStateFlow<Map<String, RemotePeer>>(emptyMap())
    val peers: StateFlow<Map<String, RemotePeer>> = _peers.asStateFlow()

    private val _chat = MutableStateFlow<List<ChatItem>>(emptyList())
    val chat: StateFlow<List<ChatItem>> = _chat.asStateFlow()

    private val _typing = MutableStateFlow<Map<String, String>>(emptyMap())
    val typing: StateFlow<Map<String, String>> = _typing.asStateFlow()

    private val _events = MutableStateFlow<List<UiEvent>>(emptyList())
    val events: StateFlow<List<UiEvent>> = _events.asStateFlow()

    private val _muted = MutableStateFlow(false)
    val muted: StateFlow<Boolean> = _muted.asStateFlow()

    private val _cameraOff = MutableStateFlow(false)
    val cameraOff: StateFlow<Boolean> = _cameraOff.asStateFlow()

    val rtcManager: RtcManager get() = rtc

    private var host = ""
    private var roomName = ""
    private var userName = ""
    private var mode = "full"
    private var audioOnly = false
    private var quality = Quality.MED
    private var ice: List<IceServer> = listOf(IceServer(listOf("stun:100.87.117.55:3478", "stun:192.168.1.42:3478")))
    private var live = false

    fun join(host: String, room: String, name: String, mode: String, audioOnly: Boolean, quality: Quality) {
        this.host = host
        this.roomName = room
        this.userName = name
        this.mode = mode
        this.audioOnly = audioOnly
        this.quality = quality
        live = true
        _room.value = room
        _conn.value = "connecting"
        Log.i(TAG, "join start host=$host room=$room mode=$mode audioOnly=$audioOnly quality=$quality")
        scope.launch(Dispatchers.IO) {
            try {
                rtc.ensureStarted(audioOnly || mode == "chat", quality)
                Log.i(TAG, "media started (mic=${rtc.localAudio != null} cam=${rtc.localVideo != null})")
            } catch (t: Throwable) {
                Log.e(TAG, "media start failed", t)
                event("Media failed: ${t.message ?: "unknown"}")
                return@launch
            }
            // Never let a slow /config stall the join: cap the wait, then
            // connect with whatever ICE we have (defaults are the Pi itself).
            val t0 = System.currentTimeMillis()
            val fetched = try {
                kotlinx.coroutines.withTimeoutOrNull(8_000) { fetchIce() } ?: emptyList()
            } catch (t: Throwable) {
                Log.e(TAG, "ice fetch failed", t)
                emptyList()
            }
            Log.i(TAG, "ice fetch done in ${System.currentTimeMillis() - t0}ms: ${fetched.size} servers")
            if (fetched.isNotEmpty()) ice = fetched
            withContext(Dispatchers.Main) {
                if (!live) return@withContext
                if (mode == "chat") _muted.value = true
                rtc.setMicEnabled(!_muted.value)
                rtc.setCameraOff(audioOnly || mode == "chat" || _cameraOff.value)
                signaling.connect("wss://$host/ws") { joinMsg(roomName, userName, mode) }
            }
        }
    }

    private fun fetchIce(): List<IceServer> {
        return try {
            val req = Request.Builder().url("https://$host/config").get().build()
            http.newCall(req).execute().use { res ->
                val body = res.body?.string().orEmpty()
                if (!res.isSuccessful || body.isBlank()) emptyList() else parseIceServers(body)
            }
        } catch (_: Exception) {
            emptyList()
        }
    }

    // ------------------------------------------------------------ signaling ---

    override fun onOpen() {
        Log.i(TAG, "signaling open")
        _conn.value = "live"
    }

    @Volatile private var wasLive = false

    override fun onClosed(willRetry: Boolean) {
        Log.w(TAG, "signaling closed willRetry=$willRetry wasLive=$wasLive")
        _conn.value = if (willRetry) "connecting" else "closed"
        if (willRetry && wasLive) event("Connection lost — reconnecting…")
        if (!willRetry) wasLive = false
    }

    override fun onSignal(s: Signal) {
        when (s) {
            is Signal.Joined -> {
                Log.i(TAG, "joined id=${s.id} room=${s.room} peers=${s.peers.size} history=${s.history.size}")
                wasLive = true
                _selfId.value = s.id
                _room.value = s.room
                _conn.value = "live"
                s.peers.forEach { addPeer(it, announce = false) }
                _chat.value = s.history
                if (mode != "chat") scope.launch { s.peers.forEach { offerTo(it.id) } }
            }
            is Signal.PeerJoined -> addPeer(Peer(s.id, s.name, s.mode), announce = true)
            is Signal.PeerLeft -> removePeer(s.id)
            is Signal.Offer -> scope.launch { answerFrom(s.from, s.kind, s.sdp) }
            is Signal.Answer -> scope.launch { handleAnswer(s.from, s.kind, s.sdp) }
            is Signal.Candidate -> scope.launch { addCandidate(s.from, s.candidate) }
            is Signal.MuteState -> _peers.update { m ->
                m[s.id]?.let { m + (s.id to it.copy(muted = s.muted)) } ?: m
            }
            is Signal.CameraState -> _peers.update { m ->
                m[s.id]?.let { m + (s.id to it.copy(cameraOff = s.cameraOff)) } ?: m
            }
            is Signal.ScreenState -> _peers.update { m ->
                m[s.id]?.let { m + (s.id to it.copy(sharing = s.sharing)) } ?: m
            }
            is Signal.Chat -> _chat.update { it + s.item }
            is Signal.Typing -> _typing.update { m ->
                if (s.typing) m + (s.from to s.name) else m - s.from
            }
            is Signal.LockState -> event(if (s.locked) "Room locked by admin" else "Room unlocked")
            is Signal.ForceMute -> {
                setMutedUi(s.muted)
                event(if (s.muted) "Muted by admin" else "Unmuted by admin")
            }
            is Signal.Kicked -> {
                event(s.reason.ifBlank { "Removed by admin" })
                leave()
            }
            is Signal.JoinDenied -> {
                event(s.reason.ifBlank { "Join denied" })
                leave()
            }
            Signal.AdminOk, Signal.AdminDenied -> Unit
        }
    }

    // ------------------------------------------------------------------ rtc ---

    private fun addPeer(p: Peer, announce: Boolean) {
        if (p.id == _selfId.value) return
        _peers.update { m ->
            val cur = m[p.id]
            m + (p.id to (cur?.copy(name = p.name, mode = p.mode) ?: RemotePeer(p.id, p.name, p.mode)))
        }
        if (announce) event("${p.name} joined")
    }

    private fun removePeer(id: String) {
        val name = _peers.value[id]?.name
        try {
            pcs.remove(id)?.close()
        } catch (_: Exception) {
        }
        _peers.update { it - id }
        if (name != null) event("$name left")
    }

    private fun pcFor(id: String): PeerConnection {
        pcs[id]?.let { return it }
        val pc = rtc.newConnection(ice, object : PeerConnection.Observer {
            override fun onIceCandidate(c: IceCandidate) {
                val cand = buildJsonObject {
                    put("candidate", JsonPrimitive(c.sdp))
                    put("sdpMid", JsonPrimitive(c.sdpMid ?: ""))
                    put("sdpMLineIndex", JsonPrimitive(c.sdpMLineIndex))
                }
                signaling.send(candidateMsg(id, cand))
            }

            override fun onAddTrack(receiver: RtpReceiver, streams: Array<out MediaStream>) {
                val track = receiver.track() as? VideoTrack ?: return
                _peers.update { m ->
                    m[id]?.let { m + (id to it.copy(video = track)) } ?: m
                }
            }

            override fun onIceConnectionChange(state: PeerConnection.IceConnectionState) {
                Log.i(TAG, "peer $id ICE state=$state")
            }

            override fun onConnectionChange(state: PeerConnection.PeerConnectionState) {
                Log.i(TAG, "peer $id connection state=$state")
                val label = when (state) {
                    PeerConnection.PeerConnectionState.CONNECTED -> "live"
                    PeerConnection.PeerConnectionState.FAILED -> "retrying"
                    PeerConnection.PeerConnectionState.DISCONNECTED -> "retrying"
                    PeerConnection.PeerConnectionState.CLOSED -> "closed"
                    else -> "connecting"
                }
                _peers.update { m ->
                    m[id]?.let { m + (id to it.copy(conn = label)) } ?: m
                }
            }

            override fun onIceConnectionReceivingChange(receiving: Boolean) {}
            override fun onIceGatheringChange(state: PeerConnection.IceGatheringState) {}
            override fun onSignalingChange(state: PeerConnection.SignalingState) {}
            override fun onIceCandidatesRemoved(candidates: Array<out IceCandidate>) {}
            override fun onRemoveStream(stream: MediaStream) {}
            override fun onDataChannel(dc: org.webrtc.DataChannel) {}
            override fun onRenegotiationNeeded() {}
            override fun onAddStream(stream: MediaStream) {}
        })
        pcs[id] = pc
        return pc
    }

    private suspend fun offerTo(id: String) {
        try {
            val pc = withContext(Dispatchers.Main) { pcFor(id) }
            val offer = withContext(Dispatchers.Main) { rtc.createOffer(pc) }
            withContext(Dispatchers.Main) { rtc.setLocal(pc, offer) }
            signaling.send(offerMsg(id, offer.type.canonicalForm(), offer.description))
        } catch (t: Throwable) {
            event("Call setup failed: ${t.message ?: "unknown"}")
        }
    }

    private suspend fun answerFrom(id: String, kind: String, sdp: String) {
        try {
            val pc = withContext(Dispatchers.Main) { pcFor(id) }
            withContext(Dispatchers.Main) {
                rtc.setRemote(pc, SessionDescription(typeOf(kind), sdp))
            }
            val answer = withContext(Dispatchers.Main) { rtc.createAnswer(pc) }
            withContext(Dispatchers.Main) { rtc.setLocal(pc, answer) }
            signaling.send(answerMsg(id, answer.type.canonicalForm(), answer.description))
        } catch (t: Throwable) {
            event("Answer failed: ${t.message ?: "unknown"}")
        }
    }

    private suspend fun handleAnswer(id: String, kind: String, sdp: String) {
        val pc = pcs[id] ?: return
        try {
            withContext(Dispatchers.Main) { rtc.setRemote(pc, SessionDescription(typeOf(kind), sdp)) }
        } catch (t: Throwable) {
            event("Remote description failed: ${t.message ?: "unknown"}")
        }
    }

    private suspend fun addCandidate(id: String, cand: JsonObject) {
        val pc = pcs[id] ?: return
        try {
            val sdpMid = (cand["sdpMid"] as? JsonPrimitive)?.contentOrNull
            val index = (cand["sdpMLineIndex"] as? JsonPrimitive)?.intOrNull ?: 0
            val sdp = (cand["candidate"] as? JsonPrimitive)?.contentOrNull ?: return
            withContext(Dispatchers.Main) { pc.addIceCandidate(IceCandidate(sdpMid, index, sdp)) }
        } catch (_: Exception) {
        }
    }

    private fun typeOf(kind: String): SessionDescription.Type =
        if (kind.equals("answer", ignoreCase = true)) SessionDescription.Type.ANSWER
        else SessionDescription.Type.OFFER

    // ------------------------------------------------------------------- ui ---

    fun sendChat(text: String) {
        val clean = text.trim()
        if (clean.isEmpty()) return
        signaling.send(chatMsg(clean))
    }

    fun sendTyping(typing: Boolean) {
        signaling.send(typingMsg(typing))
    }

    private fun setMutedUi(m: Boolean) {
        _muted.value = m
        rtc.setMicEnabled(!m)
    }

    fun toggleMute() {
        val m = !_muted.value
        setMutedUi(m)
        signaling.send(muteMsg(m))
    }

    fun toggleCamera() {
        val off = !_cameraOff.value
        _cameraOff.value = off
        rtc.setCameraOff(off)
        signaling.send(cameraMsg(off))
    }

    fun setSharing(sharing: Boolean) {
        signaling.send(screenMsg(sharing))
    }

    private fun event(text: String) {
        _events.update { it + UiEvent(text) }
    }

    fun consumeEvent(id: Long) {
        _events.update { it.filterNot { e -> e.id == id } }
    }

    fun leave() {
        live = false
        try {
            signaling.send(leaveMsg())
        } catch (_: Exception) {
        }
        signaling.disconnect()
        pcs.values.forEach { pc ->
            try {
                pc.close()
            } catch (_: Exception) {
            }
        }
        pcs.clear()
        _peers.value = emptyMap()
        _conn.value = "closed"
    }

    fun release() {
        leave()
        signaling.release()
        rtc.dispose()
    }
}
