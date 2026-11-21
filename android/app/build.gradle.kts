plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
    id("org.jetbrains.kotlin.plugin.serialization")
}

android {
    namespace = "carematch.voice"
    compileSdk = 36

    defaultConfig {
        // Reverse-DNS of call.carematch.co.in (Kotlin package is `carematch.voice`;
        // `in` is a Kotlin keyword so it is not used as a package segment).
        applicationId = "in.co.carematch.voice"
        // API 24+ (Android 7, 2016) covers the old armv7 phones still in use.
        minSdk = 24
        targetSdk = 36
        versionCode = 2
        versionName = "2.0.0"

        // Universal APK: ship native libs for every ABI in ONE apk so the
        // same file installs on armv7, arm64, and x86 devices/emulators.
        ndk {
            abiFilters += listOf("armeabi-v7a", "arm64-v8a", "x86", "x86_64")
        }
    }

    signingConfigs {
        // Local debug keystore lives at android/debug.keystore (gitignored,
        // generated on first build). It is for USB testing ONLY.
        getByName("debug") {
            storeFile = rootProject.file("debug.keystore")
            storePassword = "android"
            keyAlias = "androiddebugkey"
            keyPassword = "android"
        }
        // Release signing ONLY from env-provided keystore. Never commit one.
        // Unsigned release builds fail `apksigner verify`; use the debug APK
        // for USB testing until the release keystore is configured.
        create("release") {
            val storeFileEnv = System.getenv("VOICE_STORE_FILE")
            if (!storeFileEnv.isNullOrBlank()) {
                storeFile = file(storeFileEnv)
                storePassword = System.getenv("VOICE_STORE_PASSWORD")
                keyAlias = System.getenv("VOICE_KEY_ALIAS")
                keyPassword = System.getenv("VOICE_KEY_PASSWORD")
            }
        }
    }

    buildTypes {
        getByName("debug") {
            applicationIdSuffix = ".debug"
            isDebuggable = true
        }
        getByName("release") {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
            // Falls back to debug key when env keystore is absent (local USB testing only).
            val storeFileEnv = System.getenv("VOICE_STORE_FILE")
            signingConfig = if (storeFileEnv.isNullOrBlank()) {
                signingConfigs.getByName("debug")
            } else {
                signingConfigs.getByName("release")
            }
        }
    }

    // No per-ABI splits: one universal APK for all devices.
    splits {
        abi {
            isEnable = false
            reset()
            include("armeabi-v7a", "arm64-v8a", "x86", "x86_64")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    buildFeatures {
        compose = true
    }
    testOptions {
        unitTests.isReturnDefaultValues = true
    }
}

dependencies {
    val composeBom = platform("androidx.compose:compose-bom:2025.04.01")
    implementation(composeBom)
    androidTestImplementation(composeBom)

    implementation("androidx.core:core-ktx:1.17.0")
    implementation("androidx.activity:activity-compose:1.9.3")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.8.6")
    // Compose artifacts follow the BOM (single coherent set, no mixed versions).
    implementation("androidx.compose.material3:material3-android")
    implementation("androidx.compose.foundation:foundation-android")
    implementation("androidx.compose.ui:ui-android")
    implementation("androidx.datastore:datastore-preferences:1.1.1")

    // Networking: pinned versions, no dynamic ranges.
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.8.1")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.10.2")

    // WebRTC mesh media (DTLS-SRTP). Upstream no longer publishes
    // org.webrtc:google-webrtc to Maven Central; this is Infobip's republish
    // of Google's official Android build (commit pinned in its POM), the
    // newest Central-hosted build. Supply chain is pinned by
    // gradle/verification-metadata.xml — never bump blindly.
    implementation("com.infobip:google-webrtc:1.0.45036")

    testImplementation("junit:junit:4.13.2")
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.9.0")
}
