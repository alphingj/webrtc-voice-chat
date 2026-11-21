# Keep WebRTC native glue and serialization metadata intact.
-keep class org.webrtc.** { *; }
-keepattributes *Annotation*, Signature, InnerClasses, EnclosingMethod
-keepattributes RuntimeVisibleAnnotations, AnnotationDefault
-keepclassmembers class kotlinx.serialization.json.** { *; }
# Keep our serializable protocol models by name (they cross the network as JSON keys).
-keepnames class carematch.voice.net.** { *; }
