package carematch.voice.rtc

import android.content.Context
import carematch.voice.net.IceServer
import kotlinx.coroutines.suspendCancellableCoroutine
import org.webrtc.AudioSource
import org.webrtc.AudioTrack
import org.webrtc.Camera1Enumerator
import org.webrtc.Camera2Enumerator
import org.webrtc.CameraVideoCapturer
import org.webrtc.DefaultVideoDecoderFactory
import org.webrtc.DefaultVideoEncoderFactory
import org.webrtc.EglBase
import org.webrtc.IceCandidate
import org.webrtc.MediaConstraints
import org.webrtc.PeerConnection
import org.webrtc.PeerConnectionFactory
import org.webrtc.SdpObserver
import org.webrtc.SessionDescription
import org.webrtc.SurfaceTextureHelper
import org.webrtc.VideoSource
import org.webrtc.VideoTrack
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

/** Camera quality presets mirroring the web client's quality selector. */
enum class Quality(val width: Int, val height: Int, val fps: Int) {
    HIGH(1280, 720, 30),
    MED(640, 480, 30),
    LOW(352, 288, 15),
}

private class SdpHandler(
    private val onOk: (SessionDescription) -> Unit,
    private val onErr: (String) -> Unit,
) : SdpObserver {
    override fun onCreateSuccess(sdp: SessionDescription) = onOk(sdp)
    override fun onSetSuccess() {}
    override fun onCreateFailure(reason: String) = onErr(reason)
    override fun onSetFailure(reason: String) = onErr(reason)
}

/**
 * Owns PeerConnectionFactory, the local mic/camera tracks, and per-peer connections.
 * Remote audio plays automatically via WebRTC's audio device module; remote video
 * tracks are exposed so the UI can attach renderers.
 */
class RtcManager(private val appContext: Context) {
    val egl: EglBase = EglBase.create()

    private var factory: PeerConnectionFactory? = null
    private var audioSource: AudioSource? = null
    private var videoSource: VideoSource? = null
    private var surfaceHelper: SurfaceTextureHelper? = null
    private var capturer: CameraVideoCapturer? = null

    var localAudio: AudioTrack? = null
        private set
    var localVideo: VideoTrack? = null
        private set

    @Volatile var micEnabled = true
        private set
    @Volatile var cameraOff = false
        private set

    fun ensureStarted(audioOnly: Boolean, quality: Quality) {
        if (factory == null) {
            PeerConnectionFactory.initialize(
                PeerConnectionFactory.InitializationOptions.builder(appContext)
                    .setEnableInternalTracer(false)
                    .createInitializationOptions()
            )
            factory = PeerConnectionFactory.builder()
                .setVideoEncoderFactory(DefaultVideoEncoderFactory(egl.eglBaseContext, true, true))
                .setVideoDecoderFactory(DefaultVideoDecoderFactory(egl.eglBaseContext))
                .createPeerConnectionFactory()
        }
        if (localAudio == null) {
            audioSource = factory!!.createAudioSource(MediaConstraints())
            localAudio = factory!!.createAudioTrack("mic0", audioSource).apply { setEnabled(micEnabled) }
        }
        if (!audioOnly && localVideo == null) {
            startCamera(quality)
        }
    }

    private fun frontCameraName(): String? {
        val e2 = Camera2Enumerator(appContext)
        e2.deviceNames.firstOrNull { e2.isFrontFacing(it) }?.let { return it }
        val e1 = Camera1Enumerator(false)
        return e1.deviceNames.firstOrNull { e1.isFrontFacing(it) }
    }

    private fun startCamera(quality: Quality) {
        val name = frontCameraName() ?: return
        val enumerator = if (Camera2Enumerator.isSupported(appContext)) {
            Camera2Enumerator(appContext)
        } else {
            Camera1Enumerator(false)
        }
        val cap = enumerator.createCapturer(name, null) ?: return
        val helper = SurfaceTextureHelper.create("cam", egl.eglBaseContext)
        val src = factory!!.createVideoSource(false)
        cap.initialize(helper, appContext, src.capturerObserver)
        try {
            cap.startCapture(quality.width, quality.height, quality.fps)
        } catch (_: Exception) {
            // Fall back to a safe low resolution instead of failing the whole call.
            try {
                cap.startCapture(Quality.LOW.width, Quality.LOW.height, Quality.LOW.fps)
            } catch (_: Exception) {
                return
            }
        }
        capturer = cap
        surfaceHelper = helper
        videoSource = src
        localVideo = factory!!.createVideoTrack("cam0", src).apply { setEnabled(!cameraOff) }
    }

    fun setMicEnabled(enabled: Boolean) {
        micEnabled = enabled
        try {
            localAudio?.setEnabled(enabled)
        } catch (_: Exception) {
        }
    }

    fun setCameraOff(off: Boolean) {
        cameraOff = off
        try {
            localVideo?.setEnabled(!off)
        } catch (_: Exception) {
        }
    }

    fun newConnection(ice: List<IceServer>, observer: PeerConnection.Observer): PeerConnection {
        val f = factory ?: throw IllegalStateException("RtcManager not started")
        val rtcConfig = PeerConnection.RTCConfiguration(
            ice.map { s ->
                if (s.username != null && s.credential != null) {
                    PeerConnection.IceServer.builder(s.urls).setUsername(s.username).setPassword(s.credential).createIceServer()
                } else {
                    PeerConnection.IceServer.builder(s.urls).createIceServer()
                }
            }
        ).apply {
            sdpSemantics = PeerConnection.SdpSemantics.UNIFIED_PLAN
            continualGatheringPolicy = PeerConnection.ContinualGatheringPolicy.GATHER_CONTINUALLY
        }
        val pc = f.createPeerConnection(rtcConfig, observer)
            ?: throw IllegalStateException("createPeerConnection failed")
        localAudio?.let { track ->
            pc.addTrack(track, listOf("stream0"))
        }
        localVideo?.let { track ->
            pc.addTrack(track, listOf("stream0"))
        }
        return pc
    }

    suspend fun createOffer(pc: PeerConnection): SessionDescription =
        suspendCancellableCoroutine { cont ->
            val c = MediaConstraints()
            pc.createOffer(object : SdpObserver {
                override fun onCreateSuccess(sdp: SessionDescription) {
                    if (cont.isActive) cont.resume(sdp)
                }

                override fun onSetSuccess() {}
                override fun onCreateFailure(reason: String) {
                    if (cont.isActive) cont.resumeWithException(IllegalStateException(reason))
                }

                override fun onSetFailure(reason: String) {}
            }, c)
        }

    suspend fun createAnswer(pc: PeerConnection): SessionDescription =
        suspendCancellableCoroutine { cont ->
            pc.createAnswer(object : SdpObserver {
                override fun onCreateSuccess(sdp: SessionDescription) {
                    if (cont.isActive) cont.resume(sdp)
                }

                override fun onSetSuccess() {}
                override fun onCreateFailure(reason: String) {
                    if (cont.isActive) cont.resumeWithException(IllegalStateException(reason))
                }

                override fun onSetFailure(reason: String) {}
            }, MediaConstraints())
        }

    suspend fun setLocal(pc: PeerConnection, sdp: SessionDescription) =
        suspendCancellableCoroutine { cont ->
            pc.setLocalDescription(object : SdpObserver {
                override fun onSetSuccess() {
                    if (cont.isActive) cont.resume(Unit)
                }

                override fun onSetFailure(reason: String) {
                    if (cont.isActive) cont.resumeWithException(IllegalStateException(reason))
                }

                override fun onCreateSuccess(s: SessionDescription) {}
                override fun onCreateFailure(reason: String) {}
            }, sdp)
        }

    suspend fun setRemote(pc: PeerConnection, sdp: SessionDescription) =
        suspendCancellableCoroutine { cont ->
            pc.setRemoteDescription(object : SdpObserver {
                override fun onSetSuccess() {
                    if (cont.isActive) cont.resume(Unit)
                }

                override fun onSetFailure(reason: String) {
                    if (cont.isActive) cont.resumeWithException(IllegalStateException(reason))
                }

                override fun onCreateSuccess(s: SessionDescription) {}
                override fun onCreateFailure(reason: String) {}
            }, sdp)
        }

    fun dispose() {
        try {
            capturer?.stopCapture()
        } catch (_: Exception) {
        }
        try {
            capturer?.dispose()
        } catch (_: Exception) {
        }
        try {
            surfaceHelper?.dispose()
        } catch (_: Exception) {
        }
        try {
            videoSource?.dispose()
        } catch (_: Exception) {
        }
        try {
            audioSource?.dispose()
        } catch (_: Exception) {
        }
        localAudio = null
        localVideo = null
        capturer = null
        surfaceHelper = null
        videoSource = null
        audioSource = null
        try {
            factory?.dispose()
        } catch (_: Exception) {
        }
        factory = null
        try {
            egl.release()
        } catch (_: Exception) {
        }
    }
}
