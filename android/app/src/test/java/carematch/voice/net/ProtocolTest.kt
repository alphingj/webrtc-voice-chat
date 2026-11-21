package carematch.voice.net

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.buildJsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ProtocolTest {

    @Test
    fun cleanBounds_matchServerLimits() {
        assertEquals("user", cleanName("   "))
        assertEquals(24, cleanName("a".repeat(40)).length)
        assertEquals("main", cleanRoom(""))
        assertEquals(32, cleanRoom("r".repeat(50)).length)
        assertEquals(2000, cleanChat("x".repeat(5000)).length)
    }

    @Test
    fun joinMsg_shape_matchesWebClient() {
        val parsed = parseSignal(joinMsg("main", "amy", "full"))
        // join is outbound-only; server answers with "joined" — must not parse as inbound.
        assertNull(parsed)
    }

    @Test
    fun joined_parsesRosterAndHistory() {
        val text = """{"type":"joined","id":"abc123","room":"main",
            "peers":[{"id":"p2","name":"bob","mode":"full"},{"id":"p3","name":"cat","mode":"chat"}],
            "history":[{"from":"p2","name":"bob","text":"hi","ts":1700000000000}]}"""
        val s = parseSignal(text) as? Signal.Joined ?: error("not joined")
        assertEquals("abc123", s.id)
        assertEquals(2, s.peers.size)
        assertEquals(Peer("p3", "cat", "chat"), s.peers[1])
        assertEquals(1, s.history.size)
        assertEquals("hi", s.history[0].text)
    }

    @Test
    fun sdp_roundTripsThroughParser() {
        val offer = offerMsg("peer9", "offer", "v=0\r\nfake")
        assertTrue(offer.contains("\"to\":\"peer9\""))
        // The server forwards with `from` instead of `to` — simulate that hop.
        val o = parseSignal(offer.replace("\"to\":", "\"from\":")) as? Signal.Offer ?: error("not offer")
        assertEquals("peer9", o.from)
        assertEquals("v=0\r\nfake", o.sdp)

        val cand = candidateMsg(
            "peer9",
            buildJsonObject {
                put("candidate", JsonPrimitive("candidate:1 1 udp 1 1.2.3.4 5000 typ host"))
                put("sdpMid", JsonPrimitive("0"))
                put("sdpMLineIndex", JsonPrimitive(0))
            }
        )
        val c = parseSignal(cand.replace("\"to\":", "\"from\":")) as? Signal.Candidate ?: error("not candidate")
        assertEquals("peer9", c.from)
        assertEquals(
            "candidate:1 1 udp 1 1.2.3.4 5000 typ host",
            (c.candidate["candidate"] as? JsonPrimitive)?.contentOrNull
        )
    }

    @Test
    fun malformed_neverThrows_returnsNull() {
        listOf(
            "",
            "not json",
            "[]",
            """{"type":"joined"}""",
            """{"type":"offer","from":"","sdp":{}}""",
            """{"type":"nope","id":"x"}""",
            """{"type":"chat","text":"   "}"""
        ).forEach { assertNull("input: $it", parseSignal(it)) }
    }

    @Test
    fun controlSignals_parse() {
        assertEquals(Signal.MuteState("p1", true), parseSignal("""{"type":"mute-state","id":"p1","muted":true}"""))
        assertEquals(Signal.CameraState("p1", true), parseSignal("""{"type":"camera-state","id":"p1","cameraOff":true}"""))
        assertEquals(Signal.Kicked("Removed by admin"), parseSignal("""{"type":"kicked","reason":"Removed by admin"}"""))
        assertEquals(Signal.JoinDenied("main", "Room is locked by admin"), parseSignal("""{"type":"join-denied","room":"main","reason":"Room is locked by admin"}"""))
        assertEquals(Signal.ForceMute(true), parseSignal("""{"type":"force-mute","muted":true}"""))
    }

    @Test
    fun parseIceServers_handlesLiveConfigShape() {
        // Shape observed from GET https://call.carematch.co.in/config:
        // static entries use arrays, Metered entries use a single string.
        val config = """{"iceServers":[
            {"urls":["stun:100.87.117.55:3478","stun:192.168.1.42:3478"]},
            {"urls":["turn:100.87.117.55:3478"],"username":"voice","credential":"secret"},
            {"urls":"stun:stun.relay.metered.ca:80"},
            {"urls":"turn:global.relay.metered.ca:80","username":"u","credential":"p"}
        ],"turnConfigured":true}"""
        val ice = parseIceServers(config)
        assertEquals(4, ice.size)
        assertEquals(listOf("stun:100.87.117.55:3478", "stun:192.168.1.42:3478"), ice[0].urls)
        assertEquals("voice", ice[1].username)
        assertEquals(listOf("stun:stun.relay.metered.ca:80"), ice[2].urls)
    }

    @Test
    fun parseIceServers_badInput_returnsEmpty() {
        assertTrue(parseIceServers("").isEmpty())
        assertTrue(parseIceServers("{}").isEmpty())
        assertTrue(parseIceServers("""{"iceServers":[{"urls":[]}]}""").isEmpty())
        assertTrue(parseIceServers("garbage").isEmpty())
    }
}
