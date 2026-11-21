package carematch.voice

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import carematch.voice.call.CallSession
import carematch.voice.data.IdentityStore
import carematch.voice.net.parseIceServers
import carematch.voice.rtc.Quality
import carematch.voice.ui.CallScreen
import carematch.voice.ui.LoginScreen
import carematch.voice.ui.VoiceTheme
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import java.util.concurrent.TimeUnit

const val DEFAULT_HOST = "call.carematch.co.in"

class MainActivity : ComponentActivity() {

    private val http: OkHttpClient by lazy {
        OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(15, TimeUnit.SECONDS)
            .build()
    }

    private var session: CallSession? = null
    private lateinit var identity: IdentityStore
    private var pendingJoin: (() -> Unit)? = null

    private val permLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { grants ->
        if (grants.values.all { it }) pendingJoin?.invoke()
        pendingJoin = null
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        identity = IdentityStore(applicationContext)

        setContent {
            VoiceTheme {
                var inCall by remember { mutableStateOf(false) }
                var turnStatus by remember { mutableStateOf("checking TURN…") }
                val scope = rememberCoroutineScope()
                val idState by identity.watch().collectAsState(initial = null)

                LaunchedEffect(Unit) {
                    turnStatus = withContext(Dispatchers.IO) { fetchTurnStatus() }
                }

                val id = idState
                if (!inCall || session == null) {
                    LoginScreen(
                        initialName = id?.name ?: "user",
                        initialRoom = id?.room ?: "main",
                        recentRooms = id?.recent.orEmpty(),
                        turnStatus = turnStatus,
                        apkUrl = "https://$DEFAULT_HOST/download",
                        onJoin = { name, room, mode, audioOnly, quality ->
                            lifecycleScope.launch {
                                identity.save(name, room)
                                val s = CallSession(applicationContext, http, lifecycleScope)
                                    .also { session = it }
                                val start = {
                                    s.join(
                                        DEFAULT_HOST, room, name, mode, audioOnly,
                                        when (quality) {
                                            "high" -> Quality.HIGH
                                            "low" -> Quality.LOW
                                            else -> Quality.MED
                                        }
                                    )
                                    inCall = true
                                }
                                val need = mutableListOf(Manifest.permission.RECORD_AUDIO)
                                if (mode != "chat" && !audioOnly) need += Manifest.permission.CAMERA
                                val missing = need.filter {
                                    ContextCompat.checkSelfPermission(this@MainActivity, it) !=
                                        PackageManager.PERMISSION_GRANTED
                                }
                                if (missing.isEmpty()) {
                                    scope.launch(Dispatchers.Main) { start() }
                                } else {
                                    pendingJoin = { start() }
                                    permLauncher.launch(missing.toTypedArray())
                                }
                            }
                        },
                        onInstallApk = {
                            startActivity(Intent(Intent.ACTION_VIEW, Uri.parse("https://$DEFAULT_HOST/download")))
                        }
                    )
                } else {
                    CallScreen(
                        session = session!!,
                        onLeave = {
                            session?.release()
                            session = null
                            inCall = false
                        }
                    )
                }
            }
        }
    }

    private fun fetchTurnStatus(): String {
        return try {
            val req = Request.Builder().url("https://$DEFAULT_HOST/config").get().build()
            http.newCall(req).execute().use { res ->
                val body = res.body?.string().orEmpty()
                if (res.isSuccessful && parseIceServers(body).isNotEmpty()) "TURN: relay ready" else "TURN: direct only"
            }
        } catch (_: Exception) {
            "TURN: unreachable"
        }
    }

    override fun onDestroy() {
        session?.release()
        session = null
        super.onDestroy()
    }
}
