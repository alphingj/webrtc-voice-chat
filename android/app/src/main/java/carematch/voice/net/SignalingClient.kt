package carematch.voice.net

import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit

/**
 * WebSocket signaling client with the same 3s auto-rejoin behavior as the web app.
 * Reconnects with a FRESH join each time (server keeps no client state to resume).
 */
class SignalingClient(
    private val http: OkHttpClient,
    private val listener: Listener,
) {
    interface Listener {
        fun onSignal(s: Signal)
        fun onOpen()
        fun onClosed(willRetry: Boolean)
    }

    companion object {
        const val RECONNECT_MS = 3000L
        private const val TAG = "VoiceCall"
    }

    @Volatile private var socket: WebSocket? = null
    @Volatile private var manualClose = false
    private var url: String = ""
    private var joinPayload: () -> String = { leaveMsg() }
    private val scheduler = Executors.newSingleThreadScheduledExecutor()
    private var pending: ScheduledFuture<*>? = null

    fun connect(url: String, joinPayload: () -> String) {
        disconnect()
        this.url = url
        this.joinPayload = joinPayload
        manualClose = false
        open()
    }

    private fun open() {
        Log.i(TAG, "ws connecting to $url")
        val req = Request.Builder().url(url).build()
        socket = http.newWebSocket(req, object : WebSocketListener() {
            override fun onOpen(ws: WebSocket, response: Response) {
                Log.i(TAG, "ws open (http ${response.code}), sending join")
                val sent = ws.send(joinPayload())
                Log.i(TAG, "join send queued=$sent")
                listener.onOpen()
            }

            override fun onMessage(ws: WebSocket, text: String) {
                Log.i(TAG, "ws rx: ${text.take(120)}")
                parseSignal(text)?.let(listener::onSignal)
                    ?: Log.w(TAG, "ws rx unparsed/unknown message")
            }

            override fun onClosed(ws: WebSocket, code: Int, reason: String) {
                Log.w(TAG, "ws closed code=$code reason=$reason manual=$manualClose")
                socket = null
                listener.onClosed(!manualClose)
                scheduleReconnect()
            }

            override fun onFailure(ws: WebSocket, t: Throwable, response: Response?) {
                Log.e(TAG, "ws FAILURE: ${t.javaClass.simpleName}: ${t.message} http=${response?.code}", t)
                socket = null
                listener.onClosed(!manualClose)
                scheduleReconnect()
            }
        })
    }

    private fun scheduleReconnect() {
        if (manualClose) return
        Log.i(TAG, "ws reconnect scheduled in ${RECONNECT_MS}ms")
        pending?.cancel(false)
        pending = scheduler.schedule({ if (!manualClose) open() }, RECONNECT_MS, TimeUnit.MILLISECONDS)
    }

    fun send(text: String): Boolean {
        val ok = socket?.send(text) ?: false
        if (!ok) Log.w(TAG, "ws send dropped (no socket): ${text.take(80)}")
        return ok
    }

    fun disconnect() {
        manualClose = true
        pending?.cancel(false)
        try {
            socket?.close(1000, "leave")
        } catch (_: Exception) {
        }
        socket = null
    }

    fun release() {
        disconnect()
        scheduler.shutdownNow()
        // NOTE: the shared OkHttpClient is owned by the caller and is NOT shut down here.
    }
}
