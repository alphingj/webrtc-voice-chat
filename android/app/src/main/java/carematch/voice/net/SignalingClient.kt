package carematch.voice.net

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
        val req = Request.Builder().url(url).build()
        socket = http.newWebSocket(req, object : WebSocketListener() {
            override fun onOpen(ws: WebSocket, response: Response) {
                ws.send(joinPayload())
                listener.onOpen()
            }

            override fun onMessage(ws: WebSocket, text: String) {
                parseSignal(text)?.let(listener::onSignal)
            }

            override fun onClosed(ws: WebSocket, code: Int, reason: String) {
                socket = null
                listener.onClosed(!manualClose)
                scheduleReconnect()
            }

            override fun onFailure(ws: WebSocket, t: Throwable, response: Response?) {
                socket = null
                listener.onClosed(!manualClose)
                scheduleReconnect()
            }
        })
    }

    private fun scheduleReconnect() {
        if (manualClose) return
        pending?.cancel(false)
        pending = scheduler.schedule({ if (!manualClose) open() }, RECONNECT_MS, TimeUnit.MILLISECONDS)
    }

    fun send(text: String): Boolean = socket?.send(text) ?: false

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
