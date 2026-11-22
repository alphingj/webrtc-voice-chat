# Flutter release keeps needed symbols via its own rules; this file only
# preserves the WebRTC native glue and our serializable protocol models.
-keep class org.webrtc.** { *; }
-keepattributes *Annotation*, Signature
-keep class carematch.** { *; }
