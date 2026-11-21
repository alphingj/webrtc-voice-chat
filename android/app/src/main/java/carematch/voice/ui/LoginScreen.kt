package carematch.voice.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExposedDropdownMenuBox
import androidx.compose.material3.ExposedDropdownMenuDefaults
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.MenuAnchorType
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.SegmentedButton
import androidx.compose.material3.SegmentedButtonDefaults
import androidx.compose.material3.SingleChoiceSegmentedButtonRow
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp

/**
 * Login is a single scrollable column: on short screens (landscape, small
 * phones) every option — mode, quality, recents, TURN status, APK install —
 * stays reachable by scrolling instead of being clipped.
 */
@OptIn(ExperimentalMaterial3Api::class, ExperimentalLayoutApi::class)
@Composable
fun LoginScreen(
    initialName: String,
    initialRoom: String,
    recentRooms: List<String>,
    turnStatus: String,
    apkUrl: String,
    onJoin: (name: String, room: String, mode: String, audioOnly: Boolean, quality: String) -> Unit,
    onInstallApk: () -> Unit,
) {
    var name by remember(initialName) { mutableStateOf(initialName) }
    var room by remember(initialRoom) { mutableStateOf(initialRoom) }
    var videoMode by remember { mutableStateOf(true) }
    var quality by remember { mutableStateOf("med") }
    var qualityOpen by remember { mutableStateOf(false) }
    val qualities = listOf("high" to "High — 720p", "med" to "Medium — 480p", "low" to "Low — 360p", "audio" to "Audio only")

    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(20.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp)
    ) {
        Text("Voice + Video", style = MaterialTheme.typography.headlineMedium)
        Text("Private rooms · $turnStatus", style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)

        Card(modifier = Modifier.fillMaxWidth()) {
            Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                OutlinedTextField(
                    value = name, onValueChange = { name = it.take(24) },
                    label = { Text("Username") }, singleLine = true, modifier = Modifier.fillMaxWidth()
                )
                OutlinedTextField(
                    value = room, onValueChange = { room = it.take(32) },
                    label = { Text("Room") }, singleLine = true, modifier = Modifier.fillMaxWidth()
                )
                Text("Join as", style = MaterialTheme.typography.labelMedium)
                SingleChoiceSegmentedButtonRow(modifier = Modifier.fillMaxWidth()) {
                    SegmentedButton(
                        selected = videoMode, onClick = { videoMode = true },
                        shape = SegmentedButtonDefaults.itemShape(0, 2)
                    ) { Text("Video") }
                    SegmentedButton(
                        selected = !videoMode, onClick = { videoMode = false },
                        shape = SegmentedButtonDefaults.itemShape(1, 2)
                    ) { Text("Chat only") }
                }
                if (videoMode) {
                    ExposedDropdownMenuBox(expanded = qualityOpen, onExpandedChange = { qualityOpen = it }) {
                        OutlinedTextField(
                            value = qualities.first { it.first == quality }.second,
                            onValueChange = {}, readOnly = true, label = { Text("Camera quality") },
                            trailingIcon = { ExposedDropdownMenuDefaults.TrailingIcon(qualityOpen) },
                            modifier = Modifier.menuAnchor(MenuAnchorType.PrimaryNotEditable).fillMaxWidth()
                        )
                        ExposedDropdownMenu(expanded = qualityOpen, onDismissRequest = { qualityOpen = false }) {
                            qualities.forEach { (v, label) ->
                                DropdownMenuItem(
                                    text = { Text(label) },
                                    onClick = { quality = v; qualityOpen = false }
                                )
                            }
                        }
                    }
                }
                if (recentRooms.isNotEmpty()) {
                    Text("Recent rooms", style = MaterialTheme.typography.labelMedium)
                    FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        recentRooms.forEach { r ->
                            FilterChip(selected = r == room, onClick = { room = r }, label = { Text(r) })
                        }
                    }
                }
                Button(
                    onClick = {
                        onJoin(
                            name.ifBlank { "user" },
                            room.ifBlank { "main" },
                            if (videoMode) "full" else "chat",
                            quality == "audio",
                            quality
                        )
                    },
                    modifier = Modifier.fillMaxWidth()
                ) { Text("Join room") }
            }
        }

        Card(modifier = Modifier.fillMaxWidth()) {
            Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("Android app", style = MaterialTheme.typography.titleSmall)
                Text(
                    "Prefer native audio/video? Install the APK (v2.0.0, universal: armv7/arm64/x86) — same rooms, same server: $apkUrl",
                    style = MaterialTheme.typography.bodySmall
                )
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedButton(onClick = onInstallApk) { Text("Install APK") }
                }
            }
        }
        Spacer(modifier = Modifier.height(8.dp))
    }
}
