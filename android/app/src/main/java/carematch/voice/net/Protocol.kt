package carematch.voice.net

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.longOrNull

/** Server-side limits mirrored client-side (voip-video/server.py: ws_handler). */
const val MAX_NAME = 24
const val MAX_ROOM = 32
const val MAX_CHAT = 2000

fun cleanName(s: String): String = s.trim().take(MAX_NAME).ifEmpty { "user" }
fun cleanRoom(s: String): String = s.trim().take(MAX_ROOM).ifEmpty { "main" }
fun cleanChat(s: String): String = s.trim().take(MAX_CHAT)

private val json = Json { ignoreUnknownKeys = true }

private fun msg(type: String, vararg pairs: Pair<String, kotlinx.serialization.json.JsonElement>): String =
    buildJsonObject {
        put("type", JsonPrimitive(type))
        pairs.forEach { (k, v) -> put(k, v) }
    }.toString()

fun joinMsg(room: String, name: String, mode: String): String {
    val m = if (mode == "chat") "chat" else "full"
    return msg(
        "join",
        "room" to JsonPrimitive(cleanRoom(room)),
        "name" to JsonPrimitive(cleanName(name)),
        "mode" to JsonPrimitive(m)
    )
}

/** kind is "offer" or "answer"; sdpBody is {"type":..,"sdp":..} matching the web client. */
fun sdpMsg(type: String, to: String, kind: String, sdp: String): String =
    msg(
        type,
        "to" to JsonPrimitive(to),
        "sdp" to buildJsonObject {
            put("type", JsonPrimitive(kind))
            put("sdp", JsonPrimitive(sdp))
        }
    )

fun offerMsg(to: String, kind: String, sdp: String): String = sdpMsg("offer", to, kind, sdp)
fun answerMsg(to: String, kind: String, sdp: String): String = sdpMsg("answer", to, kind, sdp)

fun candidateMsg(to: String, candidate: JsonObject): String =
    msg("candidate", "to" to JsonPrimitive(to), "candidate" to candidate)

fun muteMsg(muted: Boolean): String = msg("mute", "muted" to JsonPrimitive(muted))
fun cameraMsg(cameraOff: Boolean): String = msg("camera", "cameraOff" to JsonPrimitive(cameraOff))
fun screenMsg(sharing: Boolean): String = msg("screen", "sharing" to JsonPrimitive(sharing))
fun typingMsg(typing: Boolean): String = msg("typing", "typing" to JsonPrimitive(typing))
fun chatMsg(text: String): String = msg("chat", "text" to JsonPrimitive(cleanChat(text)))
fun leaveMsg(): String = msg("leave")

// ---------------------------------------------------------------- inbound ---

data class Peer(val id: String, val name: String, val mode: String)
data class ChatItem(val from: String, val name: String, val text: String, val ts: Long)
data class IceServer(val urls: List<String>, val username: String? = null, val credential: String? = null)

sealed interface Signal {
    data class Joined(val id: String, val room: String, val peers: List<Peer>, val history: List<ChatItem>) : Signal
    data class PeerJoined(val id: String, val name: String, val mode: String) : Signal
    data class PeerLeft(val id: String, val name: String) : Signal
    data class Offer(val from: String, val kind: String, val sdp: String) : Signal
    data class Answer(val from: String, val kind: String, val sdp: String) : Signal
    data class Candidate(val from: String, val candidate: JsonObject) : Signal
    data class MuteState(val id: String, val muted: Boolean) : Signal
    data class CameraState(val id: String, val cameraOff: Boolean) : Signal
    data class ScreenState(val id: String, val sharing: Boolean) : Signal
    data class Chat(val item: ChatItem) : Signal
    data class Typing(val from: String, val name: String, val typing: Boolean) : Signal
    data class LockState(val room: String, val locked: Boolean) : Signal
    data class ForceMute(val muted: Boolean) : Signal
    data class Kicked(val reason: String) : Signal
    data class JoinDenied(val room: String, val reason: String) : Signal
    data object AdminOk : Signal
    data object AdminDenied : Signal
}

private fun JsonObject.str(key: String, default: String = ""): String =
    (this[key] as? JsonPrimitive)?.contentOrNull ?: default

private fun JsonObject.bool(key: String, default: Boolean = false): Boolean =
    (this[key] as? JsonPrimitive)?.booleanOrNull ?: default

private fun parsePeer(o: JsonObject): Peer? {
    val id = o.str("id")
    if (id.isBlank()) return null
    val mode = o.str("mode", "full").let { if (it == "chat") "chat" else "full" }
    return Peer(id, o.str("name", id), mode)
}

private fun parseChat(o: JsonObject): ChatItem? {
    val text = o.str("text")
    if (text.isBlank()) return null
    val ts = (o["ts"] as? JsonPrimitive)?.longOrNull
        ?: (o["ts"] as? JsonPrimitive)?.intOrNull?.toLong()
        ?: 0L
    return ChatItem(o.str("from"), o.str("name"), text, ts)
}

private fun parseSdp(o: JsonObject): Pair<String, String>? {
    val sdpObj = o["sdp"] as? JsonObject ?: return null
    val sdp = sdpObj.str("sdp")
    if (sdp.isBlank()) return null
    return sdpObj.str("type", "offer") to sdp
}

/** Parses one server message. Returns null for malformed/unknown input (never throws). */
fun parseSignal(text: String): Signal? {
    val o = try {
        json.parseToJsonElement(text) as? JsonObject ?: return null
    } catch (_: Exception) {
        return null
    }
    return when (o.str("type")) {
        "joined" -> {
            val id = o.str("id")
            if (id.isBlank()) return null
            val peers = (o["peers"] as? JsonArray).orEmpty().mapNotNull { (it as? JsonObject)?.let(::parsePeer) }
            val history = (o["history"] as? JsonArray).orEmpty().mapNotNull { (it as? JsonObject)?.let(::parseChat) }
            Signal.Joined(id, o.str("room", "main"), peers, history)
        }
        "peer-joined" -> {
            val id = o.str("id")
            if (id.isBlank()) null else Signal.PeerJoined(id, o.str("name", id), o.str("mode", "full").let { if (it == "chat") "chat" else "full" })
        }
        "peer-left" -> {
            val id = o.str("id")
            if (id.isBlank()) null else Signal.PeerLeft(id, o.str("name", id))
        }
        "offer" -> {
            val from = o.str("from")
            val sdp = parseSdp(o)
            if (from.isBlank() || sdp == null) null else Signal.Offer(from, sdp.first, sdp.second)
        }
        "answer" -> {
            val from = o.str("from")
            val sdp = parseSdp(o)
            if (from.isBlank() || sdp == null) null else Signal.Answer(from, sdp.first, sdp.second)
        }
        "candidate" -> {
            val from = o.str("from")
            val cand = o["candidate"] as? JsonObject
            if (from.isBlank() || cand == null) null else Signal.Candidate(from, cand)
        }
        "mute-state" -> {
            val id = o.str("id")
            if (id.isBlank()) null else Signal.MuteState(id, o.bool("muted"))
        }
        "camera-state" -> {
            val id = o.str("id")
            if (id.isBlank()) null else Signal.CameraState(id, o.bool("cameraOff"))
        }
        "screen-state" -> {
            val id = o.str("id")
            if (id.isBlank()) null else Signal.ScreenState(id, o.bool("sharing"))
        }
        "chat" -> parseChat(o)?.let { Signal.Chat(it) }
        "typing" -> Signal.Typing(o.str("from"), o.str("name"), o.bool("typing"))
        "lock-state" -> Signal.LockState(o.str("room"), o.bool("locked"))
        "force-mute" -> Signal.ForceMute(o.bool("muted", true))
        "kicked" -> Signal.Kicked(o.str("reason", "Removed by admin"))
        "join-denied" -> Signal.JoinDenied(o.str("room"), o.str("reason", "Room is locked"))
        "admin-ok" -> Signal.AdminOk
        "admin-denied" -> Signal.AdminDenied
        else -> null
    }
}

/**
 * Parses GET /config -> {"iceServers":[{urls, username?, credential?}], ...}.
 * Tolerates `urls` as a string or an array (server normalizes, client stays lenient).
 * Never throws; unknown entries are skipped.
 */
fun parseIceServers(configJson: String): List<IceServer> {
    val root = try {
        json.parseToJsonElement(configJson) as? JsonObject ?: return emptyList()
    } catch (_: Exception) {
        return emptyList()
    }
    val arr = root["iceServers"] as? JsonArray ?: return emptyList()
    return arr.mapNotNull { el ->
        val o = el as? JsonObject ?: return@mapNotNull null
        val urls = when (val u = o["urls"]) {
            is JsonPrimitive -> u.contentOrNull?.split(",")?.map { it.trim() }?.filter { it.isNotEmpty() }
            is JsonArray -> u.mapNotNull { (it as? JsonPrimitive)?.contentOrNull?.trim() }.filter { it.isNotEmpty() }
            else -> null
        }.orEmpty()
        if (urls.isEmpty()) return@mapNotNull null
        val user = (o["username"] as? JsonPrimitive)?.contentOrNull?.ifBlank { null }
        val cred = (o["credential"] as? JsonPrimitive)?.contentOrNull?.ifBlank { null }
        IceServer(urls, user, cred)
    }
}
